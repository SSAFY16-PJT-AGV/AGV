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

from server.allocation_store import AllocationConflict, InvalidAllocation, _id, _validate_json
from server.order_publisher import validate_order
from server.routing import MapGraph
from server.segments import plan_segment


_EVENT_KEYS = frozenset(('type', 'event_id', 'robot_session', 'order_id', 'seq',
                         'map_id', 'stopped_at', 'cleared_resources'))


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
    def __init__(self, store, map_data, *, simulation_enabled=False, recovery_gate=None):
        if type(simulation_enabled) is not bool:
            raise ValueError('simulation_enabled must be boolean')
        self.store = store
        self._enabled = simulation_enabled
        self._gate = recovery_gate
        self._map = copy.deepcopy(map_data)
        self._fingerprint = hashlib.sha256(_canonical(self._map)).hexdigest()
        graph = MapGraph(self._map)
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
            paths = db.execute('PRAGMA database_list').fetchall()
            main_path = next((row[2] for row in paths if row[1] == 'main'), None)
            if (not main_path or os.path.normcase(os.path.realpath(main_path))
                    != os.path.normcase(os.path.realpath(self.store.path))):
                raise AllocationConflict('recovery and allocation must use the same database')
            yield db

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
            if not isinstance(path, list) or len(path) < 2 or len(set(path)) != len(path):
                raise ValueError('invalid route')
        except (ValueError, TypeError) as error:
            raise AllocationConflict('invalid stored route') from error
        return row[1], path, row[4]

    def _segment(self, path, released_until):
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
            if index < len(rows) and state != 'acked':
                verified_event = db.execute('SELECT 1 FROM progress_events WHERE task_id=? AND seq=?',
                                            (task_id, seq)).fetchone()
                if state != 'uncertain' or verified_event is None:
                    raise AllocationConflict('previous order has no verified progression event')
            order = _read_order(payload)
            validate_order(robot_id, order)
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
            current = set(self._segment(path, current_stop).resources)
            if self._occupancy(db, task_id) != current:
                raise AllocationConflict('current occupancy differs from the planned segment')
            end_index = path.index(current_stop)
            next_segment = None if end_index == len(path) - 1 else plan_segment(self._map, path[end_index:])
            keep = {('node', current_stop)} if next_segment is None else set(next_segment.resources)
            removable = current - keep
            if cleared != removable:
                raise AllocationConflict('clearance must exactly cover the previous resources to release')
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
                raise AllocationConflict('next segment is occupied') from error
            if not db.execute('SELECT 1 FROM progress_orders WHERE task_id=? AND seq=1', (task_id,)).fetchone():
                initial = db.execute('SELECT payload,state FROM allocation_outbox WHERE task_id=?', (task_id,)).fetchone()
                db.execute('INSERT INTO progress_orders(task_id,seq,payload,state) VALUES(?,?,?,?)',
                           (task_id, 1, initial[0], initial[1]))
            next_seq, next_payload, status = None, None, 'finished'
            if next_segment is not None:
                next_seq, status = seq + 1, 'advanced'
                next_order = dict(order, seq=next_seq, released_until=next_segment.released_until)
                validate_order(robot_id, next_order)
                next_payload = json.dumps(next_order, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                db.execute('INSERT INTO progress_orders(task_id,seq,payload,state) VALUES(?,?,?,?)',
                           (task_id, next_seq, next_payload, 'pending'))
                db.execute('UPDATE allocation_jobs SET released_until=? WHERE task_id=?',
                           (next_segment.released_until, task_id))
            db.execute('INSERT INTO progress_events VALUES(?,?,?,?,?,?,?)',
                       (robot_id, event['event_id'], digest, task_id, seq, next_seq, status))
            return {'status': status, 'task_id': task_id, 'next_seq': next_seq, 'payload': next_payload}

    def get_order(self, task_id, seq):
        _id(task_id, 'task id')
        if type(seq) is not int or seq < 1:
            raise ValueError('invalid sequence')
        with closing(self.store._connect()) as db:
            row = db.execute('SELECT payload,state FROM progress_orders WHERE task_id=? AND seq=?',
                             (task_id, seq)).fetchone()
        return None if row is None else {'payload': row[0], 'state': row[1]}

    def _transition(self, task_id, seq, robot_session, expected_payload, expected_resources, source, target):
        _id(task_id, 'task id'); _id(robot_session, 'robot session')
        if type(seq) is not int or seq < 2 or not isinstance(expected_payload, str):
            raise ValueError('invalid order transition')
        expected = set(copy.deepcopy(expected_resources))
        with self._transaction() as db:
            robot, path, stop = self._job(db, task_id)
            current_seq, payload, state, order = self._latest(db, task_id, robot, path, stop)
            if current_seq != seq or payload != expected_payload or state != source:
                raise AllocationConflict('order changed before transition')
            current = set(self._segment(path, stop).resources)
            if expected != current or self._occupancy(db, task_id) != current:
                raise AllocationConflict('reserved resources changed before transition')
            prior_row = db.execute('SELECT payload FROM progress_orders WHERE task_id=? AND seq=?',
                                   (task_id, seq - 1)).fetchone()
            if prior_row is None:
                raise AllocationConflict('previous order missing')
            previous = _read_order(prior_row[0])
            self._authorize(db, task_id, robot_session, seq - 1, previous['released_until'])
            updated = db.execute('UPDATE progress_orders SET state=? WHERE task_id=? AND seq=? AND state=?',
                                 (target, task_id, seq, source))
            if updated.rowcount != 1:
                raise AllocationConflict('order state changed')

    def mark_attempted(self, task_id, seq, robot_session, expected_payload, expected_resources):
        self._transition(task_id, seq, robot_session, expected_payload, expected_resources, 'pending', 'uncertain')

    def mark_broker_acked(self, task_id, seq, robot_session, expected_payload, expected_resources):
        self._transition(task_id, seq, robot_session, expected_payload, expected_resources, 'uncertain', 'acked')
