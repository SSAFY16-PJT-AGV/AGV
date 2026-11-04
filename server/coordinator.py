"""Connect simulated task planning, durable allocation and guarded publishing.

This is not an operational safety controller. The caller must validate the
map, robot locations and availability and provide an isolated simulator. Old
database orders are never authorized by a newly created Coordinator.
"""
import copy
import hashlib
import json
import math
import threading

from server.dispatch import choose_robot
from server.allocation_store import AllocationConflict, InvalidAllocation, _validate_json
from server.order_publisher import OrderPublishingDisabled, validate_order
from server.routing import MapGraph
from server.segments import plan_segment


class CoordinatorError(RuntimeError):
    """A task cannot be safely prepared or published by this instance."""


class Coordinator:
    """One coordinator instance authorizes only tasks that it prepared."""

    def __init__(self, store, map_data, publisher=None, *, ack_timeout=3.0):
        if (isinstance(ack_timeout, bool) or not isinstance(ack_timeout, (int, float))
                or not 0 < ack_timeout <= 30 or not math.isfinite(ack_timeout)):
            raise ValueError('ack_timeout must be finite, greater than 0, and at most 30 seconds')
        self._map = copy.deepcopy(map_data)
        _validate_json(self._map)
        try:
            encoded_map = json.dumps(self._map, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False, separators=(',', ':')).encode('ascii')
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            raise ValueError('map must be canonical JSON') from error
        self._map_fingerprint = hashlib.sha256(encoded_map).hexdigest()
        self._graph = MapGraph(self._map)
        if '@' in self._graph.map_id or self._graph.map_id != self._graph.map_id.strip():
            raise ValueError('map_id cannot contain @ or leading/trailing whitespace')
        self._map_version = '{}@v{}'.format(self._graph.map_id, self._graph.version)
        self._store = store
        self._publisher = publisher
        self._ack_timeout = float(ack_timeout)
        self._lock = threading.Lock()
        self._prepared = {}
        self._prepare_claimed = False
        self._publisher_owner = object()

    def _release_preparation_claim(self):
        """Only use before any allocation commit, or after a known rollback."""
        self._publisher.release_claim(self._publisher_owner)
        with self._lock:
            self._prepare_claimed = False

    def prepare(self, task_id, pickup_node, candidates, action_at_end):
        """Persist one planned allocation, reserving every computed segment resource."""
        candidates_copy = copy.deepcopy(candidates)
        action_copy = copy.deepcopy(action_at_end)
        choice = choose_robot(self._graph, pickup_node, candidates_copy)
        if choice is None:
            raise CoordinatorError('no reachable available robot')
        route = list(choice.route.nodes)
        segment = plan_segment(self._map, route)
        expected_order = {'order_id': task_id, 'seq': 1, 'map_id': self._map_version,
                          'nodes': route, 'released_until': segment.released_until,
                          'action_at_end': action_copy}
        # Reject unusable simulated order format before persisting reservations.
        validate_order(choice.robot_id, expected_order)
        if self._publisher is None:
            raise OrderPublishingDisabled('simulation publishing is disabled')
        # One publisher handles at most one attempt; never prepare another task
        # that would later be impossible to publish with that publisher.
        with self._lock:
            if self._prepare_claimed:
                raise CoordinatorError('coordinator publisher already claimed')
            self._prepare_claimed = True
        try:
            self._publisher.claim(self._publisher_owner)
        except Exception:
            with self._lock:
                self._prepare_claimed = False
            raise
        try:
            self._publisher.validate(choice.robot_id, copy.deepcopy(expected_order),
                                     owner=self._publisher_owner)
        except Exception:
            self._release_preparation_claim()
            raise
        try:
            allocation = self._store.allocate(
                task_id, choice.robot_id, self._map_version, route, segment.released_until,
                segment.resources, action_copy,
                map_fingerprint=self._map_fingerprint,
            )
        except (AllocationConflict, InvalidAllocation):
            # A post-commit failure may look like an allocation error. Release
            # this publisher only when persisted reads prove no task allocation
            # or outbox order exists. Failed reads leave the claim held.
            try:
                no_allocation = self._store.get_allocation(task_id) is None
                no_order = self._store.get_order(task_id) is None
            except Exception:
                no_allocation = no_order = False
            if no_allocation and no_order:
                self._release_preparation_claim()
            raise
        expected_allocation = {'task_id': task_id, 'robot_id': choice.robot_id,
                               'map_id': self._map_version, 'nodes': route,
                               'released_until': segment.released_until,
                               'map_fingerprint': self._map_fingerprint}
        order_record = self._store.get_order(task_id)
        if (allocation != expected_allocation or not isinstance(order_record, dict)
                or order_record.get('state') != 'pending'):
            raise CoordinatorError('persisted allocation or order is inconsistent')
        try:
            stored_order = json.loads(order_record['payload'])
        except (ValueError, TypeError, KeyError) as error:
            raise CoordinatorError('persisted order is invalid') from error
        if stored_order != expected_order:
            raise CoordinatorError('persisted order does not match the allocation')
        with self._lock:
            self._prepared[task_id] = (copy.deepcopy(expected_allocation), order_record['payload'],
                                       tuple(segment.resources))
        return copy.deepcopy(allocation)

    def publish(self, task_id):
        """Send one locally prepared order; uncertain outcomes are never retried."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError('task_id must be a non-empty string')
        with self._lock:
            # One local attempt, even if readiness checking fails. A later
            # reconnect cannot silently revive a stale location assumption.
            prepared = self._prepared.pop(task_id, None)
        if prepared is None:
            raise CoordinatorError('order was not prepared by this coordinator')
        if self._publisher is None:
            raise OrderPublishingDisabled('simulation publishing is disabled')
        allocation = self._store.get_allocation(task_id)
        record = self._store.get_order(task_id)
        if (allocation != prepared[0] or not isinstance(record, dict)
                or record.get('state') != 'pending' or record.get('payload') != prepared[1]):
            raise CoordinatorError('stored order is not the prepared pending order')
        try:
            order = json.loads(record['payload'])
        except (TypeError, ValueError) as error:
            raise CoordinatorError('persisted order is invalid') from error
        robot_id = allocation['robot_id']
        self._publisher.validate(robot_id, copy.deepcopy(order), owner=self._publisher_owner)
        self._store.mark_attempted(task_id, prepared[1], prepared[0], prepared[2])
        info = self._publisher.publish(robot_id, copy.deepcopy(order), owner=self._publisher_owner)
        info.wait_for_publish(timeout=self._ack_timeout)
        if info.is_published() is not True:
            raise TimeoutError('MQTT broker acknowledgement not confirmed; outcome uncertain')
        self._store.mark_broker_acked(task_id, prepared[1], prepared[0], prepared[2])
        return {'task_id': task_id, 'state': 'acked'}
