"""Durable event inbox for explicitly isolated simulations; no MQTT or hardware."""
import hashlib
import json
import math
import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from types import MappingProxyType

from server.allocation_store import AllocationConflict, _id, _validate_json
from server.segment_progress import _EVENT_KEYS, _canonical, _resources, _read_order
from server.order_publisher import validate_order
from server.segments import plan_segment

MAX_EVENT_BYTES = 256 * 1024
_locks_guard = threading.Lock()
_locks = {}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('invalid JSON constant')


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('non-finite JSON number')
    return number


def _event(payload):
    if isinstance(payload, bytes):
        if len(payload) > MAX_EVENT_BYTES:
            raise ValueError('event exceeds maximum size')
        event = json.loads(payload.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=_constant, parse_float=_finite)
    elif isinstance(payload, dict):
        event = payload
    else:
        raise ValueError('event must be UTF-8 bytes or an object')
    _validate_json(event)
    if not isinstance(event, dict) or set(event) != _EVENT_KEYS or event.get('type') != 'rear_clear':
        raise ValueError('invalid rear_clear event')
    for key in ('event_id', 'robot_session', 'order_id', 'map_id', 'stopped_at'):
        _id(event[key], key)
    if type(event['seq']) is not int or event['seq'] < 1:
        raise ValueError('invalid sequence')
    _resources(event['cleared_resources'])
    encoded = _canonical(event)
    if len(encoded) > MAX_EVENT_BYTES:
        raise ValueError('canonical event exceeds maximum size')
    return json.loads(encoded), encoded.decode('ascii'), hashlib.sha256(encoded).hexdigest()


class SimEventInbox:
    def __init__(self, db_path, *, robot_tasks, simulation_enabled=False):
        if simulation_enabled is not True:
            raise ValueError('isolated simulation must be explicitly enabled')
        self.path = os.path.realpath(os.fspath(db_path))
        if not os.path.isfile(self.path):
            raise ValueError('existing allocation database required')
        if not isinstance(robot_tasks, dict) or not robot_tasks:
            raise ValueError('explicit robot-to-task mapping required')
        mapping, seen = {}, set()
        for robot, tasks in robot_tasks.items():
            _id(robot, 'robot id', True)
            if not robot.startswith('sim-'):
                raise ValueError('only simulated robots accepted')
            if not isinstance(tasks, (list, tuple, set, frozenset)) or not tasks:
                raise ValueError('nonempty task list required')
            for task in tasks:
                _id(task, 'task id')
                if task in seen:
                    raise ValueError('task belongs to multiple mappings')
                seen.add(task)
            mapping[robot] = frozenset(tasks)
        self.robot_tasks = MappingProxyType(mapping)
        with _locks_guard:
            self._lock = _locks.setdefault(os.path.normcase(self.path), threading.RLock())

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('PRAGMA synchronous = FULL')
        return db

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def initialize(self):
        with self._write() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='allocation_jobs'").fetchone():
                raise ValueError('allocation database must be initialized first')
            db.execute('CREATE TABLE IF NOT EXISTS sim_event_inbox ('
                       'robot_id TEXT NOT NULL, event_id TEXT NOT NULL, task_id TEXT NOT NULL, '
                       'robot_session TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, '
                       'digest TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN '
                       "('pending','applied','rejected')), result TEXT, PRIMARY KEY(robot_id,event_id))")

    def _robot(self, robot):
        _id(robot, 'robot id', True)
        if robot not in self.robot_tasks:
            raise ValueError('robot is not explicitly allowed')

    def store(self, robot_id, payload):
        self._robot(robot_id)
        event, canonical, digest = _event(payload)
        if event['order_id'] not in self.robot_tasks[robot_id]:
            raise ValueError('event task does not match robot mapping')
        with self._write() as db:
            existing = db.execute('SELECT payload,digest,state,task_id,robot_session,seq FROM sim_event_inbox WHERE robot_id=? AND event_id=?',
                                  (robot_id, event['event_id'])).fetchone()
            if existing:
                if (existing[0] != canonical or existing[1] != digest
                        or tuple(existing[3:6]) != (event['order_id'], event['robot_session'], event['seq'])):
                    raise AllocationConflict('event id reused with different content or corrupt metadata')
                return {'status': 'duplicate', 'event_id': event['event_id'], 'state': existing[2]}
            db.execute('INSERT INTO sim_event_inbox(robot_id,event_id,task_id,robot_session,seq,payload,digest,state) '
                       "VALUES(?,?,?,?,?,?,?,'pending')", (robot_id, event['event_id'], event['order_id'],
                       event['robot_session'], event['seq'], canonical, digest))
        return {'status': 'stored', 'event_id': event['event_id'], 'state': 'pending'}

    def get(self, robot_id, event_id):
        self._robot(robot_id)
        _id(event_id, 'event id')
        with closing(self._connect()) as db:
            row = db.execute('SELECT payload,digest,state,result,task_id,robot_session,seq FROM sim_event_inbox WHERE robot_id=? AND event_id=?',
                             (robot_id, event_id)).fetchone()
        if row is None:
            return None
        event, canonical, digest = _event(row[0].encode('ascii'))
        if (canonical != row[0] or digest != row[1] or event['event_id'] != event_id
                or event['order_id'] not in self.robot_tasks[robot_id]
                or (event['order_id'], event['robot_session'], event['seq']) != tuple(row[4:7])):
            raise AllocationConflict('stored inbox event is inconsistent')
        return {'event': event, 'state': row[2], 'result': json.loads(row[3]) if row[3] else None}

    def list_pending(self):
        # Reopen after a crash to discover events requiring explicit retry.
        with self._lock:
            with closing(self._connect()) as db:
                rows = db.execute("SELECT robot_id,event_id FROM sim_event_inbox WHERE state='pending' "
                                  'ORDER BY robot_id,event_id').fetchall()
            pending = []
            for robot_id, event_id in rows:
                if robot_id in self.robot_tasks:
                    item = self.get(robot_id, event_id)
                    if item is not None and item['state'] == 'pending':
                        pending.append({'robot_id': robot_id, 'event_id': event_id, 'event': item['event']})
            return pending

    def _mark_applied(self, robot_id, event_id, event, result):
        _, payload, digest = _event(event)
        result_json = _canonical(result).decode('ascii')
        with self._write() as db:
            row = db.execute('SELECT payload,digest,state,result,task_id,robot_session,seq FROM sim_event_inbox WHERE robot_id=? AND event_id=?',
                             (robot_id, event_id)).fetchone()
            if (row is None or row[0] != payload or row[1] != digest
                    or tuple(row[4:7]) != (event['order_id'], event['robot_session'], event['seq'])):
                raise AllocationConflict('inbox event changed before completion')
            if row[2] == 'applied':
                return json.loads(row[3])
            if row[2] != 'pending':
                raise AllocationConflict('inbox event is not pending')
            db.execute("UPDATE sim_event_inbox SET state='applied',result=? WHERE robot_id=? AND event_id=?",
                       (result_json, robot_id, event_id))
        return result

    def _recover_applied(self, robot_id, event_id, event, progress):
        # This is bookkeeping only: a prior progress transaction already
        # committed the event and its effects. No gate or movement is granted.
        _, payload, digest = _event(event)
        with self._write() as db:
            inbox = db.execute('SELECT payload,digest,state,task_id,robot_session,seq,result '
                               'FROM sim_event_inbox WHERE robot_id=? AND event_id=?',
                               (robot_id, event_id)).fetchone()
            if (inbox is None or inbox[0] != payload or inbox[1] != digest
                    or tuple(inbox[3:6]) != (event['order_id'], event['robot_session'], event['seq'])):
                raise AllocationConflict('inbox record changed')
            if inbox[2] == 'applied':
                return json.loads(inbox[6])
            if inbox[2] != 'pending':
                raise AllocationConflict('event is not pending')
            journal = db.execute('SELECT digest,task_id,seq,next_seq,result FROM progress_events '
                                 'WHERE robot_id=? AND event_id=?', (robot_id, event_id)).fetchone()
            if journal is None:
                return None
            if (journal[0] != digest or journal[1] != event['order_id'] or journal[2] != event['seq']
                    or journal[4] not in ('advanced', 'finished')):
                raise AllocationConflict('progress journal disagrees with inbox')
            job = db.execute('SELECT robot_id,map_id,nodes_json FROM allocation_jobs WHERE task_id=?',
                             (event['order_id'],)).fetchone()
            fingerprint = db.execute("SELECT value FROM allocation_meta WHERE key='map_fingerprint'").fetchone()
            if (not job or job[0] != robot_id or job[1] != event['map_id']
                    or job[1] != progress._map_id or not fingerprint or fingerprint[0] != progress._fingerprint):
                raise AllocationConflict('allocation or map differs from progress journal')
            path = json.loads(job[2])
            initial_row = db.execute('SELECT payload FROM allocation_outbox WHERE task_id=?',
                                     (event['order_id'],)).fetchone()
            previous_row = initial_row if event['seq'] == 1 else db.execute(
                'SELECT payload FROM progress_orders WHERE task_id=? AND seq=?',
                (event['order_id'], event['seq'])).fetchone()
            if not initial_row or not previous_row:
                raise AllocationConflict('previous order is missing')
            first, previous = _read_order(initial_row[0]), _read_order(previous_row[0])
            validate_order(robot_id, first)
            validate_order(robot_id, previous)
            if (first['seq'] != 1 or previous['seq'] != event['seq']
                    or previous['order_id'] != event['order_id'] or previous['map_id'] != event['map_id']
                    or previous['nodes'] != path or previous['released_until'] != event['stopped_at']
                    or {k:v for k,v in previous.items() if k not in ('seq','released_until')}
                    != {k:v for k,v in first.items() if k not in ('seq','released_until')}):
                raise AllocationConflict('previous order disagrees with event')
            next_payload = None
            if journal[4] == 'advanced':
                if journal[3] != event['seq'] + 1:
                    raise AllocationConflict('invalid next sequence')
                row = db.execute('SELECT payload,state FROM progress_orders WHERE task_id=? AND seq=?',
                                 (event['order_id'], journal[3])).fetchone()
                if not row or row[1] not in ('pending','uncertain','acked'):
                    raise AllocationConflict('next order is missing or invalid')
                next_order = _read_order(row[0])
                validate_order(robot_id, next_order)
                try:
                    expected_stop = plan_segment(progress._map, path[path.index(event['stopped_at']):]).released_until
                except (ValueError, KeyError, TypeError) as exc:
                    raise AllocationConflict('invalid next segment') from exc
                if (next_order['seq'] != journal[3] or next_order['released_until'] != expected_stop
                        or {k:v for k,v in next_order.items() if k not in ('seq','released_until')}
                        != {k:v for k,v in first.items() if k not in ('seq','released_until')}):
                    raise AllocationConflict('next order does not match progress journal')
                next_payload = row[0]
            elif journal[3] is not None or not path or event['stopped_at'] != path[-1]:
                raise AllocationConflict('invalid final progress journal')
            result = {'status':'duplicate', 'task_id':event['order_id'],
                      'next_seq':journal[3], 'payload':next_payload}
            db.execute("UPDATE sim_event_inbox SET state='applied',result=? WHERE robot_id=? AND event_id=?",
                       (_canonical(result).decode('ascii'), robot_id, event_id))
            return result

    def retry(self, robot_id, event_id, progress):
        if os.path.normcase(os.path.realpath(os.fspath(progress.store.path))) != os.path.normcase(self.path):
            raise ValueError('progress must use the same allocation database')
        with self._lock:
            record = self.get(robot_id, event_id)
            if record is None:
                raise ValueError('unknown event')
            if record['state'] == 'applied':
                return record['result']
            if record['state'] != 'pending':
                raise AllocationConflict('event is not pending')
            event = record['event']
            recovered = self._recover_applied(robot_id, event_id, event, progress)
            if recovered is not None:
                return recovered
            result = progress.advance(robot_id, event, retained=False)
            if not isinstance(result, dict) or result.get('status') not in ('advanced', 'duplicate', 'finished'):
                raise AllocationConflict('progress did not confirm event application')
            return self._mark_applied(robot_id, event_id, event, result)
