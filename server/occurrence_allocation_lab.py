"""Isolated SQLite experiment for occurrence-indexed simulated reservations.

No MQTT publishing, physical motion, robot identity authentication, fresh stop
proof, physical rear clearance, or integration with the existing allocation
store is provided. Incoming events and broker ACKs are *synthetic test inputs*.
The existing production/simulation gates must remain closed for these results.
"""
from contextlib import closing, contextmanager
import copy
import hashlib
import json
import os
import re
import secrets
import sqlite3

from server.field_map_planner import plan_field_route
from server.occurrence_journal import _decode, _encode, _id
from server.route_occurrence import occurrence_clearance, plan_occurrence_segment, visit_indices_from_orders


class LabConflict(ValueError):
    """An isolated experimental transaction is stale or cannot be verified."""


_SCHEMA = {
    'lab_meta': 'CREATE TABLE lab_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)',
    'lab_jobs': ('CREATE TABLE lab_jobs (task_id TEXT PRIMARY KEY NOT NULL, robot_id TEXT NOT NULL UNIQUE, '
                 'session_id TEXT NOT NULL, map_id TEXT NOT NULL, fingerprint TEXT NOT NULL, '
                 'incoming_edge TEXT NOT NULL, goal TEXT NOT NULL, nodes TEXT NOT NULL, '
                 'last_seq INTEGER NOT NULL, start_index INTEGER NOT NULL, end_index INTEGER NOT NULL, '
                 'status TEXT NOT NULL, generation TEXT NOT NULL)'),
    'lab_orders': ('CREATE TABLE lab_orders (task_id TEXT NOT NULL REFERENCES lab_jobs(task_id), '
                   'seq INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL, '
                   'state TEXT NOT NULL, PRIMARY KEY(task_id,seq))'),
    'lab_events': ('CREATE TABLE lab_events (robot_id TEXT NOT NULL, event_id TEXT NOT NULL, '
                   'task_id TEXT NOT NULL REFERENCES lab_jobs(task_id), seq INTEGER NOT NULL, '
                   'payload TEXT NOT NULL, digest TEXT NOT NULL, generation TEXT NOT NULL, '
                   'PRIMARY KEY(robot_id,event_id), UNIQUE(task_id,seq))'),
    'lab_occupancy': ('CREATE TABLE lab_occupancy (kind TEXT NOT NULL, resource_id TEXT NOT NULL, '
                      'task_id TEXT NOT NULL REFERENCES lab_jobs(task_id), PRIMARY KEY(kind,resource_id))'),
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def _digest(payload):
    return hashlib.sha256(payload.encode('ascii')).hexdigest()


class OccurrenceAllocationLab:
    """One epoch owner of a separate file-backed, simulation-only test DB."""

    def __init__(self, path, map_data, *, simulation=False):
        if simulation is not True:
            raise ValueError('explicit simulation=True is required')
        path = os.fspath(path)
        if not isinstance(path, str) or not path or path == ':memory:':
            raise ValueError('separate file-backed experimental database required')
        self.path = path
        self._map = copy.deepcopy(map_data)
        self._map_json = _canonical(self._map)
        self._fingerprint = _digest(self._map_json)
        if not isinstance(self._map, dict):
            raise ValueError('map must be an object')
        name, version = self._map.get('map_id'), self._map.get('version')
        if not isinstance(name, str) or not name.strip() or type(version) is not int or version < 0:
            raise ValueError('map identity and version required')
        self._map_id = '{}@v{}'.format(name, version)
        self._generation = secrets.token_hex(32)

    @property
    def generation(self):
        """Local owner epoch; it is a concurrency fence, not authentication."""
        return self._generation

    @contextmanager
    def _transaction(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    @staticmethod
    def _schema(db):
        objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        expected = {('table', name, sql) for name, sql in _SCHEMA.items()}
        if len(objects) != len(expected) or set(objects) != expected:
            raise LabConflict('experimental DB schema differs')
        meta = dict(db.execute('SELECT key,value FROM lab_meta'))
        if set(meta) != {'schema_version', 'map_id', 'fingerprint', 'generation'} or meta['schema_version'] != '1':
            raise LabConflict('experimental DB metadata differs')
        return meta

    def _verify(self, db, *, expected_generation=None):
        meta = self._schema(db)
        generation = self.generation if expected_generation is None else expected_generation
        if (not isinstance(meta['generation'], str)
                or re.fullmatch('[0-9a-f]{64}', meta['generation']) is None
                or meta['generation'] != generation or meta['map_id'] != self._map_id
                or meta['fingerprint'] != self._fingerprint or _canonical(self._map) != self._map_json):
            raise LabConflict('generation or map changed')

    def _verify_all(self, db, *, expected_generation=None):
        """Recheck every job and foreign key inside the caller's transaction.

        The lab's unique resource key is useful only when existing owners have
        not lost rows. A valid target task cannot establish that other tasks'
        occupancy, history or event records are intact.
        """
        self._verify(db, expected_generation=expected_generation)
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise LabConflict('experimental DB has orphan records')
        tasks = db.execute('SELECT task_id FROM lab_jobs ORDER BY task_id').fetchall()
        for (task_id,) in tasks:
            self._load(db, task_id, expected_generation=expected_generation)

    @staticmethod
    def _require_no_blocks(db):
        if db.execute("SELECT 1 FROM lab_jobs WHERE status='blocked' LIMIT 1").fetchone():
            raise LabConflict('unresolved pre-restart tasks block new activity')

    def initialize(self):
        """Fence an older instance; unfinished jobs remain blocked with occupancy."""
        with self._transaction(write=True) as db:
            objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if not objects:
                for sql in _SCHEMA.values():
                    db.execute(sql)
                db.executemany('INSERT INTO lab_meta VALUES (?,?)', [
                    ('schema_version', '1'), ('map_id', self._map_id),
                    ('fingerprint', self._fingerprint), ('generation', self.generation)])
                return
            meta = self._schema(db)
            if meta['map_id'] != self._map_id or meta['fingerprint'] != self._fingerprint:
                raise LabConflict('map differs from experimental DB')
            # Recheck old jobs under their current owner epoch before fencing
            # it. If the DB is corrupt, rollback leaves the old epoch intact;
            # all old owner writes will also fail the same global check.
            self._verify_all(db, expected_generation=meta['generation'])
            if meta['generation'] != self.generation:
                db.execute("UPDATE lab_jobs SET status='blocked' WHERE status='active'")
                db.execute("UPDATE lab_meta SET value=? WHERE key='generation'", (self.generation,))
            self._verify_all(db)

    def _load(self, db, task_id, *, expected_generation=None):
        self._verify(db, expected_generation=expected_generation)
        row = db.execute('SELECT robot_id,session_id,map_id,fingerprint,incoming_edge,goal,nodes,'
                         'last_seq,start_index,end_index,status,generation FROM lab_jobs WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise LabConflict('unknown task')
        robot, session, map_id, fp, incoming, goal, nodes_json, seq, start, end, status, job_generation = row
        try:
            _id(task_id, 'task_id'); _id(robot, 'robot_id'); _id(session, 'session_id')
            if not robot.startswith('sim-') or not robot[4:].strip() or status not in ('active', 'blocked', 'complete'):
                raise ValueError('invalid simulated task')
            if (not isinstance(job_generation, str) or re.fullmatch('[0-9a-f]{64}', job_generation) is None
                    or (status == 'active' and job_generation != (
                        self.generation if expected_generation is None else expected_generation))):
                raise ValueError('task generation differs')
            if map_id != self._map_id or fp != self._fingerprint:
                raise ValueError('task map changed')
            nodes = json.loads(nodes_json)
            if nodes_json != _canonical(nodes):
                raise ValueError('noncanonical route')
            planned = plan_field_route(self._map, map_ref=map_id, incoming_edge=incoming, goal=goal)
            if list(planned.nodes) != nodes:
                raise ValueError('stored route differs from candidate')
            rows = db.execute('SELECT seq,payload,digest,state FROM lab_orders WHERE task_id=? ORDER BY seq',
                              (task_id,)).fetchall()
            if not rows or type(seq) is not int or seq != len(rows):
                raise ValueError('missing order history')
            orders, states = [], []
            for i, (number, payload, digest, state) in enumerate(rows, start=1):
                order = _decode(payload)
                canonical, expected_digest = _encode(order)
                if (number != i or payload != canonical or digest != expected_digest
                        or state not in ('pending', 'uncertain', 'acked') or (i < len(rows) and state != 'acked')):
                    raise ValueError('order history inconsistent')
                orders.append(order); states.append(state)
            indices = visit_indices_from_orders(orders)
            if (orders[0]['order_id'] != task_id or orders[0]['map_id'] != map_id
                    or orders[0]['nodes'] != nodes or type(start) is not int or type(end) is not int
                    or start != (0 if seq == 1 else indices[-2]) or end != indices[-1]):
                raise ValueError('order and visit indexes disagree')
            cursor = 0
            for index in indices:
                segment = plan_occurrence_segment(self._map, nodes, cursor)
                if segment.end_index != index:
                    raise ValueError('order skips a waiting occurrence')
                cursor = index
            current = plan_occurrence_segment(self._map, nodes, start)
            held = set(db.execute('SELECT kind,resource_id FROM lab_occupancy WHERE task_id=?', (task_id,)))
            expected = {('node', nodes[-1])} if status == 'complete' else set(current.resources)
            if held != expected:
                raise ValueError('occupancy differs from current segment')
            events = db.execute('SELECT robot_id,event_id,seq,payload,digest,generation FROM lab_events '
                                'WHERE task_id=? ORDER BY seq', (task_id,)).fetchall()
            count = seq if status == 'complete' else seq - 1
            if len(events) != count:
                raise ValueError('event history is incomplete')
            for i, (event_robot, event_id, event_seq, payload, digest, event_generation) in enumerate(events):
                event = _decode(payload)
                start_at = 0 if i == 0 else indices[i - 1]
                self._event(event_robot, event, robot, session, task_id, orders[i],
                            occurrence_clearance(self._map, nodes, start_at))
                if (event_robot != robot or event_id != event['event_id'] or event_seq != i + 1
                        or payload != _canonical(event) or digest != _digest(payload)
                        or not isinstance(event_generation, str)
                        or re.fullmatch('[0-9a-f]{64}', event_generation) is None
                        or event_generation != job_generation):
                    raise ValueError('event history is inconsistent')
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
            raise LabConflict('experimental task history or occupancy is inconsistent') from exc
        return {'task_id': task_id, 'robot_id': robot, 'session_id': session, 'map_id': map_id,
                'start_index': start, 'end_index': end, 'status': status, 'orders': orders,
                'order_states': states, 'occupancy': tuple(sorted(held))}

    @staticmethod
    def _event(robot_id, event, expected_robot, session, task, order, cleared):
        fields = {'type', 'event_id', 'robot_session', 'order_id', 'seq', 'map_id', 'stopped_at', 'cleared_resources'}
        if not isinstance(event, dict) or set(event) != fields or event['type'] != 'rear_clear':
            raise ValueError('invalid simulated event')
        _id(event['event_id'], 'event_id')
        resources = event['cleared_resources']
        if not isinstance(resources, list) or any(not isinstance(r, list) or len(r) != 2 for r in resources):
            raise ValueError('invalid simulated clearance')
        pairs = [tuple(r) for r in resources]
        if (robot_id != expected_robot or event['robot_session'] != session or event['order_id'] != task
                or type(event['seq']) is not int or event['seq'] != order['seq']
                or event['map_id'] != order['map_id'] or event['stopped_at'] != order['released_until']
                or len(set(pairs)) != len(pairs) or set(pairs) != set(cleared)):
            raise ValueError('simulated event does not match segment')

    def snapshot(self, task_id):
        _id(task_id, 'task_id')
        with self._transaction() as db:
            return self._load(db, task_id)

    def start(self, task_id, robot_id, session_id, incoming_edge, goal):
        _id(task_id, 'task_id'); _id(robot_id, 'robot_id'); _id(session_id, 'session_id')
        if not robot_id.startswith('sim-') or not robot_id[4:].strip():
            raise ValueError('only simulation robot IDs are accepted')
        route = plan_field_route(self._map, map_ref=self._map_id, incoming_edge=incoming_edge, goal=goal)
        nodes = list(route.nodes)
        segment = plan_occurrence_segment(self._map, nodes, 0)
        order = {'order_id': task_id, 'seq': 1, 'map_id': self._map_id, 'nodes': nodes,
                 'released_until': segment.released_until, 'action_at_end': {'type': 'none'}}
        payload, digest = _encode(order)
        try:
            with self._transaction(write=True) as db:
                self._verify_all(db)
                self._require_no_blocks(db)
                db.execute('INSERT INTO lab_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (task_id, robot_id, session_id, self._map_id, self._fingerprint,
                            incoming_edge, goal, _canonical(nodes), 1, 0, segment.end_index, 'active', self.generation))
                db.executemany('INSERT INTO lab_occupancy VALUES (?,?,?)',
                               [(kind, resource, task_id) for kind, resource in segment.resources])
                db.execute('INSERT INTO lab_orders VALUES (?,?,?,?,?)', (task_id, 1, payload, digest, 'pending'))
                self._verify_all(db)
                return self._load(db, task_id)
        except sqlite3.IntegrityError as exc:
            raise LabConflict('robot, task or resource is already allocated') from exc

    def _mark(self, task_id, seq, session, expected, target):
        _id(task_id, 'task_id'); _id(session, 'session_id')
        if type(seq) is not int or seq < 1:
            raise ValueError('sequence must be positive')
        with self._transaction(write=True) as db:
            self._verify_all(db)
            job = self._load(db, task_id)
            self._require_no_blocks(db)
            if (job['status'] != 'active' or job['session_id'] != session
                    or job['orders'][-1]['seq'] != seq or job['order_states'][-1] != expected):
                raise LabConflict('order is stale or state is uncertain')
            db.execute('UPDATE lab_orders SET state=? WHERE task_id=? AND seq=? AND state=?',
                       (target, task_id, seq, expected))
            self._verify_all(db)
            return self._load(db, task_id)

    def mark_attempted(self, task_id, seq, session):
        """Record uncertainty *before* a hypothetical send; does not send."""
        return self._mark(task_id, seq, session, 'pending', 'uncertain')

    def mark_broker_acked(self, task_id, seq, session):
        """Record a synthetic broker ACK; this is not robot receipt proof."""
        return self._mark(task_id, seq, session, 'uncertain', 'acked')

    def advance_simulated(self, robot_id, event, *, generation, retained=False):
        """Atomically apply a synthetic test stop, resource transfer and outbox.

        This operation cannot verify a real robot and does not publish MQTT.
        """
        if retained is not False or generation != self.generation:
            raise LabConflict('retained or old-generation observations are rejected')
        _id(robot_id, 'robot_id')
        if not isinstance(event, dict):
            raise LabConflict('invalid simulated event')
        try:
            task_id = _id(event.get('order_id'), 'order_id')
            event_id = _id(event.get('event_id'), 'event_id')
            payload = _canonical(event)
            digest = _digest(payload)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
            raise LabConflict('invalid simulated event') from exc
        try:
            with self._transaction(write=True) as db:
                self._verify_all(db)
                job = self._load(db, task_id)
                self._require_no_blocks(db)
                if job['robot_id'] != robot_id:
                    raise LabConflict('robot differs')
                existing = db.execute('SELECT task_id,payload,digest FROM lab_events WHERE robot_id=? AND event_id=?',
                                      (robot_id, event_id)).fetchone()
                if existing:
                    if existing != (task_id, payload, digest):
                        raise LabConflict('event id reused with different content')
                    return job
                if job['status'] != 'active':
                    raise LabConflict('task is not active')
                if job['order_states'][-1] != 'acked':
                    raise LabConflict('order delivery is pending or uncertain')
                nodes = job['orders'][-1]['nodes']
                current = plan_occurrence_segment(self._map, nodes, job['start_index'])
                cleared = occurrence_clearance(self._map, nodes, job['start_index'])
                try:
                    self._event(robot_id, event, job['robot_id'], job['session_id'], task_id,
                                job['orders'][-1], cleared)
                except (ValueError, TypeError) as exc:
                    raise LabConflict('simulated event is inconsistent') from exc
                if current.end_index == len(nodes) - 1:
                    next_segment = None
                    keep = {('node', nodes[-1])}
                else:
                    next_segment = plan_occurrence_segment(self._map, nodes, current.end_index)
                    keep = set(next_segment.resources)
                current_resources = set(current.resources)
                db.executemany('INSERT INTO lab_occupancy VALUES (?,?,?)',
                               [(kind, resource, task_id) for kind, resource in sorted(keep - current_resources)])
                for kind, resource in sorted(current_resources - keep):
                    deleted = db.execute('DELETE FROM lab_occupancy WHERE task_id=? AND kind=? AND resource_id=?',
                                         (task_id, kind, resource))
                    if deleted.rowcount != 1:
                        raise LabConflict('resource ownership changed')
                seq = job['orders'][-1]['seq']
                if next_segment is None:
                    db.execute("UPDATE lab_jobs SET status='complete' WHERE task_id=?", (task_id,))
                else:
                    following = dict(job['orders'][-1], seq=seq + 1, released_until=next_segment.released_until)
                    following_payload, following_digest = _encode(following)
                    db.execute('INSERT INTO lab_orders VALUES (?,?,?,?,?)',
                               (task_id, seq + 1, following_payload, following_digest, 'pending'))
                    db.execute('UPDATE lab_jobs SET last_seq=?,start_index=?,end_index=? WHERE task_id=?',
                               (seq + 1, next_segment.start_index, next_segment.end_index, task_id))
                db.execute('INSERT INTO lab_events VALUES (?,?,?,?,?,?,?)',
                           (robot_id, event_id, task_id, seq, payload, digest, self.generation))
                self._verify_all(db)
                return self._load(db, task_id)
        except sqlite3.IntegrityError as exc:
            raise LabConflict('next resource or event is already owned') from exc
