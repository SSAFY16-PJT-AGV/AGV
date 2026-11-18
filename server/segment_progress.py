"""Conservative, simulation-only segment advancement in a single SQLite transaction.

This module does not authenticate robots or publish messages. A simulation
recovery gate must authorize each session and stop; physical clearance cannot
be inferred from the JSON event. Do not attach to real robot traffic.
"""
import copy
import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager

from server.allocation_store import (AllocationConflict, InvalidAllocation, _id, _validate_json,
                                     _stored_field_route, _verify_field_global)
from server.order_publisher import validate_order
from server.routing import MapGraph, UnsupportedRobotRoutingError
from server.segments import plan_segment
from server.route_occurrence import plan_occurrence_segment, visit_indices_from_orders


_EVENT_KEYS = frozenset(('type', 'event_id', 'robot_session', 'order_id', 'seq',
                         'map_id', 'stopped_at', 'cleared_resources'))


class SegmentOccupied(AllocationConflict):
    """A validated simulated progression cannot reserve its next resources."""


def _canonical(value):
    _validate_json(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('ascii')


def _read_order(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate order field')
            result[key] = value
        return result
    def bad_constant(value):
        raise ValueError('invalid order number')
    value = json.loads(payload, object_pairs_hook=pairs, parse_constant=bad_constant)
    _validate_json(value)
    return value


def _resources(value):
    if not isinstance(value, list):
        raise ValueError('cleared_resources must be a list')
    result = set()
    for item in value:
        if (not isinstance(item, list) or len(item) != 2
                or item[0] not in ('node', 'edge', 'zone')):
            raise ValueError('invalid cleared resource')
        _id(item[1], 'resource id')
        pair = (item[0], item[1])
        if pair in result:
            raise ValueError('duplicate cleared resource')
        result.add(pair)
    return result


class SegmentProgress:
    def __init__(self, store, map_data, *, simulation_enabled=False, recovery_gate=None, field_simulation=False):
        if type(simulation_enabled) is not bool or type(field_simulation) is not bool:
            raise ValueError('simulation_enabled must be boolean')
        if field_simulation and not simulation_enabled:
            raise ValueError('field simulation requires explicit simulation_enabled')
        self.store = store
        self._enabled = simulation_enabled
        self._gate = recovery_gate
        self._map = copy.deepcopy(map_data)
        self._fingerprint = hashlib.sha256(_canonical(self._map)).hexdigest()
        graph = MapGraph(self._map)
        # A restored allocation can bypass the route search performed during
        # initial assignment. Never advance it on a map whose vehicle-heading
        # and turn rules are not supported by the simulation planner.
        if graph.requires_robot_navigation and not field_simulation:
            raise UnsupportedRobotRoutingError(
                'robot heading and direction-aware turn constraints are required'
            )
        # The caller explicitly selected this mode; do not silently downgrade
        # it merely because a particular map has no navigation metadata.
        self._field_required = field_simulation
        if '@' in graph.map_id or graph.map_id != graph.map_id.strip():
            raise ValueError('invalid map id')
        self._map_id = '{}@v{}'.format(graph.map_id, graph.version)

    def initialize(self):
        with self.store._write() as db:
            db.execute('CREATE TABLE IF NOT EXISTS progress_orders ('
                       'task_id TEXT NOT NULL REFERENCES allocation_jobs(task_id), '
                       'seq INTEGER NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, '
                       'PRIMARY KEY(task_id, seq))')
            db.execute('CREATE TABLE IF NOT EXISTS progress_events ('
                       'robot_id TEXT NOT NULL, event_id TEXT NOT NULL, digest TEXT NOT NULL, '
                       'task_id TEXT NOT NULL REFERENCES allocation_jobs(task_id), '
                       'seq INTEGER NOT NULL, next_seq INTEGER, result TEXT NOT NULL, '
                       'PRIMARY KEY(robot_id, event_id))')

    def _authorize(self, db, task_id, session, seq, stopped_at):
        if not self._enabled or self._gate is None:
            raise RuntimeError('simulation progress is disabled or not reconciled')
        self._gate.require_reconciled(db, task_id, session,
                                      expected_seq=seq, expected_stopped_at=stopped_at)

    @contextmanager
    def _transaction(self):
        if not self._enabled or self._gate is None:
            raise RuntimeError('simulation progress is disabled or not reconciled')
        with self._gate.transaction() as db:
            self._check_database(db)
            verify_fleet = getattr(self.store, '_verify_field_fleet', None)
            if self._field_required and callable(verify_fleet):
                verify_fleet(db)
            yield db
            if self._field_required and callable(verify_fleet):
                verify_fleet(db)

    def _check_database(self, db):
        if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
            raise AllocationConflict('an active recovery transaction is required')
        paths = db.execute('PRAGMA database_list').fetchall()
        main_path = next((row[2] for row in paths if row[1] == 'main'), None)
        if (not main_path or os.path.normcase(os.path.realpath(main_path))
                != os.path.normcase(os.path.realpath(self.store.path))):
            raise AllocationConflict('recovery and allocation must use the same database')

    def _job(self, db, task_id, robot_id=None):
        row = db.execute('SELECT t.status,j.robot_id,j.map_id,j.nodes_json,j.released_until, '
                         "(SELECT value FROM allocation_meta WHERE key='map_fingerprint'), "
                         "(SELECT value FROM allocation_meta WHERE key='active_map') "
                         'FROM tasks t JOIN allocation_jobs j ON j.task_id=t.task_id '
                         'WHERE t.task_id=?', (task_id,)).fetchone()
        if (row is None or row[0] != 'ASSIGNED' or row[2] != self._map_id
                or row[5] != self._fingerprint or row[6] != self._map_id
                or not row[1].startswith('sim-') or (robot_id is not None and row[1] != robot_id)):
            raise AllocationConflict('task, robot, map or fingerprint does not match')
        try:
            path = json.loads(row[3])
            if not isinstance(path, list) or len(path) < 2:
                raise ValueError('invalid route')
            repeated = len(set(path)) != len(path)
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='occurrence_allocations'").fetchone()
            occurrence = (db.execute('SELECT current_index FROM occurrence_allocations WHERE task_id=?',
                                     (task_id,)).fetchone() if table else None)
            if repeated:
                if (occurrence is None or type(occurrence[0]) is not int
                        or not 1 <= occurrence[0] < len(path) or path[occurrence[0]] != row[4]):
                    raise ValueError('repeated route occurrence is missing or inconsistent')
            elif occurrence is not None:
                raise ValueError('unexpected occurrence for non-repeated route')
        except (ValueError, TypeError) as error:
            raise AllocationConflict('invalid stored route') from error
        field = _stored_field_route(db, task_id, row[2], path, self._fingerprint)
        field_mode = db.execute("SELECT value FROM allocation_meta WHERE key='field_route_mode'").fetchone()
        if self._field_required and field_mode != ('required',):
            raise AllocationConflict('field route mode is missing or invalid')
        if not self._field_required and field_mode is not None:
            raise AllocationConflict('unexpected field route mode')
        if self._field_required and field is None:
            raise AllocationConflict('field route provenance is required')
        if not self._field_required and field is not None:
            raise AllocationConflict('unexpected field route provenance')
        return row[1], path, row[4]

    def _segment(self, path, released_until):
        if len(set(path)) != len(path):
            raise AllocationConflict('repeated route requires its stored occurrence index')
        offset = 0
        while offset < len(path) - 1:
            segment = plan_segment(self._map, path[offset:])
            if segment.released_until == released_until:
                return segment
            offset = path.index(segment.released_until)
        raise AllocationConflict('released_until is not a route waiting point')

    def _latest(self, db, task_id, robot_id, path, current_stop):
        initial = db.execute('SELECT payload,state FROM allocation_outbox WHERE task_id=?', (task_id,)).fetchone()
        if initial is None or initial[1] not in ('uncertain', 'acked'):
            raise AllocationConflict('initial order has not been attempted')
        first = _read_order(initial[0])
        validate_order(robot_id, first)
        if self._field_required:
            from server.field_robot_contract import validate_field_robot_order
            validate_field_robot_order(first)
        if len(set(path)) != len(path):
            return self._latest_occurrence(db, task_id, robot_id, path, current_stop, initial, first)
        if (first['order_id'] != task_id or first['seq'] != 1 or first['map_id'] != self._map_id
                or first['nodes'] != path
                or first['released_until'] != plan_segment(self._map, path).released_until):
            raise AllocationConflict('initial order disagrees with allocation')
        rows = db.execute('SELECT seq,payload,state FROM progress_orders WHERE task_id=? ORDER BY seq',
                          (task_id,)).fetchall()
        if not rows:
            if first['released_until'] != current_stop:
                raise AllocationConflict('initial released node differs from allocation')
            return 1, initial[0], initial[1], first
        if rows[0] != (1, initial[0], initial[1]):
            raise AllocationConflict('initial order history disagrees')
        previous_stop = None
        for index, (seq, payload, state) in enumerate(rows, start=1):
            if seq != index or state not in ('pending', 'uncertain', 'acked'):
                raise AllocationConflict('order history is invalid')
            if index < len(rows):
                verified_event = db.execute(
                    "SELECT 1 FROM progress_events WHERE task_id=? AND robot_id=? AND seq=? "
                    "AND next_seq=? AND result='advanced'", (task_id, robot_id, seq, seq + 1)
                ).fetchone()
                if state not in ('uncertain', 'acked') or verified_event is None:
                    raise AllocationConflict('previous order has no verified progression event')
            order = _read_order(payload)
            validate_order(robot_id, order)
            if self._field_required:
                from server.field_robot_contract import validate_field_robot_order
                validate_field_robot_order(order)
            if (order['order_id'] != task_id or order['seq'] != seq
                    or order['map_id'] != self._map_id or order['nodes'] != path
                    or order['action_at_end'] != first['action_at_end']):
                raise AllocationConflict('order history disagrees with route')
            expected = plan_segment(self._map, path[0 if previous_stop is None else path.index(previous_stop):])
            if order['released_until'] != expected.released_until:
                raise AllocationConflict('order history skips a waiting point')
            previous_stop = order['released_until']
        seq, payload, state = rows[-1]
        if previous_stop != current_stop:
            raise AllocationConflict('current released node disagrees with order history')
        return seq, payload, state, _read_order(payload)

    def _latest_occurrence(self, db, task_id, robot_id, path, stop, initial, first):
        rows = db.execute('SELECT seq,payload,state FROM progress_orders WHERE task_id=? ORDER BY seq',
                          (task_id,)).fetchall()
        if rows:
            if rows[0] != (1, initial[0], initial[1]):
                raise AllocationConflict('initial order histories disagree')
        else:
            rows = [(1, initial[0], initial[1])]
        orders = []
        for index, (seq, payload, state) in enumerate(rows, 1):
            if seq != index or state not in ('pending', 'uncertain', 'acked'):
                raise AllocationConflict('order history is invalid')
            if index < len(rows):
                verified = db.execute("SELECT 1 FROM progress_events WHERE task_id=? AND robot_id=? AND seq=? "
                                      "AND next_seq=? AND result='advanced'", (task_id, robot_id, seq, seq + 1)).fetchone()
                if state not in ('uncertain', 'acked') or verified is None:
                    raise AllocationConflict('previous order has no verified progression event')
            order = _read_order(payload)
            validate_order(robot_id, order)
            if (order['order_id'] != task_id or order['map_id'] != self._map_id or order['nodes'] != path):
                raise AllocationConflict('order history disagrees with route')
            orders.append(order)
        try:
            indices = visit_indices_from_orders(orders)
            previous = 0
            for index in indices:
                segment = plan_occurrence_segment(self._map, path, previous)
                if segment.end_index != index:
                    raise ValueError('order skips a waiting occurrence')
                previous = index
        except (ValueError, TypeError) as exc:
            raise AllocationConflict('occurrence order history is invalid') from exc
        current = db.execute('SELECT current_index FROM occurrence_allocations WHERE task_id=?',
                             (task_id,)).fetchone()
        if current != (indices[-1],) or path[indices[-1]] != stop:
            raise AllocationConflict('current visit index disagrees with order history')
        return rows[-1][0], rows[-1][1], rows[-1][2], orders[-1]

    def _segment_for_task(self, db, task_id, path, stop):
        if len(set(path)) == len(path):
            return self._segment(path, stop)
        row = db.execute('SELECT current_index FROM occurrence_allocations WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise AllocationConflict('repeated route occurrence is unavailable')
        current = row[0]
        rows = db.execute('SELECT payload FROM progress_orders WHERE task_id=? ORDER BY seq', (task_id,)).fetchall()
        if not rows:
            payload = db.execute('SELECT payload FROM allocation_outbox WHERE task_id=?', (task_id,)).fetchone()
            rows = [payload] if payload else []
        try:
            indices = visit_indices_from_orders([_read_order(item[0]) for item in rows])
            if indices[-1] != current or path[current] != stop:
                raise ValueError('stored occurrence differs from history')
            start = 0 if len(indices) == 1 else indices[-2]
            segment = plan_occurrence_segment(self._map, path, start)
            if segment.end_index != current:
                raise ValueError('stored occurrence is not the next waiting point')
            return segment
        except (ValueError, TypeError, IndexError) as exc:
            raise AllocationConflict('repeated route occurrence is invalid') from exc

    @staticmethod
    def _occupancy(db, task_id):
        return set(db.execute('SELECT kind,resource_id FROM allocation_occupancy WHERE task_id=?',
                              (task_id,)).fetchall())

    def advance(self, robot_id, event, *, retained=False):
        if type(retained) is not bool or retained:
            raise ValueError('retained events are not accepted')
        _id(robot_id, 'robot id', True)
        if not robot_id.startswith('sim-'):
            raise ValueError('only simulation robots are accepted')
        event = copy.deepcopy(event)
        if not isinstance(event, dict) or set(event) != _EVENT_KEYS or event.get('type') != 'rear_clear':
            raise ValueError('invalid rear_clear event')
        for key in ('event_id', 'robot_session', 'order_id', 'map_id', 'stopped_at'):
            _id(event[key], key)
        if type(event['seq']) is not int or event['seq'] < 1:
            raise ValueError('invalid sequence')
        cleared = _resources(event['cleared_resources'])
        digest = hashlib.sha256(_canonical(event)).hexdigest()
        task_id, seq = event['order_id'], event['seq']
        with self._transaction() as db:
            _verify_field_global(db, field_required=self._field_required)
            self._authorize(db, task_id, event['robot_session'], seq, event['stopped_at'])
            existing = db.execute('SELECT digest,task_id,seq,next_seq,result FROM progress_events '
                                  'WHERE robot_id=? AND event_id=?', (robot_id, event['event_id'])).fetchone()
            if existing:
                if existing[0] != digest or existing[1] != task_id or existing[2] != seq:
                    raise AllocationConflict('event id was reused with different content')
                payload = None
                if existing[3] is not None:
                    row = db.execute('SELECT payload FROM progress_orders WHERE task_id=? AND seq=?',
                                     (task_id, existing[3])).fetchone()
                    if row is None:
                        raise AllocationConflict('duplicate event has no order history')
                    payload = row[0]
                return {'status': 'duplicate', 'task_id': task_id, 'next_seq': existing[3], 'payload': payload}
            robot, path, current_stop = self._job(db, task_id, robot_id)
            current_seq, payload, state, order = self._latest(db, task_id, robot, path, current_stop)
            if (state not in ('acked', 'uncertain') or seq != current_seq or event['map_id'] != self._map_id
                    or event['stopped_at'] != current_stop):
                raise AllocationConflict('event does not match the acknowledged current segment')
            current_segment = self._segment_for_task(db, task_id, path, current_stop)
            current = set(current_segment.resources)
            if self._occupancy(db, task_id) != current:
                raise AllocationConflict('current occupancy differs from the planned segment')
            repeated = len(set(path)) != len(path)
            end_index = current_segment.end_index if repeated else path.index(current_stop)
            next_segment = (None if end_index == len(path) - 1 else
                            plan_occurrence_segment(self._map, path, end_index) if repeated else
                            plan_segment(self._map, path[end_index:]))
            keep = {('node', current_stop)} if next_segment is None else set(next_segment.resources)
            removable = current - keep
            if cleared != removable:
                raise AllocationConflict('clearance must exactly cover the previous resources to release')
            # The fresh simulation store also represents not-yet-assigned
            # robots as idle node occupants. Check those under the same write
            # transaction before changing the current segment.
            from server.sim_handoff_store import SimHandoffStore, SimResourceBusy
            if type(self.store) is SimHandoffStore:
                try:
                    self.store._check_resource_availability(db, keep - current, ignore_task=task_id)
                except SimResourceBusy as error:
                    raise SegmentOccupied('next segment is occupied') from error
            for kind, resource_id in sorted(removable):
                deleted = db.execute('DELETE FROM allocation_occupancy WHERE task_id=? AND kind=? AND resource_id=?',
                                     (task_id, kind, resource_id))
                if deleted.rowcount != 1:
                    raise AllocationConflict('resource ownership changed')
            try:
                for kind, resource_id in sorted(keep - current):
                    db.execute('INSERT INTO allocation_occupancy(kind,resource_id,task_id) VALUES(?,?,?)',
                               (kind, resource_id, task_id))
            except sqlite3.IntegrityError as error:
                if getattr(error, 'sqlite_errorcode', None) in (
                        sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY, sqlite3.SQLITE_CONSTRAINT_UNIQUE):
                    raise SegmentOccupied('next segment is occupied') from error
                raise AllocationConflict('next segment reservation failed') from error
            if not db.execute('SELECT 1 FROM progress_orders WHERE task_id=? AND seq=1', (task_id,)).fetchone():
                initial = db.execute('SELECT payload,state FROM allocation_outbox WHERE task_id=?', (task_id,)).fetchone()
                db.execute('INSERT INTO progress_orders(task_id,seq,payload,state) VALUES(?,?,?,?)',
                           (task_id, 1, initial[0], initial[1]))
            next_seq, next_payload, status = None, None, 'finished'
            if next_segment is not None:
                next_seq, status = seq + 1, 'advanced'
                next_order = dict(order, seq=next_seq, released_until=next_segment.released_until)
                validate_order(robot_id, next_order)
                if self._field_required:
                    from server.field_robot_contract import validate_field_robot_order
                    validate_field_robot_order(next_order)
                next_payload = json.dumps(next_order, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                db.execute('INSERT INTO progress_orders(task_id,seq,payload,state) VALUES(?,?,?,?)',
                           (task_id, next_seq, next_payload, 'pending'))
                db.execute('UPDATE allocation_jobs SET released_until=? WHERE task_id=?',
                           (next_segment.released_until, task_id))
                if repeated:
                    updated = db.execute('UPDATE occurrence_allocations SET current_index=? '
                                         'WHERE task_id=? AND current_index=?',
                                         (next_segment.end_index, task_id, end_index))
                    if updated.rowcount != 1:
                        raise AllocationConflict('visit index changed during advancement')
            db.execute('INSERT INTO progress_events VALUES(?,?,?,?,?,?,?)',
                       (robot_id, event['event_id'], digest, task_id, seq, next_seq, status))
            _verify_field_global(db, field_required=self._field_required)
            return {'status': status, 'task_id': task_id, 'next_seq': next_seq, 'payload': next_payload}

    def get_order(self, task_id, seq):
        _id(task_id, 'task id')
        if type(seq) is not int or seq < 1:
            raise ValueError('invalid sequence')
        with closing(self.store._connect()) as db:
            row = db.execute('SELECT payload,state FROM progress_orders WHERE task_id=? AND seq=?',
                             (task_id, seq)).fetchone()
        return None if row is None else {'payload': row[0], 'state': row[1]}

    def _verify_transition(self, db, task_id, seq, robot_session, expected_payload, expected_resources, source):
        _id(task_id, 'task id'); _id(robot_session, 'robot session')
        if type(seq) is not int or seq < 2 or not isinstance(expected_payload, str):
            raise ValueError('invalid order transition')
        expected = set(copy.deepcopy(expected_resources))
        self._check_database(db)
        verify_fleet = getattr(self.store, '_verify_field_fleet', None)
        if self._field_required and callable(verify_fleet):
            verify_fleet(db)
        _verify_field_global(db, field_required=self._field_required)
        robot, path, stop = self._job(db, task_id)
        current_seq, payload, state, order = self._latest(db, task_id, robot, path, stop)
        if current_seq != seq or payload != expected_payload or state != source:
            raise AllocationConflict('order changed before transition')
        current = set(self._segment_for_task(db, task_id, path, stop).resources)
        if expected != current or self._occupancy(db, task_id) != current:
            raise AllocationConflict('reserved resources changed before transition')
        prior_row = db.execute('SELECT payload FROM progress_orders WHERE task_id=? AND seq=?',
                               (task_id, seq - 1)).fetchone()
        if prior_row is None:
            raise AllocationConflict('previous order missing')
        previous = _read_order(prior_row[0])
        self._authorize(db, task_id, robot_session, seq - 1, previous['released_until'])

    def verify_uncertain_in_transaction(self, db, task_id, seq, robot_session, expected_payload, expected_resources):
        """Recheck an uncertain order in the caller's active gate transaction.

        This does not mutate state or publish. The caller must hold this same
        gate transaction through its single, nonblocking MQTT queue operation.
        """
        self._verify_transition(db, task_id, seq, robot_session, expected_payload,
                                expected_resources, 'uncertain')

    def _transition(self, task_id, seq, robot_session, expected_payload, expected_resources, source, target):
        with self._transaction() as db:
            self._verify_transition(db, task_id, seq, robot_session, expected_payload,
                                    expected_resources, source)
            updated = db.execute('UPDATE progress_orders SET state=? WHERE task_id=? AND seq=? AND state=?',
                                 (target, task_id, seq, source))
            if updated.rowcount != 1:
                raise AllocationConflict('order state changed')
            _verify_field_global(db, field_required=self._field_required)

    def mark_attempted(self, task_id, seq, robot_session, expected_payload, expected_resources):
        self._transition(task_id, seq, robot_session, expected_payload, expected_resources, 'pending', 'uncertain')

    def mark_broker_acked(self, task_id, seq, robot_session, expected_payload, expected_resources):
        self._transition(task_id, seq, robot_session, expected_payload, expected_resources, 'uncertain', 'acked')
