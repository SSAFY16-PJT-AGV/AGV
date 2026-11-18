"""Categorical investigation references in an isolated simulation database.

Records are not authenticated people, physical evidence, control decisions,
or an immutable audit trail. They never change tasks, reservations or orders.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

from server.order_publisher import _topic
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress


class InvestigationConflict(ValueError):
    """A simulated reference could not be safely stored or read."""


_CREATE = ('CREATE TABLE sim_investigation_notes ('
           'note_id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, '
           'task_id TEXT NOT NULL REFERENCES tasks(task_id), category TEXT NOT NULL, '
           'created_utc TEXT NOT NULL, snapshot_json TEXT NOT NULL)')
_CATEGORIES = frozenset(('position', 'connection', 'action', 'other'))
_MAX_TASK_NOTES = 1000
_MAX_DATABASE_NOTES = 10000
_MAX_PAGE = 20
_MAX_SNAPSHOT_BYTES = 64 * 1024


def _bounded_column(name, maximum):
    return ("CASE WHEN typeof({0})='text' AND length(CAST({0} AS BLOB)) "
            "BETWEEN 1 AND {1} THEN {0} ELSE NULL END").format(name, maximum)


_NOTE_COLUMNS = ('note_id,' + ','.join(_bounded_column(name, maximum) for name, maximum in (
    ('request_id', 32), ('task_id', 256), ('category', 10), ('created_utc', 40),
    ('snapshot_json', _MAX_SNAPSHOT_BYTES))))


def _id(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise ValueError('random request ID must be 32 lowercase hexadecimal characters')
    return value


def _text(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid simulation metadata')
    try:
        size = len(value.encode('utf-8', errors='strict'))
    except UnicodeError as exc:
        raise ValueError('invalid simulation metadata') from exc
    if size > 256:
        raise ValueError('invalid simulation metadata')
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False,
                      separators=(',', ':'))


def _validate_snapshot(value):
    keys = {'status', 'configured_robot', 'robot_id', 'map_id', 'released_until', 'orders', 'orders_truncated',
            'occupancy', 'occupancy_truncated', 'integrity'}
    if (not isinstance(value, dict) or set(value) != keys or value['integrity'] != 'not_checked'
            or type(value['orders_truncated']) is not bool or type(value['occupancy_truncated']) is not bool):
        raise ValueError('invalid reference snapshot')
    _text(value['status'])
    _topic(value['configured_robot'])
    job = (value['robot_id'], value['map_id'], value['released_until'])
    if job != (None, None, None):
        _topic(job[0])
        _text(job[1]); _text(job[2])
    orders, occupancy = value['orders'], value['occupancy']
    if not isinstance(orders, list) or len(orders) > 100 or not isinstance(occupancy, list) or len(occupancy) > 500:
        raise ValueError('invalid reference snapshot')
    for order in orders:
        if (not isinstance(order, dict) or set(order) != {'seq', 'state'}
                or type(order['seq']) is not int or order['seq'] < 1):
            raise ValueError('invalid reference snapshot')
        _text(order['state'])
    for resource in occupancy:
        if (not isinstance(resource, list) or len(resource) != 2
                or resource[0] not in ('node', 'edge', 'zone')):
            raise ValueError('invalid reference snapshot')
        _text(resource[1])
    if job == (None, None, None) and (orders or occupancy):
        raise ValueError('invalid reference snapshot')


class SimInvestigationNotes:
    """Bounded category-only references in the existing simulation DB."""

    def __init__(self, progress, robot_tasks, *, simulation=False):
        if simulation is not True:
            raise ValueError('explicit isolated simulation is required')
        if (type(progress) is not SegmentProgress or progress._enabled is not True
                or type(progress._gate) is not RecoveryGate):
            raise ValueError('simulation progress and recovery gate are required')
        if not isinstance(robot_tasks, dict) or not robot_tasks:
            raise ValueError('explicit simulated robot tasks required')
        tasks = {}
        for robot, values in robot_tasks.items():
            _topic(robot)
            if not isinstance(values, (list, tuple, set, frozenset)) or not values:
                raise ValueError('explicit simulated task list required')
            for task in values:
                _text(task)
                if task in tasks:
                    raise ValueError('task cannot map to multiple robots')
                tasks[task] = robot
        self._tasks = tasks
        self._progress, self._store, self._gate = progress, progress.store, progress._gate
        raw = os.fspath(self._store.path)
        if not isinstance(raw, str) or os.path.islink(raw) or not os.path.isfile(raw):
            raise ValueError('existing regular simulation database required')
        self.path = os.path.realpath(raw)
        stat = os.stat(self.path)
        self._file_identity = (stat.st_dev, stat.st_ino)

    def _check_file(self):
        try:
            if os.path.islink(self.path) or not os.path.isfile(self.path):
                raise InvestigationConflict('simulation database unavailable')
            stat = os.stat(self.path)
            if (stat.st_dev, stat.st_ino) != self._file_identity:
                raise InvestigationConflict('simulation database changed')
            check = getattr(self._store, 'validate_bound', None)
            if callable(check):
                check(self.path)
        except (OSError, sqlite3.Error) as exc:
            raise InvestigationConflict('simulation database unavailable') from exc

    @contextmanager
    def _transaction(self, *, write):
        # Notes cannot authorize control. Do not hold the gate operation lock
        # while SQLite waits: a disconnect must be able to block locally first.
        if (getattr(self._gate._local, 'db', None) is not None
                or getattr(self._gate._local, 'serializing', False)):
            raise InvestigationConflict('nested recovery transaction is not supported')
        self._check_file()
        uri = Path(self.path).resolve().as_uri() + ('?mode=rw' if write else '?mode=ro')
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)) as db:
                self._check_file()
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                try:
                    self._progress._check_database(db)
                    self._gate._active(db)
                    yield db
                    self._gate._active(db)
                    self._check_file()
                    db.execute('COMMIT')
                except BaseException:
                    if db.in_transaction:
                        db.execute('ROLLBACK')
                    raise
        except sqlite3.Error:
            raise InvestigationConflict('simulation reference database unavailable') from None

    @staticmethod
    def _check_schema(db):
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                         "AND name='sim_investigation_notes'").fetchone()
        if row != (_CREATE,):
            raise InvestigationConflict('simulation reference schema differs')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                      "AND lower(tbl_name)='sim_investigation_notes' LIMIT 1").fetchone():
            raise InvestigationConflict('simulation reference triggers are not allowed')

    def initialize(self):
        with self._transaction(write=True) as db:
            row = db.execute("SELECT type FROM sqlite_master WHERE name='sim_investigation_notes'").fetchone()
            if row is None:
                db.execute(_CREATE)
            self._check_schema(db)

    def _task(self, task):
        _text(task)
        if task not in self._tasks:
            raise ValueError('simulation task is not configured')

    def _check_binding(self, note, task):
        robot = note['snapshot']['robot_id']
        if (note['task_id'] != task or note['snapshot']['configured_robot'] != self._tasks[task]
                or (robot is not None and robot != self._tasks[task])):
            raise InvestigationConflict('recorded simulation task mapping differs')

    def _check_current_binding(self, db, task):
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise InvestigationConflict('simulation database has orphan records')
        task_row = db.execute('SELECT status FROM tasks WHERE task_id=?', (task,)).fetchone()
        if task_row is None:
            raise InvestigationConflict('simulation task is unavailable')
        row = db.execute('SELECT ' + _bounded_column('robot_id', 256) +
                         ' FROM allocation_jobs WHERE task_id=?', (task,)).fetchone()
        outbox = db.execute('SELECT 1 FROM allocation_outbox WHERE task_id=?', (task,)).fetchone()
        if ((row is None) != (outbox is None)
                or (task_row[0] == 'ASSIGNED' and (row is None or outbox is None))):
            raise InvestigationConflict('simulation allocation metadata is incomplete')
        if row is not None and row[0] != self._tasks[task]:
            raise InvestigationConflict('current simulation task mapping differs')

    def _snapshot(self, db, task):
        def bounded(column):
            return ("CASE WHEN typeof({0})='text' AND length(CAST({0} AS BLOB)) "
                    "BETWEEN 1 AND 256 THEN {0} ELSE NULL END").format(column)
        row = db.execute('SELECT ' + bounded('status') + ' FROM tasks WHERE task_id=?', (task,)).fetchone()
        if row is None or row[0] is None:
            raise InvestigationConflict('simulation task is unavailable')
        job = db.execute('SELECT ' + bounded('robot_id') + ',' + bounded('map_id') + ',' +
                         bounded('released_until') + ' FROM allocation_jobs WHERE task_id=?', (task,)).fetchone()
        first = db.execute('SELECT ' + bounded('state') + ' FROM allocation_outbox WHERE task_id=?', (task,)).fetchone()
        later = db.execute("SELECT CASE WHEN typeof(seq)='integer' AND seq>1 THEN seq ELSE NULL END, " +
                           bounded('state') + " FROM progress_orders WHERE task_id=? "
                           "AND (seq<>1 OR typeof(seq)<>'integer') ORDER BY seq LIMIT 100", (task,)).fetchall()
        resources = db.execute('SELECT ' + bounded('kind') + ',' + bounded('resource_id') +
                               ' FROM allocation_occupancy WHERE task_id=? ORDER BY kind,resource_id LIMIT 501',
                               (task,)).fetchall()
        if ((job is None) != (first is None) or (job is not None and
                (any(value is None for value in job) or job[0] != self._tasks[task]))
                or (first is not None and first[0] is None)
                or any(seq is None or state is None for seq, state in later)
                or any(kind is None or resource is None for kind, resource in resources)
                or (job is None and (later or resources))):
            raise InvestigationConflict('simulation task metadata is inconsistent')
        orders = ([{'seq': 1, 'state': first[0]}] if first else [])
        orders.extend({'seq': seq, 'state': state} for seq, state in later[:99])
        return {'status': row[0], 'configured_robot': self._tasks[task], 'robot_id': job[0] if job else None,
                'map_id': job[1] if job else None, 'released_until': job[2] if job else None,
                'orders': orders, 'orders_truncated': len(later) > 99,
                'occupancy': [list(item) for item in resources[:500]],
                'occupancy_truncated': len(resources) > 500, 'integrity': 'not_checked'}

    @staticmethod
    def _note(row):
        note_id, request, task, category, created, snapshot = row
        try:
            if (type(note_id) is not int or note_id < 1 or _id(request) != request
                    or _text(task) != task or category not in _CATEGORIES
                    or not isinstance(created, str) or len(created) > 40 or not created.endswith('Z')
                    or not isinstance(snapshot, str) or len(snapshot.encode('utf-8')) > _MAX_SNAPSHOT_BYTES):
                raise ValueError('invalid reference')
            value = json.loads(snapshot)
            _validate_snapshot(value)
            if _canonical(value) != snapshot:
                raise ValueError('invalid reference snapshot')
            observed = datetime.fromisoformat(created[:-1] + '+00:00')
            if observed.isoformat(timespec='microseconds').replace('+00:00', 'Z') != created:
                raise ValueError('invalid reference timestamp')
            return {'note_id': note_id, 'request_id': request, 'task_id': task,
                    'category': category, 'created_utc': created, 'snapshot': value}
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise InvestigationConflict('stored simulation reference is invalid') from exc

    def record(self, task_id, request_id, category):
        self._task(task_id)
        _id(request_id)
        if not isinstance(category, str) or category not in _CATEGORIES:
            raise ValueError('invalid investigation category')
        with self._transaction(write=True) as db:
            self._check_schema(db)
            self._check_current_binding(db, task_id)
            prior = db.execute('SELECT ' + _NOTE_COLUMNS + ' '
                               'FROM sim_investigation_notes WHERE request_id=?', (request_id,)).fetchone()
            if prior is not None:
                result = self._note(prior)
                if (result['task_id'], result['category']) != (task_id, category):
                    raise InvestigationConflict('request ID belongs to another reference')
                self._check_binding(result, task_id)
                return result
            if (db.execute('SELECT COUNT(*) FROM sim_investigation_notes WHERE task_id=?', (task_id,)).fetchone()[0]
                    >= _MAX_TASK_NOTES or db.execute('SELECT COUNT(*) FROM sim_investigation_notes').fetchone()[0]
                    >= _MAX_DATABASE_NOTES):
                raise InvestigationConflict('simulation reference capacity reached')
            snapshot = _canonical(self._snapshot(db, task_id))
            created = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            cursor = db.execute('INSERT INTO sim_investigation_notes(request_id,task_id,category,created_utc,snapshot_json) '
                                'VALUES(?,?,?,?,?)', (request_id, task_id, category, created, snapshot))
            result = self._note((cursor.lastrowid, request_id, task_id, category, created, snapshot))
            self._check_schema(db)
            return result

    def list(self, task_id, *, after_id=0, limit=20):
        self._task(task_id)
        if type(after_id) is not int or not 0 <= after_id <= 9223372036854775807:
            raise ValueError('invalid reference cursor')
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE:
            raise ValueError('invalid reference page size')
        with self._transaction(write=False) as db:
            self._check_schema(db)
            self._check_current_binding(db, task_id)
            rows = db.execute('SELECT ' + _NOTE_COLUMNS + ' '
                              'FROM sim_investigation_notes WHERE task_id=? AND note_id>? '
                              'ORDER BY note_id LIMIT ?', (task_id, after_id, limit + 1)).fetchall()
            notes = [self._note(row) for row in rows[:limit]]
            for note in notes:
                self._check_binding(note, task_id)
            return {'notes': notes, 'next_after': notes[-1]['note_id'] if len(rows) > limit else None}
