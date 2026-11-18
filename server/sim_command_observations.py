"""Proposed response claims in an existing isolated allocation database.

This is not a robot protocol, identity proof, drive permit or reservation
release. No networking or actuator integration is provided by this module.
"""
from contextlib import closing, contextmanager
import copy
import hashlib
from pathlib import Path
import re
import sqlite3

from server.allocation_store import _verify_field_global
from server.order_publisher import _payload, _topic
from server.proposed_command_journal import _RESPONSE_KEYS, _canonical, _sequence, _text
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress


class ObservationConflict(ValueError):
    """A simulated claim is stale, conflicting, or cannot be bound safely."""


_CREATE = ('CREATE TABLE sim_command_observations ('
           'robot_id TEXT NOT NULL, event_id TEXT NOT NULL, task_id TEXT NOT NULL, '
           'seq INTEGER NOT NULL, robot_session TEXT NOT NULL, map_id TEXT NOT NULL, '
           'order_digest TEXT NOT NULL, outcome TEXT NOT NULL, epoch TEXT NOT NULL, '
           'response_json TEXT NOT NULL, PRIMARY KEY(robot_id,event_id), '
           'UNIQUE(task_id,seq))')
_CREATE_META = ('CREATE TABLE sim_command_observation_meta ('
                'singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch TEXT NOT NULL)')


class SimCommandObservations:
    """Record proposed response claims against the current simulated order."""

    def __init__(self, progress, robot_sessions, *, simulation=False):
        if simulation is not True:
            raise ValueError('explicit isolated simulation is required')
        if (type(progress) is not SegmentProgress or progress._enabled is not True
                or progress._field_required is not True or type(progress._gate) is not RecoveryGate):
            raise ValueError('field simulation progress and recovery gate are required')
        if not isinstance(robot_sessions, dict) or not robot_sessions:
            raise ValueError('explicit simulated robot sessions are required')
        seen, mapping = set(), {}
        for robot, tasks in robot_sessions.items():
            _topic(robot)
            if not isinstance(tasks, dict) or not tasks:
                raise ValueError('explicit task/session mapping is required')
            mapping[robot] = {}
            for task, session in tasks.items():
                _text(task, 'task id')
                _text(session, 'session')
                if task in seen:
                    raise ValueError('task cannot be mapped to multiple robots')
                seen.add(task)
                mapping[robot][task] = session
        self._progress = progress
        self._store = progress.store
        self._gate = progress._gate
        self._sessions = mapping

    @contextmanager
    def _transaction(self):
        # Match RecoveryGate's lock-before-database order. A fresh epoch in
        # another process also has to acquire the SQLite writer lock.
        with self._gate._operation_lock:
            if getattr(self._gate._local, 'db', None) is not None:
                raise ObservationConflict('nested recovery transactions are not supported')
            validate_bound = getattr(self._store, 'validate_bound', None)
            if callable(validate_bound):
                validate_bound(self._store.path)
            uri = Path(self._store.path).resolve().as_uri() + '?mode=rw'
            with closing(sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)) as db:
                if callable(validate_bound):
                    validate_bound(self._store.path)
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                try:
                    self._progress._check_database(db)
                    self._gate._active(db)
                    verify_fleet = getattr(self._store, '_verify_field_fleet', None)
                    if callable(verify_fleet):
                        verify_fleet(db)
                    _verify_field_global(db, field_required=True)
                    yield db
                    _verify_field_global(db, field_required=True)
                    if callable(verify_fleet):
                        verify_fleet(db)
                    if callable(validate_bound):
                        validate_bound(self._store.path)
                    db.execute('COMMIT')
                except BaseException:
                    if db.in_transaction:
                        db.execute('ROLLBACK')
                    raise

    @staticmethod
    def _check_schema(db):
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                         "AND name='sim_command_observations'").fetchone()
        if row != (_CREATE,):
            raise ObservationConflict('simulation response table is missing or changed')
        meta = db.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                          "AND name='sim_command_observation_meta'").fetchone()
        if meta != (_CREATE_META,):
            raise ObservationConflict('simulation response epoch table is missing or changed')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND lower(tbl_name) IN "
                      "('sim_command_observations','sim_command_observation_meta') LIMIT 1").fetchone():
            raise ObservationConflict('simulation response tables cannot have triggers')

    def _check_epoch(self, db):
        if db.execute('SELECT singleton,epoch FROM sim_command_observation_meta').fetchall() != [(1, self._gate._epoch)]:
            raise ObservationConflict('simulation response epoch differs')

    def initialize(self):
        """Create the opt-in table in this allocation DB, or check its schema."""
        with self._transaction() as db:
            found = db.execute("SELECT name FROM sqlite_master WHERE name IN "
                               "('sim_command_observations','sim_command_observation_meta')").fetchall()
            if not found:
                # No evidence binds an existing allocation to this fresh epoch.
                if db.execute('SELECT 1 FROM allocation_jobs LIMIT 1').fetchone():
                    raise ObservationConflict('existing allocations cannot be bound to a new observation epoch')
                db.execute(_CREATE)
                db.execute(_CREATE_META)
                db.execute('INSERT INTO sim_command_observation_meta VALUES (1,?)', (self._gate._epoch,))
            self._check_schema(db)
            self._check_epoch(db)

    def record(self, robot_id, response, *, retained=False):
        """Record an untrusted synthetic claim without changing any order state."""
        _topic(robot_id)
        if retained is not False:
            raise ObservationConflict('retained or ambiguous responses are rejected')
        event = copy.deepcopy(response)
        if not isinstance(event, dict) or set(event) != _RESPONSE_KEYS:
            raise ValueError('invalid proposed simulation response')
        for key in ('event_id', 'robot_session', 'order_id', 'map_id'):
            _text(event[key], key)
        _sequence(event['seq'])
        if (event['outcome'] not in ('accepted', 'rejected')
                or not isinstance(event['order_digest'], str)
                or re.fullmatch('[0-9a-f]{64}', event['order_digest']) is None):
            raise ValueError('invalid outcome or wire digest')
        task = event['order_id']
        if self._sessions.get(robot_id, {}).get(task) != event['robot_session']:
            raise ObservationConflict('response session or task is not configured')
        canonical = _canonical(event)
        with self._transaction() as db:
            self._check_schema(db)
            self._check_epoch(db)
            with self._gate._state_lock:
                if task in self._gate._blocked:
                    raise ObservationConflict('simulated task requires recovery')
            robot, path, stop = self._progress._job(db, task, robot_id)
            seq, stored, state, order = self._progress._latest(db, task, robot, path, stop)
            segment = self._progress._segment_for_task(db, task, path, stop)
            if self._progress._occupancy(db, task) != set(segment.resources):
                raise ObservationConflict('simulation occupancy differs from current segment')
            wire = _payload(order)
            if (seq != event['seq'] or state not in ('uncertain', 'acked')
                    or event['map_id'] != self._progress._map_id
                    or stored.encode('utf-8') != wire
                    or hashlib.sha256(wire).hexdigest() != event['order_digest']):
                raise ObservationConflict('response does not match current attempted order')
            identity = (robot_id, event['event_id'])
            prior = db.execute('SELECT task_id,seq,robot_session,map_id,order_digest,outcome,epoch,response_json '
                               'FROM sim_command_observations WHERE robot_id=? AND event_id=?', identity).fetchone()
            expected = (task, seq, event['robot_session'], event['map_id'], event['order_digest'],
                        event['outcome'], self._gate._epoch, canonical)
            if prior is not None:
                if prior != expected:
                    raise ObservationConflict('event ID is bound to different content')
                return {'status': 'duplicate', 'outcome': event['outcome']}
            if db.execute('SELECT 1 FROM sim_command_observations WHERE task_id=? AND seq=?',
                          (task, seq)).fetchone():
                raise ObservationConflict('command already has a response claim')
            db.execute('INSERT INTO sim_command_observations VALUES (?,?,?,?,?,?,?,?,?,?)',
                       identity + expected)
            return {'status': 'recorded', 'outcome': event['outcome']}
