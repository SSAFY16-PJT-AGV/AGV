"""Durable pickup stock observations for an isolated Unreal simulation.

The authenticated caller supplies the world identity, snapshots, and correlated
action replies. This module does not authenticate Unreal or prove physical
inventory. A stored count is never fresh after restarting the process.
"""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading


class PickupUnavailable(RuntimeError):
    """A fresh, unreserved simulated pickup item cannot be proven."""


def _text(value, label):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid ' + label)
    return value


class PickupInventory:
    """Snapshot and reservations are transactional within this separate DB.

    Call only under the application's existing isolated simulation process
    lock. A caller must reserve before dispatch, record `mark_dispatched`
    before sending a grab, and never release a dispatched uncertain action.
    Transactions across this DB and the order journal are not atomic.
    """

    def __init__(self, path, sources, *, _mode=None, _expected_creation_id=None):
        if _mode not in ('new', 'existing'):
            raise ValueError('use create_new or open_existing for pickup inventory')
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or raw == ':memory:':
            raise ValueError('file-backed pickup inventory required')
        if not isinstance(sources, dict) or not sources or len(sources) > 128:
            raise ValueError('explicit bounded pickup sources required')
        self.sources = {_text(k, 'source'): _text(v, 'product') for k, v in sources.items()}
        if _mode == 'existing' and (os.path.islink(raw) or not os.path.isfile(raw)):
            raise ValueError('existing regular pickup database required')
        self.path = os.path.realpath(raw)
        self._source_json = json.dumps(self.sources, sort_keys=True, separators=(',', ':'))
        self._lock = threading.RLock()
        self._session = None
        self._fresh_revision = None
        self._pending_snapshot = None
        if _mode == 'existing':
            if not isinstance(_expected_creation_id, str) or re.fullmatch('[0-9a-f]{64}', _expected_creation_id) is None:
                raise ValueError('independent pickup creation identity required')
            self._creation_id = _expected_creation_id
            created = False
        else:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            created = True
            self._creation_id = secrets.token_hex(32)
        try:
            info = os.stat(self.path)
            self._creation_file = (info.st_dev, info.st_ino)
        except OSError as exc:
            raise ValueError('pickup database unavailable') from exc
        self._schema_initialized = not created
        try:
            if created:
                with self._write() as db:
                    db.execute('PRAGMA application_id=1095194192')
                    db.execute('PRAGMA user_version=1')
                    db.execute('CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                               'sources TEXT NOT NULL, world_id TEXT, session_id TEXT, revision INTEGER NOT NULL, '
                               'mutation INTEGER NOT NULL, creation_id TEXT NOT NULL)')
                    db.execute('INSERT INTO metadata VALUES (1,?,NULL,NULL,0,0,?)',
                               (self._source_json, self._creation_id))
                    db.execute('CREATE TABLE counts (source_id TEXT PRIMARY KEY, available INTEGER)')
                    db.executemany('INSERT INTO counts VALUES (?,NULL)', [(s,) for s in self.sources])
                    db.execute('CREATE TABLE reservations (reservation_id TEXT PRIMARY KEY, '
                               'request_id TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL REFERENCES counts(source_id), '
                               'product_type TEXT NOT NULL, state TEXT NOT NULL '
                               "CHECK(state IN ('reserved','inflight','unknown','consumed','released')), "
                               'order_id TEXT, action_id TEXT UNIQUE, agv_id TEXT, session_id TEXT, '
                               'dispatch_revision INTEGER)')
                self._schema_initialized = True
            else:
                self._validate()
        except sqlite3.Error as exc:
            raise ValueError('pickup inventory unavailable or corrupt') from exc

    @classmethod
    def create_new(cls, path, sources):
        return cls(path, sources, _mode='new')

    @classmethod
    def open_existing(cls, path, sources, *, expected_creation_id):
        return cls(path, sources, _mode='existing', _expected_creation_id=expected_creation_id)

    @property
    def creation_id(self):
        return self._creation_id

    def _check_file(self):
        try:
            if os.path.islink(self.path) or not os.path.isfile(self.path):
                raise ValueError('regular pickup database required')
            info = os.stat(self.path)
        except OSError as exc:
            raise ValueError('pickup database unavailable') from exc
        if (info.st_dev, info.st_ino) != self._creation_file:
            raise ValueError('pickup database file was replaced')

    def _connect(self):
        self._check_file()
        db = sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=rw',
                             uri=True, timeout=5, isolation_level=None)
        try:
            self._check_file()
            if self._schema_initialized:
                rows = db.execute('SELECT creation_id FROM metadata WHERE singleton=1').fetchall()
                if rows != [(self._creation_id,)]:
                    raise ValueError('pickup database identity differs')
            return db
        except BaseException:
            db.close()
            raise

    @contextmanager
    def _write(self):
        with self._lock, closing(self._connect()) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _validate(self):
        with closing(self._connect()) as db:
            if (db.execute('PRAGMA application_id').fetchone() != (1095194192,)
                    or db.execute('PRAGMA user_version').fetchone() != (1,)
                    or db.execute('PRAGMA quick_check').fetchall() != [('ok',)]
                    or db.execute('PRAGMA foreign_key_check').fetchone() is not None):
                raise ValueError('pickup database integrity differs')
            row = db.execute('SELECT sources,world_id,revision FROM metadata WHERE singleton=1').fetchone()
            if (row is None or row[0] != self._source_json or type(row[2]) is not int or row[2] < 0
                    or (row[1] is not None and re.fullmatch('[0-9a-f]{32}', row[1]) is None)
                    or {r[0] for r in db.execute('SELECT source_id FROM counts')} != set(self.sources)):
                raise ValueError('pickup database source or world differs')
            bad = db.execute("SELECT 1 FROM reservations WHERE state NOT IN "
                             "('reserved','inflight','unknown','consumed','released') LIMIT 1").fetchone()
            if bad:
                raise ValueError('pickup reservation state invalid')

    def _active(self, db, token, *, fresh=False):
        if not isinstance(token, str) or token != self._session:
            raise PickupUnavailable('pickup session is not current')
        row = db.execute('SELECT session_id,revision FROM metadata WHERE singleton=1').fetchone()
        if row is None or row[0] != token:
            raise PickupUnavailable('pickup session was invalidated')
        if fresh and (self._fresh_revision is None or self._fresh_revision != row[1]):
            raise PickupUnavailable('fresh pickup snapshot required')
        return row[1]

    def begin_session(self, world_id):
        if not isinstance(world_id, str) or re.fullmatch('[0-9a-fA-F]{32}', world_id) is None:
            raise ValueError('valid Unreal world identity required')
        world_id = world_id.lower()
        token = secrets.token_hex(32)
        with self._write() as db:
            old = db.execute('SELECT world_id FROM metadata WHERE singleton=1').fetchone()
            if old is None or old[0] not in (None, world_id):
                raise ValueError('Unreal world differs from saved pickup inventory')
            db.execute("UPDATE reservations SET state='unknown' WHERE state='inflight'")
            db.execute('UPDATE metadata SET world_id=?,session_id=? WHERE singleton=1', (world_id, token))
            self._session = token
            self._fresh_revision = None
            self._pending_snapshot = None
        return token

    def end_session(self, token):
        with self._write() as db:
            self._active(db, token)
            db.execute("UPDATE reservations SET state='unknown' WHERE state='inflight' AND session_id=?", (token,))
            db.execute('UPDATE metadata SET session_id=NULL WHERE singleton=1')
            self._session = None
            self._fresh_revision = None
            self._pending_snapshot = None

    def begin_snapshot(self, token):
        """Capture a one-use fence immediately before sending a UE request."""
        with self._write() as db:
            revision = self._active(db, token)
            mutation = db.execute('SELECT mutation FROM metadata WHERE singleton=1').fetchone()[0]
            nonce = secrets.token_hex(32)
            self._pending_snapshot = (nonce, revision, mutation)
            return nonce

    def apply_snapshot(self, token, pickups, *, snapshot_fence):
        if not isinstance(pickups, list) or len(pickups) != len(self.sources):
            raise ValueError('complete pickup snapshot required')
        parsed = {}
        for item in pickups:
            if not isinstance(item, dict) or set(item) != {'source_id', 'product_type', 'available'}:
                raise ValueError('invalid pickup snapshot')
            source, kind, count = item['source_id'], item['product_type'], item['available']
            if (not isinstance(source, str) or source not in self.sources or source in parsed
                    or kind != self.sources[source] or type(count) is not int or not 0 <= count <= 1000000):
                raise ValueError('pickup source, product or quantity differs')
            parsed[source] = count
        if set(parsed) != set(self.sources):
            raise ValueError('pickup snapshot is incomplete')
        with self._write() as db:
            revision = self._active(db, token)
            mutation = db.execute('SELECT mutation FROM metadata WHERE singleton=1').fetchone()[0]
            pending = self._pending_snapshot
            if (not isinstance(snapshot_fence, str) or pending is None
                    or pending != (snapshot_fence, revision, mutation)):
                self._pending_snapshot = None
                raise PickupUnavailable('pickup snapshot request is stale or already consumed')
            revision += 1
            db.executemany('UPDATE counts SET available=? WHERE source_id=?',
                           [(count, source) for source, count in parsed.items()])
            db.execute('UPDATE metadata SET revision=? WHERE singleton=1', (revision,))
            self._fresh_revision = revision
            self._pending_snapshot = None
        return revision

    def reserve(self, token, request_id, source_id, product_type):
        _text(request_id, 'request'); _text(source_id, 'source'); _text(product_type, 'product')
        if self.sources.get(source_id) != product_type:
            raise ValueError('pickup source and product differ')
        with self._write() as db:
            self._active(db, token, fresh=True)
            existing = db.execute('SELECT reservation_id,source_id,product_type,state FROM reservations '
                                  'WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing[1:3] != (source_id, product_type):
                    raise ValueError('request already belongs to another pickup source')
                return {'reservation_id': existing[0], 'request_id': request_id,
                        'source_id': source_id, 'product_type': product_type, 'state': existing[3]}
            count = db.execute('SELECT available FROM counts WHERE source_id=?', (source_id,)).fetchone()[0]
            held = db.execute("SELECT COUNT(*) FROM reservations WHERE source_id=? AND state IN "
                              "('reserved','inflight','unknown')", (source_id,)).fetchone()[0]
            if type(count) is not int or count <= held:
                raise PickupUnavailable('no confirmed unreserved pickup stock')
            rid = secrets.token_hex(16)
            db.execute("INSERT INTO reservations(reservation_id,request_id,source_id,product_type,state) "
                       "VALUES (?,?,?,?,'reserved')", (rid, request_id, source_id, product_type))
            return {'reservation_id': rid, 'request_id': request_id, 'source_id': source_id,
                    'product_type': product_type, 'state': 'reserved'}

    def mark_dispatched(self, token, reservation_id, order_id, action_id, agv_id):
        for value, label in ((reservation_id, 'reservation'), (order_id, 'order'),
                             (action_id, 'action'), (agv_id, 'AGV')):
            _text(value, label)
        try:
            with self._write() as db:
                revision = self._active(db, token, fresh=True)
                row = db.execute("SELECT source_id FROM reservations WHERE reservation_id=? AND state='reserved'",
                                 (reservation_id,)).fetchone()
                if row is None:
                    raise ValueError('pickup reservation is not dispatchable')
                available = db.execute('SELECT available FROM counts WHERE source_id=?', row).fetchone()[0]
                held = db.execute("SELECT COUNT(*) FROM reservations WHERE source_id=? AND state IN "
                                  "('reserved','inflight','unknown')", row).fetchone()[0]
                if type(available) is not int or available < held:
                    raise PickupUnavailable('current pickup stock cannot cover held reservations')
                changed = db.execute("UPDATE reservations SET state='inflight',order_id=?,action_id=?,agv_id=?,"
                                     "session_id=?,dispatch_revision=? WHERE reservation_id=? AND state='reserved'",
                                     (order_id, action_id, agv_id, token, revision, reservation_id)).rowcount
                if changed != 1:
                    raise ValueError('pickup reservation is not dispatchable')
        except sqlite3.IntegrityError as exc:
            raise ValueError('pickup action was already dispatched') from exc

    def confirm(self, token, reservation_id, order_id, action_id, agv_id, *, applied):
        if type(applied) is not bool:
            raise ValueError('explicit action result required')
        uncertain = False
        with self._write() as db:
            revision = self._active(db, token)
            row = db.execute('SELECT source_id,state,order_id,action_id,agv_id,session_id,dispatch_revision '
                             'FROM reservations WHERE reservation_id=?', (reservation_id,)).fetchone()
            if row is None or row[2:6] != (order_id, action_id, agv_id, token):
                raise ValueError('pickup acknowledgement identity differs')
            if row[1] == ('consumed' if applied else 'released'):
                return
            if row[1] != 'inflight':
                raise ValueError('pickup result requires an in-flight action')
            if applied and (row[6] != revision or self._fresh_revision != revision
                            or db.execute('SELECT available FROM counts WHERE source_id=?', (row[0],)).fetchone()[0] < 1):
                db.execute("UPDATE reservations SET state='unknown' WHERE reservation_id=?", (reservation_id,))
                self._fresh_revision = None
                uncertain = True
            elif applied:
                db.execute('UPDATE counts SET available=available-1 WHERE source_id=?', (row[0],))
                db.execute("UPDATE reservations SET state='consumed' WHERE reservation_id=?", (reservation_id,))
                db.execute('UPDATE metadata SET mutation=mutation+1 WHERE singleton=1')
            else:
                db.execute("UPDATE reservations SET state='released' WHERE reservation_id=?", (reservation_id,))
                # A rejected grab may mean the previously observed stock is no
                # longer there; do not reuse that count without a fresh UE view.
                self._fresh_revision = None
                self._pending_snapshot = None
        if uncertain:
            raise PickupUnavailable('pickup snapshot changed before the correlated result')

    def cancel_reserved(self, token, reservation_id):
        """Release only an action which was definitely never dispatched."""
        with self._write() as db:
            self._active(db, token)
            if db.execute("UPDATE reservations SET state='released' WHERE reservation_id=? AND state='reserved'",
                          (reservation_id,)).rowcount != 1:
                raise ValueError('only an undispatched pickup can be cancelled')

    def mark_unknown(self, token, reservation_id):
        """Keep a sent grab reserved when its result was lost or ambiguous."""
        with self._write() as db:
            self._active(db, token)
            row = db.execute('SELECT state,session_id FROM reservations WHERE reservation_id=?',
                             (reservation_id,)).fetchone()
            if row == ('unknown', token):
                return
            if row != ('inflight', token):
                raise ValueError('only an in-flight pickup can become unknown')
            db.execute("UPDATE reservations SET state='unknown' WHERE reservation_id=?", (reservation_id,))
            self._fresh_revision = None
            self._pending_snapshot = None

    def snapshot(self):
        with self._lock, closing(self._connect()) as db:
            db.execute('BEGIN')
            world, token, revision = db.execute('SELECT world_id,session_id,revision FROM metadata WHERE singleton=1').fetchone()
            fresh = bool(self._session and self._session == token and self._fresh_revision == revision)
            counts = dict(db.execute('SELECT source_id,available FROM counts'))
            rows = db.execute('SELECT reservation_id,request_id,source_id,product_type,state,order_id,action_id,agv_id '
                              'FROM reservations ORDER BY rowid').fetchall()
            reservations = [dict(zip(('reservation_id','request_id','source_id','product_type','state',
                                      'order_id','action_id','agv_id'), row)) for row in rows]
            sources = {}
            for source, kind in self.sources.items():
                held = sum(row[2] == source and row[4] in ('reserved','inflight','unknown') for row in rows)
                value = counts[source]
                sources[source] = {'product_type': kind, 'available': value, 'reserved': held,
                                   'allocatable': max(0, value - held) if fresh and type(value) is int else None}
            return {'world_id': world, 'fresh': fresh, 'revision': revision,
                    'sources': sources, 'reservations': reservations}
