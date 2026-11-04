"""Publish one already-reserved next segment to an isolated simulator.

Never use for physical drive authorization. No automatic retry or recovery is
performed; an uncertain attempt retains occupancy for manual reconciliation.
"""
import copy
import math
import threading

from server.allocation_store import _id
from server.order_publisher import OrderPublisher, OrderPublishingDisabled, validate_order
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress, _read_order


class ProgressPublishingError(RuntimeError):
    """A pending next-segment order cannot safely be published."""


class ProgressPublisher:
    """One explicit send attempt, using a dedicated simulation publisher."""

    def __init__(self, progress, publisher=None, *, ack_timeout=3.0):
        if not isinstance(progress, SegmentProgress):
            raise TypeError('progress must be a SegmentProgress')
        if publisher is not None and not isinstance(publisher, OrderPublisher):
            raise TypeError('publisher must be an OrderPublisher')
        if (isinstance(ack_timeout, bool) or not isinstance(ack_timeout, (int, float))
                or not math.isfinite(ack_timeout) or not 0 < ack_timeout <= 30):
            raise ValueError('ack_timeout must be finite and in (0, 30] seconds')
        self._progress = progress
        self._publisher = publisher
        self._timeout = float(ack_timeout)
        self._lock = threading.Lock()
        self._running = False
        self._used = False
        self._owner = object()

    def _pending(self, task_id, seq):
        record = self._progress.get_order(task_id, seq)
        previous = self._progress.get_order(task_id, seq - 1)
        allocation = self._progress.store.get_allocation(task_id)
        if (not isinstance(record, dict) or record.get('state') != 'pending'
                or not isinstance(previous, dict) or previous.get('state') not in ('uncertain', 'acked')
                or not isinstance(allocation, dict)):
            raise ProgressPublishingError('a current pending next order is required')
        try:
            order = _read_order(record['payload'])
            prior = _read_order(previous['payload'])
            robot_id = allocation['robot_id']
            validate_order(robot_id, order)
            if (order['order_id'] != task_id or order['seq'] != seq
                    or prior['order_id'] != task_id or prior['seq'] != seq - 1
                    or order['map_id'] != allocation['map_id']
                    or order['nodes'] != allocation['nodes']
                    or order['released_until'] != allocation['released_until']):
                raise ValueError('order and allocation mismatch')
            segment = self._progress._segment(allocation['nodes'], allocation['released_until'])
            if not segment.nodes or segment.nodes[0] != prior['released_until']:
                raise ValueError('previous and next segments do not meet')
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ProgressPublishingError('pending order or allocation is invalid') from exc
        return robot_id, copy.deepcopy(order), record['payload'], tuple(segment.resources)

    def _fence(self, db, task_id, seq, robot_session, robot_id, payload, resources):
        """Recheck durable intent under the writer lock immediately before send."""
        self._progress.verify_uncertain_in_transaction(
            db, task_id, seq, robot_session, payload, resources)
        row = db.execute('SELECT robot_id FROM allocation_jobs WHERE task_id=?', (task_id,)).fetchone()
        if row is None or row[0] != robot_id:
            raise ProgressPublishingError('robot allocation changed before queueing')
        return _read_order(payload)

    def publish(self, task_id, seq, robot_session):
        """Explicitly send seq >= 2 once; confirmed result is broker ACK only."""
        _id(task_id, 'task id'); _id(robot_session, 'robot session')
        if type(seq) is not int or seq < 2:
            raise ValueError('next segment sequence must be at least 2')
        if self._publisher is None:
            raise OrderPublishingDisabled('simulation publishing is disabled')
        gate = self._progress._gate
        if not self._progress._enabled or not isinstance(gate, RecoveryGate):
            raise ProgressPublishingError('a simulation recovery gate is required')
        with self._lock:
            if self._running or self._used:
                raise ProgressPublishingError('publisher is already in use or consumed')
            self._running = True
        claimed = False
        try:
            robot_id, order, payload, resources = self._pending(task_id, seq)
            self._publisher.claim(self._owner)
            claimed = True
            with gate.serialized():
                self._publisher.validate(robot_id, copy.deepcopy(order), owner=self._owner)
                self._progress.mark_attempted(task_id, seq, robot_session, payload, resources)
                with self._lock:
                    self._used = True
                # A fresh gate in another process has a different local lock.
                # The SQLite writer lock fences its epoch change and any
                # occupancy update until this queue attempt has completed.
                with gate.transaction() as db:
                    current = self._fence(db, task_id, seq, robot_session, robot_id, payload, resources)
                    self._publisher.validate(robot_id, copy.deepcopy(current), owner=self._owner)
                    info = self._publisher.publish(robot_id, copy.deepcopy(current), owner=self._owner)
            info.wait_for_publish(timeout=self._timeout)
            if info.is_published() is not True:
                raise TimeoutError('broker acknowledgement not confirmed; outcome uncertain')
            self._progress.mark_broker_acked(task_id, seq, robot_session, payload, resources)
            return {'task_id': task_id, 'seq': seq, 'state': 'acked'}
        except BaseException:
            if claimed:
                # After any claimed failure the connection and proof must not
                # be reused. The gate locally blocks before attempting its DB
                # write, so an invalidation error still leaves this instance
                # blocked. A fresh challenge and a new client are required.
                with self._lock:
                    self._used = True
                try:
                    gate.invalidate(task_id)
                except Exception as exc:
                    raise ProgressPublishingError(
                        'publication failed and recovery proof could not be durably invalidated'
                    ) from exc
            raise
        finally:
            with self._lock:
                self._running = False
