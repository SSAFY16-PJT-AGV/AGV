"""Simulation-only recovery observations; never actuates or releases occupancy.

This proposal has no message authentication or physical safety verification.
The caller must use an isolated simulator and validate each sensitive change
in the same SQLite write transaction; a past observation is not drive permission.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager

from server.allocation_store import _validate_json


class RecoveryError(ValueError):
    """A recovery request is invalid, stale or requires manual reconciliation."""


def _id(value, name):
    if not isinstance(value, str) or not value or value != value.strip():
        raise RecoveryError(name + ' must be a nonempty string')
    try:
        encoded = value.encode('utf-8', errors='strict')
    except UnicodeError as exc:
        raise RecoveryError(name + ' must be valid UTF-8') from exc
    if len(encoded) > 128 or '\x00' in value:
        raise RecoveryError(name + ' exceeds the simulation limit')
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('non-finite JSON value')


def _decode(payload):
    if not isinstance(payload, str) or len(payload.encode('utf-8', errors='strict')) > 256 * 1024:
        raise RecoveryError('invalid stored order payload')
    try:
        value = json.loads(payload, object_pairs_hook=_unique_pairs, parse_constant=_constant)
        _validate_json(value)
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise RecoveryError('invalid stored order payload') from exc
    fields = {'order_id', 'seq', 'map_id', 'nodes', 'released_until', 'action_at_end'}
    if not isinstance(value, dict) or set(value) != fields:
        raise RecoveryError('invalid stored order fields')
    return value


def _digest_json(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(',', ':')).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def _snapshot(db, task_id):
    """Read an internally consistent order history and currently held resources."""
    row = db.execute(
        "SELECT j.robot_id,j.map_id,j.nodes_json,j.released_until,o.payload,o.state,t.status "
        "FROM allocation_jobs j JOIN allocation_outbox o ON o.task_id=j.task_id "
        "JOIN tasks t ON t.task_id=j.task_id WHERE j.task_id=?", (task_id,)
    ).fetchone()
    if row is None or row[6] != 'ASSIGNED':
        raise RecoveryError('task has no active allocation')
    robot, map_id, nodes_json, released, first_payload, first_state, status = row
    if not isinstance(robot, str) or not robot.startswith('sim-') or not robot[4:].strip():
        raise RecoveryError('only isolated simulation robots can be reconciled')
    _id(robot, 'robot_id'); _id(map_id, 'map_id')
    active = db.execute("SELECT value FROM allocation_meta WHERE key='active_map'").fetchone()
    fingerprint = db.execute("SELECT value FROM allocation_meta WHERE key='map_fingerprint'").fetchone()
    if active is None or active[0] != map_id or fingerprint is None:
        raise RecoveryError('active map is unavailable or inconsistent')
    digest = fingerprint[0]
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise RecoveryError('map fingerprint is invalid')
    try:
        nodes = json.loads(nodes_json)
    except (TypeError, ValueError) as exc:
        raise RecoveryError('stored route is invalid') from exc
    if not isinstance(nodes, list) or not nodes or any(not isinstance(n, str) or not n for n in nodes):
        raise RecoveryError('stored route is invalid')
    if len(set(nodes)) != len(nodes):
        raise RecoveryError('stored route is ambiguous')
    records = {1: (first_payload, first_state)}
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='progress_orders'").fetchone()
    if exists:
        for seq, payload, state in db.execute(
                'SELECT seq,payload,state FROM progress_orders WHERE task_id=? ORDER BY seq', (task_id,)):
            if type(seq) is not int or seq < 1:
                raise RecoveryError('stored order sequence is invalid')
            if seq == 1:
                if records[1] != (payload, state):
                    raise RecoveryError('first order histories disagree')
            elif seq in records:
                raise RecoveryError('duplicate order sequence')
            else:
                records[seq] = (payload, state)
    seqs = sorted(records)
    if seqs != list(range(1, len(seqs) + 1)):
        raise RecoveryError('stored order history is incomplete')
    parsed = []
    for seq in seqs:
        payload, state = records[seq]
        if state not in ('pending', 'uncertain', 'acked') or (state == 'pending' and seq != seqs[-1]):
            raise RecoveryError('stored order state is invalid')
        order = _decode(payload)
        if (order['order_id'] != task_id or type(order['seq']) is not int or order['seq'] != seq
                or order['map_id'] != map_id or order['nodes'] != nodes
                or order['released_until'] not in nodes or not isinstance(order['action_at_end'], dict)):
            raise RecoveryError('stored order and allocation disagree')
        if parsed and nodes.index(order['released_until']) <= nodes.index(parsed[-1][1]['released_until']):
            raise RecoveryError('release sequence does not advance')
        parsed.append((state, order, payload))
    if parsed[-1][1]['released_until'] != released:
        raise RecoveryError('latest order and released node disagree')
    sent = [entry for entry in parsed if entry[0] in ('uncertain', 'acked')]
    if not sent:
        raise RecoveryError('no possibly applied order exists')
    latest = sent[-1][1]
    occupancy = sorted(db.execute(
        'SELECT kind,resource_id FROM allocation_occupancy WHERE task_id=?', (task_id,)
    ).fetchall())
    if not occupancy or ('node', latest['released_until']) not in occupancy:
        raise RecoveryError('reported stopped node is not reserved')
    snapshot = {'robot_id': robot, 'map_id': map_id, 'map_fingerprint': digest,
                'route': nodes, 'released_until': released, 'task_status': status,
                'orders': [[seq, *records[seq]] for seq in seqs],
                'occupancy': [list(item) for item in occupancy]}
    return {'robot_id': robot, 'order_id': task_id, 'seq': latest['seq'], 'map_id': map_id,
            'stopped_at': latest['released_until'], 'order_digest': _digest_json(latest),
            'snapshot_digest': _digest_json(snapshot), 'map_fingerprint': digest}


class RecoveryGate:
    """Per-instance recovery epoch with short-lived, single-use challenges."""

    def __init__(self, path, *, clock=time.monotonic, ttl=30):
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or raw == ':memory:':
            raise ValueError('file-backed database path is required')
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or not 0 < ttl <= 300:
            raise ValueError('ttl must be finite and in (0, 300] seconds')
        if not callable(clock):
            raise ValueError('clock must be callable')
        self.path = os.path.normcase(os.path.realpath(raw))
        self._clock, self._ttl = clock, float(ttl)
        self._epoch = secrets.token_hex(32)
        self._initialized = False
        self._init_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._operation_lock = threading.RLock()
        self._local = threading.local()
        self._blocked = set()
        self._generations = {}
        self._token_generations = {}

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5, isolation_level=None)

    def _now(self):
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RecoveryError('monotonic clock is invalid')
        return float(value)

    def _block(self, task_id):
        with self._state_lock:
            generation = self._generations.get(task_id, 0) + 1
            self._generations[task_id] = generation
            self._blocked.add(task_id)
            for old, identity in list(self._token_generations.items()):
                if identity[0] == task_id:
                    del self._token_generations[old]
            return generation

    def initialize(self):
        """Start a new epoch; an older gate instance becomes invalid."""
        with self._operation_lock:
            return self._initialize()

    def _initialize(self):
        with self._init_lock:
            if self._initialized:
                return
            with closing(self._connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    required = {'tasks', 'allocation_jobs', 'allocation_outbox',
                                'allocation_occupancy', 'allocation_meta'}
                    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not required.issubset(names):
                        raise RecoveryError('allocation database must be initialized first')
                    db.execute('CREATE TABLE IF NOT EXISTS recovery_runtime '
                               '(singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch TEXT NOT NULL)')
                    db.execute('CREATE TABLE IF NOT EXISTS recovery_challenges '
                               '(task_id TEXT PRIMARY KEY, epoch TEXT NOT NULL, challenge TEXT NOT NULL, '
                               'robot_session TEXT NOT NULL, robot_id TEXT NOT NULL, order_id TEXT NOT NULL, '
                               'seq INTEGER NOT NULL, map_id TEXT NOT NULL, stopped_at TEXT NOT NULL, '
                               'order_digest TEXT NOT NULL, snapshot_digest TEXT NOT NULL, '
                               'expires REAL NOT NULL, status TEXT NOT NULL, map_fingerprint TEXT NOT NULL)')
                    db.execute('INSERT INTO recovery_runtime(singleton,epoch) VALUES (1,?) '
                               'ON CONFLICT(singleton) DO UPDATE SET epoch=excluded.epoch', (self._epoch,))
                    db.execute('COMMIT')
                except BaseException:
                    if db.in_transaction: db.execute('ROLLBACK')
                    raise
            self._initialized = True

    def _active(self, db):
        if not self._initialized:
            raise RecoveryError('recovery gate is not initialized')
        row = db.execute('SELECT epoch FROM recovery_runtime WHERE singleton=1').fetchone()
        if row is None or row[0] != self._epoch:
            raise RecoveryError('recovery epoch is no longer active')

    def begin(self, task_id, robot_session):
        """Replace any earlier proof with one newly issued challenge."""
        with self._operation_lock:
            if getattr(self._local, 'db', None) is not None:
                _id(task_id, 'task_id')
                self._block(task_id)
                raise RecoveryError('cannot begin inside a recovery transaction')
            return self._begin(task_id, robot_session)

    def _begin(self, task_id, robot_session):
        _id(task_id, 'task_id'); _id(robot_session, 'robot_session')
        token = secrets.token_hex(32)
        now = self._now()
        generation = self._block(task_id)
        failure = None
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._active(db)
                db.execute("UPDATE recovery_challenges SET status='revoked' WHERE task_id=?", (task_id,))
                try:
                    data = _snapshot(db, task_id)
                except Exception as exc:
                    failure = exc
                if failure is None:
                    db.execute('INSERT OR REPLACE INTO recovery_challenges VALUES '
                               '(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (task_id, self._epoch, token, robot_session, data['robot_id'], data['order_id'],
                                data['seq'], data['map_id'], data['stopped_at'], data['order_digest'],
                                data['snapshot_digest'], now + self._ttl, 'pending', data['map_fingerprint']))
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
        if failure is not None:
            raise failure
        with self._state_lock:
            self._token_generations[token] = (task_id, generation)
        return token

    def complete(self, task_id, robot_session, challenge, *, robot_id, order_id, seq,
                 map_id, stopped_at, order_digest, stopped):
        """Consume the matching challenge on any response; no physical effect."""
        with self._operation_lock:
            if getattr(self._local, 'db', None) is not None:
                _id(task_id, 'task_id')
                self._block(task_id)
                raise RecoveryError('cannot complete inside a recovery transaction')
            return self._complete(task_id, robot_session, challenge, robot_id=robot_id,
                                  order_id=order_id, seq=seq, map_id=map_id,
                                  stopped_at=stopped_at, order_digest=order_digest, stopped=stopped)

    def _complete(self, task_id, robot_session, challenge, *, robot_id, order_id, seq,
                  map_id, stopped_at, order_digest, stopped):
        _id(task_id, 'task_id'); _id(robot_session, 'robot_session'); _id(challenge, 'challenge')
        failure = None
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._active(db)
                row = db.execute('SELECT epoch,challenge,robot_session,robot_id,order_id,seq,map_id, '
                                 'stopped_at,order_digest,snapshot_digest,expires,status '
                                 'FROM recovery_challenges WHERE task_id=?', (task_id,)).fetchone()
                if row is None or row[0] != self._epoch or row[1] != challenge or row[11] != 'pending':
                    raise RecoveryError('challenge is unknown or already used')
                db.execute("UPDATE recovery_challenges SET status='consumed' WHERE task_id=?", (task_id,))
                try:
                    data = _snapshot(db, task_id)
                    if (self._now() >= row[10] or type(stopped) is not bool or stopped is not True
                            or type(seq) is not int or robot_session != row[2] or robot_id != row[3]
                            or order_id != row[4] or seq != row[5] or map_id != row[6]
                            or stopped_at != row[7] or order_digest != row[8]
                            or data['snapshot_digest'] != row[9]):
                        raise RecoveryError('recovery response does not match current challenge')
                    db.execute("UPDATE recovery_challenges SET status='verified' WHERE task_id=?", (task_id,))
                except (RecoveryError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
                    failure = RecoveryError('recovery response rejected')
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
        if failure is not None:
            with self._state_lock:
                self._token_generations.pop(challenge, None)
            raise failure
        with self._state_lock:
            identity = self._token_generations.pop(challenge, None)
            if identity == (task_id, self._generations.get(task_id)):
                self._blocked.discard(task_id)

    def invalidate(self, task_id):
        """Revoke a task's simulated proof after a disconnect or session change."""
        with self._operation_lock:
            if getattr(self._local, 'db', None) is not None:
                _id(task_id, 'task_id')
                self._block(task_id)
                raise RecoveryError('cannot invalidate inside a recovery transaction')
            return self._invalidate(task_id)

    def _invalidate(self, task_id):
        _id(task_id, 'task_id')
        self._block(task_id)
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._active(db)
                db.execute("UPDATE recovery_challenges SET status='revoked' WHERE task_id=?", (task_id,))
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise

    @contextmanager
    def transaction(self):
        """Serialize local invalidations with a SQLite write transaction.

        The gate lock is always acquired before the database lock. This only
        orders events observed by this instance, not real-world stopping.
        """
        with self._operation_lock:
            if getattr(self._local, 'db', None) is not None:
                raise RecoveryError('nested recovery transactions are not supported')
            with closing(self._connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    self._active(db)
                    self._local.db = db
                    yield db
                    db.execute('COMMIT')
                except BaseException:
                    if db.in_transaction:
                        db.execute('ROLLBACK')
                    raise
                finally:
                    self._local.db = None

    def require_reconciled(self, db, task_id, robot_session, *, expected_seq, expected_stopped_at):
        """Check proof in the caller's existing SQLite write transaction.

        Caller must use this gate's transaction(), independently verify the
        allocation and occupancy, and invalidate on disconnect.
        """
        _id(task_id, 'task_id'); _id(robot_session, 'robot_session')
        _id(expected_stopped_at, 'expected_stopped_at')
        if type(expected_seq) is not int or expected_seq < 1:
            raise RecoveryError('expected sequence must be a positive integer')
        if not isinstance(db, sqlite3.Connection) or not db.in_transaction or getattr(self._local, 'db', None) is not db:
            raise RecoveryError('the gate transaction is required')
        with self._state_lock:
            if task_id in self._blocked:
                raise RecoveryError('simulated recovery proof is blocked')
        database = db.execute('PRAGMA database_list').fetchall()
        main_path = next((row[2] for row in database if row[1] == 'main'), None)
        if not main_path or os.path.normcase(os.path.realpath(main_path)) != self.path:
            raise RecoveryError('recovery proof must use the same database')
        self._active(db)
        row = db.execute('SELECT c.epoch,c.robot_session,c.seq,c.stopped_at,c.expires,c.status, '
                         'c.robot_id,c.map_id,j.robot_id,j.map_id,t.status,m.value, '
                         'c.order_id,c.order_digest,c.map_fingerprint, '
                         "(SELECT value FROM allocation_meta WHERE key='map_fingerprint') "
                         'FROM recovery_challenges c JOIN allocation_jobs j ON j.task_id=c.task_id '
                         'JOIN tasks t ON t.task_id=c.task_id '
                         "LEFT JOIN allocation_meta m ON m.key='active_map' WHERE c.task_id=?",
                         (task_id,)).fetchone()
        if (row is None or row[0] != self._epoch or row[1] != robot_session or row[2] != expected_seq
                or row[3] != expected_stopped_at or self._now() >= row[4] or row[5] != 'verified'
                or row[6] != row[8] or row[7] != row[9] or row[10] != 'ASSIGNED' or row[11] != row[7]
                or row[12] != task_id or row[14] != row[15]):
            raise RecoveryError('current simulated recovery proof is unavailable')
        if expected_seq == 1:
            record = db.execute('SELECT payload,state FROM allocation_outbox WHERE task_id=?', (task_id,)).fetchone()
        else:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='progress_orders'").fetchone():
                raise RecoveryError('proven order is unavailable')
            record = db.execute('SELECT payload,state FROM progress_orders WHERE task_id=? AND seq=?',
                                (task_id, expected_seq)).fetchone()
        if record is None or record[1] not in ('uncertain', 'acked'):
            raise RecoveryError('proven order is unavailable')
        try:
            order = _decode(record[0])
            if (order['order_id'] != task_id or type(order['seq']) is not int
                    or order['seq'] != expected_seq or order['map_id'] != row[7]
                    or order['released_until'] != expected_stopped_at or _digest_json(order) != row[13]):
                raise RecoveryError('proven order has changed')
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, RecoveryError):
                raise
            raise RecoveryError('proven order is invalid') from exc
        if not db.execute('SELECT 1 FROM allocation_occupancy WHERE task_id=? '
                          "AND kind='node' AND resource_id=?", (task_id, expected_stopped_at)).fetchone():
            raise RecoveryError('proven stopped node is no longer reserved')
        with self._state_lock:
            if task_id in self._blocked:
                raise RecoveryError('simulated recovery proof is blocked')
        return True
