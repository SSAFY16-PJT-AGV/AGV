"""Slot-aware order reservations for the team's v3 *simulated* shelf layout.

This independent journal does not send commands or change physical reservations.
Observations and acknowledgements must come from the explicitly bound simulation
session; they do not establish real inventory, a physical stop or safety.
"""
from contextlib import closing, contextmanager
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading


PRODUCT_SHELVES = {'milk': 'N02', 'tissue': 'N03', 'egg': 'N04', 'juice': 'N05'}
PRODUCT_MARKERS = {'milk': 30, 'tissue': 31, 'egg': 32, 'juice': 33}
_TERMINAL = ('completed', 'failed')


class SlotUnavailable(RuntimeError):
    """Fresh, unclaimed source and destination slots are required."""


def _text(value, label):
    if (type(value) is not str or not value or value != value.strip() or len(value) > 128
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid ' + label)
    return value


def slot_layout():
    """Team marker IDs are one-based, unlike the old UE zero-based indices."""
    return tuple({'shelf_id': 'N{:02d}'.format(shelf), 'slot_id': 4 * (shelf - 1) + index,
                  'slot_index': index - 1, 'marker_id': 4 * (shelf - 1) + index,
                  'stop_node': 'D{:02d}'.format(shelf)}
                 for shelf in range(1, 7) for index in range(1, 5))


class TeamShelfOrderSlots:
    """Atomic pick/place slot claims for one bounded synthetic world.

    The caller must mark dispatch before invoking a simulated action. Any
    session loss or process restart turns unfinished work into ``unknown``;
    both claims remain held. This journal is not connected to physical MQTT.
    """

    def __init__(self, path, *, create, expected_creation_id=None):
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or raw == ':memory:':
            raise ValueError('a file-backed simulation journal is required')
        self.path = os.path.abspath(raw)
        if expected_creation_id is not None and (not isinstance(expected_creation_id, str)
                or re.fullmatch(r'[0-9a-f]{64}', expected_creation_id) is None):
            raise ValueError('invalid simulated slot journal identity')
        if create and expected_creation_id is not None:
            raise ValueError('new journal cannot have an expected identity')
        self._lock = threading.RLock()
        self._session = self._snapshot = None
        self._fresh = self._admission_ready = False
        if create:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            stat = os.stat(self.path)
            self._file_identity = (stat.st_dev, stat.st_ino)
            with self._write() as db:
                db.execute('PRAGMA application_id=1095194691')
                db.execute('PRAGMA user_version=2')
                db.execute('CREATE TABLE identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                           'creation_id TEXT NOT NULL UNIQUE)')
                self._creation_id = secrets.token_hex(32)
                db.execute('INSERT INTO identity VALUES (1,?)', (self._creation_id,))
                db.execute('CREATE TABLE meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                           'world_id TEXT, session TEXT, mutation INTEGER NOT NULL)')
                db.execute('INSERT INTO meta VALUES (1,NULL,NULL,0)')
                db.execute('CREATE TABLE slots (slot_id INTEGER PRIMARY KEY, shelf_id TEXT NOT NULL, '
                           'slot_index INTEGER NOT NULL, marker_id INTEGER NOT NULL UNIQUE, '
                           'state TEXT NOT NULL, product_type TEXT)')
                db.executemany('INSERT INTO slots VALUES (?,?,?,?,?,NULL)',
                               [(x['slot_id'], x['shelf_id'], x['slot_index'], x['marker_id'], 'unknown')
                                for x in slot_layout()])
                db.execute('CREATE TABLE orders (request_id TEXT PRIMARY KEY, product_type TEXT NOT NULL, '
                           'source_slot_id INTEGER NOT NULL REFERENCES slots(slot_id), '
                           'destination_slot_id INTEGER NOT NULL REFERENCES slots(slot_id), '
                           'state TEXT NOT NULL)')
                db.execute('CREATE TABLE claims (slot_id INTEGER PRIMARY KEY REFERENCES slots(slot_id), '
                           'request_id TEXT NOT NULL REFERENCES orders(request_id))')
                db.execute('CREATE TABLE actions (action_id TEXT PRIMARY KEY, request_id TEXT NOT NULL '
                           'REFERENCES orders(request_id), phase TEXT NOT NULL, session TEXT NOT NULL, '
                           'state TEXT NOT NULL)')
        else:
            if os.path.islink(self.path) or not os.path.isfile(self.path):
                raise ValueError('existing regular simulation journal required')
            stat = os.stat(self.path)
            self._file_identity = (stat.st_dev, stat.st_ino)
            with self._write() as db:
                version = db.execute('PRAGMA user_version').fetchone()
                if (db.execute('PRAGMA application_id').fetchone() != (1095194691,)
                        or version not in ((1,), (2,))
                        or db.execute('PRAGMA quick_check').fetchall() != [('ok',)]
                        or db.execute('PRAGMA foreign_key_check').fetchone() is not None):
                    raise ValueError('invalid simulated slot journal')
                if version == (1,):
                    if expected_creation_id is not None:
                        raise ValueError('legacy journal has no bound identity')
                    db.execute('CREATE TABLE identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                               'creation_id TEXT NOT NULL UNIQUE)')
                    db.execute('INSERT INTO identity VALUES (1,?)', (secrets.token_hex(32),))
                    db.execute('PRAGMA user_version=2')
                identities = db.execute('SELECT creation_id FROM identity WHERE singleton=1').fetchall()
                if (len(identities) != 1 or not isinstance(identities[0][0], str)
                        or re.fullmatch(r'[0-9a-f]{64}', identities[0][0]) is None
                        or (expected_creation_id is not None and identities[0][0] != expected_creation_id)):
                    raise ValueError('simulated slot journal identity differs')
                self._creation_id = identities[0][0]
                actual = db.execute('SELECT slot_id,shelf_id,slot_index,marker_id FROM slots ORDER BY slot_id').fetchall()
                expected = [(x['slot_id'], x['shelf_id'], x['slot_index'], x['marker_id']) for x in slot_layout()]
                if actual != expected:
                    raise ValueError('simulated slot layout differs')
                self._integrity(db)
                db.execute("UPDATE orders SET state='unknown' WHERE state NOT IN ('completed','failed')")
                db.execute("UPDATE actions SET state='unknown' WHERE state='inflight'")
                db.execute('UPDATE meta SET session=NULL,mutation=mutation+1 WHERE singleton=1')

    @classmethod
    def create_new(cls, path):
        return cls(path, create=True)

    @classmethod
    def open_existing(cls, path, *, expected_creation_id=None):
        return cls(path, create=False, expected_creation_id=expected_creation_id)

    @property
    def creation_id(self):
        with self._write() as db:
            row = db.execute('SELECT creation_id FROM identity WHERE singleton=1').fetchone()
            if row != (self._creation_id,):
                raise ValueError('simulated slot journal identity changed')
            return self._creation_id

    @contextmanager
    def _write(self):
        with self._lock:
            if os.path.islink(self.path) or not os.path.isfile(self.path):
                raise ValueError('simulation journal unavailable')
            stat = os.stat(self.path)
            if (stat.st_dev, stat.st_ino) != self._file_identity:
                raise ValueError('simulation journal replaced')
            db = sqlite3.connect(Path(self.path).as_uri() + '?mode=rw', uri=True,
                                 timeout=5, isolation_level=None)
            with closing(db):
                stat = os.stat(self.path)
                if (stat.st_dev, stat.st_ino) != self._file_identity:
                    raise ValueError('simulation journal replaced')
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                try:
                    yield db
                    db.execute('COMMIT')
                except BaseException:
                    if db.in_transaction:
                        db.execute('ROLLBACK')
                    raise

    def _integrity(self, db):
        if (db.execute('PRAGMA application_id').fetchone() != (1095194691,)
                or db.execute('PRAGMA user_version').fetchone() != (2,)
                or db.execute('SELECT creation_id FROM identity WHERE singleton=1').fetchone() != (self._creation_id,)):
            raise ValueError('simulated slot journal identity changed')
        rows = db.execute('SELECT slot_id,shelf_id,slot_index,marker_id,state,product_type '
                          'FROM slots ORDER BY slot_id').fetchall()
        if len(rows) != 24:
            raise ValueError('simulated slot count differs')
        expected_layout = [(x['slot_id'], x['shelf_id'], x['slot_index'], x['marker_id']) for x in slot_layout()]
        if [r[:4] for r in rows] != expected_layout:
            raise ValueError('simulated slot layout differs')
        for _, _, _, _, state, product in rows:
            if (state not in ('unknown', 'empty', 'occupied')
                    or (state == 'occupied' and product not in PRODUCT_SHELVES)
                    or (state != 'occupied' and product is not None)):
                raise ValueError('simulated slot observation invalid')
        expected = set()
        for request, product, source, destination, state in db.execute(
                'SELECT request_id,product_type,source_slot_id,destination_slot_id,state FROM orders'):
            if (product not in PRODUCT_SHELVES or state not in
                    ('reserved', 'picking', 'carried', 'placing', 'unknown', 'completed', 'failed')
                    or not 1 <= source <= 4 or not 1 <= destination <= 24
                    or (destination - 1) // 4 + 1 != int(PRODUCT_SHELVES[product][1:])):
                raise ValueError('simulated order metadata invalid')
            if state not in _TERMINAL:
                expected.update(((source, request), (destination, request)))
        actual = set(db.execute('SELECT slot_id,request_id FROM claims'))
        if actual != expected:
            raise ValueError('simulated slot claims differ from orders')

    @staticmethod
    def _change(db):
        db.execute('UPDATE meta SET mutation=mutation+1 WHERE singleton=1')

    def _active(self, db, session, *, fresh=False):
        self._integrity(db)
        row = db.execute('SELECT session,mutation FROM meta WHERE singleton=1').fetchone()
        if row is None or session is None or session != self._session or session != row[0]:
            raise ValueError('simulation session is not active')
        if fresh and not self._fresh:
            raise SlotUnavailable('fresh simulated slot snapshot required')
        return row[1]

    def begin_session(self, world_id):
        world_id = _text(world_id, 'world ID')
        with self._write() as db:
            row = db.execute('SELECT world_id FROM meta WHERE singleton=1').fetchone()
            if row is None or row[0] not in (None, world_id):
                raise ValueError('simulation world differs')
            db.execute("UPDATE orders SET state='unknown' WHERE state NOT IN ('completed','failed')")
            db.execute("UPDATE actions SET state='unknown' WHERE state='inflight'")
            session = secrets.token_hex(32)
            db.execute('UPDATE meta SET world_id=?,session=?,mutation=mutation+1 WHERE singleton=1',
                       (world_id, session))
            self._session, self._snapshot, self._fresh, self._admission_ready = session, None, False, False
            return session

    def end_session(self, session):
        with self._write() as db:
            self._active(db, session)
            db.execute("UPDATE orders SET state='unknown' WHERE state NOT IN ('completed','failed')")
            db.execute("UPDATE actions SET state='unknown' WHERE state='inflight'")
            db.execute('UPDATE meta SET session=NULL,mutation=mutation+1 WHERE singleton=1')
            self._session, self._snapshot, self._fresh, self._admission_ready = None, None, False, False

    def begin_snapshot(self, session, *, invalidate=False):
        if type(invalidate) is not bool:
            raise ValueError('snapshot invalidation must be explicit')
        with self._write() as db:
            mutation = self._active(db, session)
            nonce = secrets.token_hex(32)
            self._snapshot = (session, nonce, mutation)
            if invalidate:
                # Do not block an acknowledgement for an action already sent.
                # New work waits until a complete fenced snapshot is applied.
                self._admission_ready = False
            return nonce

    def apply_snapshot(self, session, slots, *, fence):
        if not isinstance(slots, list) or len(slots) != 24:
            raise ValueError('all 24 simulated slots are required')
        parsed = {}
        for row in slots:
            if (not isinstance(row, dict) or set(row) != {'slot_id', 'state', 'product_type'}
                    or type(row['slot_id']) is not int or not 1 <= row['slot_id'] <= 24
                    or row['slot_id'] in parsed or row['state'] not in ('unknown', 'empty', 'occupied')
                    or (row['state'] == 'occupied' and
                        (type(row['product_type']) is not str or row['product_type'] not in PRODUCT_SHELVES))
                    or (row['state'] != 'occupied' and row['product_type'] is not None)):
                raise ValueError('invalid simulated slot observation')
            parsed[row['slot_id']] = (row['state'], row['product_type'])
        with self._write() as db:
            mutation = self._active(db, session)
            if self._snapshot != (session, fence, mutation):
                raise ValueError('stale simulated slot snapshot')
            held = {r[0] for r in db.execute('SELECT slot_id FROM claims')}
            for slot_id, (state, product) in parsed.items():
                # A claimed slot with an uncertain action is not inferred from
                # a later snapshot. Its claim stays in place until recovery.
                if slot_id in held:
                    state, product = 'unknown', None
                db.execute('UPDATE slots SET state=?,product_type=? WHERE slot_id=?', (state, product, slot_id))
            self._change(db)
            self._snapshot, self._fresh, self._admission_ready = None, True, True

    def _admission(self):
        if not self._admission_ready:
            raise SlotUnavailable('fresh simulated slot observation required before new work')

    def reserve(self, session, request_id, product_type):
        request_id = _text(request_id, 'request ID')
        if type(product_type) is not str or product_type not in PRODUCT_SHELVES:
            raise ValueError('unknown simulated product')
        with self._write() as db:
            self._active(db, session, fresh=True)
            self._admission()
            existing = db.execute('SELECT product_type FROM orders WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing[0] != product_type:
                    raise ValueError('request ID reused for a different product')
                return self._order(db, request_id)
            source = db.execute("SELECT s.slot_id FROM slots s LEFT JOIN claims c ON s.slot_id=c.slot_id "
                                "WHERE s.shelf_id='N01' AND s.state='occupied' AND s.product_type=? "
                                'AND c.slot_id IS NULL ORDER BY s.slot_id LIMIT 1', (product_type,)).fetchone()
            dest = db.execute('SELECT s.slot_id FROM slots s LEFT JOIN claims c ON s.slot_id=c.slot_id '
                              "WHERE s.shelf_id=? AND s.state='empty' AND c.slot_id IS NULL "
                              'ORDER BY s.slot_id LIMIT 1', (PRODUCT_SHELVES[product_type],)).fetchone()
            if source is None or dest is None:
                raise SlotUnavailable('no confirmed unclaimed source or destination slot')
            db.execute('INSERT INTO orders VALUES (?,?,?,?,?)', (request_id, product_type, source[0], dest[0], 'reserved'))
            db.executemany('INSERT INTO claims VALUES (?,?)', [(source[0], request_id), (dest[0], request_id)])
            self._change(db)
            return self._order(db, request_id)

    def mark_dispatched(self, session, request_id, phase, action_id):
        _text(request_id, 'request ID')
        _text(action_id, 'action ID')
        if phase not in ('pick', 'place'):
            raise ValueError('unknown action')
        with self._write() as db:
            self._active(db, session, fresh=True)
            self._admission()
            row = db.execute('SELECT state FROM orders WHERE request_id=?', (request_id,)).fetchone()
            if row != (('reserved' if phase == 'pick' else 'carried'),):
                raise ValueError('order is not ready for that action')
            try:
                db.execute('INSERT INTO actions VALUES (?,?,?,?,?)', (action_id, request_id, phase, session, 'inflight'))
            except sqlite3.IntegrityError as exc:
                raise ValueError('duplicate action ID') from exc
            db.execute('UPDATE orders SET state=? WHERE request_id=?',
                       ('picking' if phase == 'pick' else 'placing', request_id))
            self._change(db)

    def begin_navigation(self, session, request_id, task_id):
        """Durably mark the one allowed simulated navigation publish attempt."""
        _text(request_id, 'request ID')
        _text(task_id, 'task ID')
        action_id = 'nav:' + task_id
        _text(action_id, 'navigation action ID')
        with self._write() as db:
            self._active(db, session, fresh=True)
            self._admission()
            if self._order(db, request_id)['state'] != 'reserved':
                raise ValueError('slot reservation is not ready for navigation')
            if db.execute("SELECT 1 FROM actions WHERE request_id=? AND phase='navigate'", (request_id,)).fetchone():
                raise ValueError('navigation has already been attempted')
            try:
                db.execute('INSERT INTO actions VALUES (?,?,?,?,?)',
                           (action_id, request_id, 'navigate', session, 'inflight'))
            except sqlite3.IntegrityError as exc:
                raise ValueError('navigation task is already bound') from exc
            self._change(db)

    def finish_navigation(self, session, request_id, task_id, *, published):
        """Record broker acknowledgement only; never treat it as arrival."""
        _text(request_id, 'request ID')
        _text(task_id, 'task ID')
        if type(published) is not bool:
            raise ValueError('explicit navigation publication outcome required')
        action_id = 'nav:' + task_id
        with self._write() as db:
            self._active(db, session)
            row = db.execute('SELECT phase,session,state FROM actions WHERE action_id=? AND request_id=?',
                             (action_id, request_id)).fetchone()
            if row != ('navigate', session, 'inflight') or self._order(db, request_id)['state'] != 'reserved':
                raise ValueError('navigation result is stale or uncorrelated')
            db.execute('UPDATE actions SET state=? WHERE action_id=?',
                       ('published' if published else 'unknown', action_id))
            if not published:
                db.execute("UPDATE orders SET state='unknown' WHERE request_id=?", (request_id,))
            self._change(db)

    def complete_action(self, session, request_id, action_id, applied):
        _text(request_id, 'request ID')
        _text(action_id, 'action ID')
        if type(applied) is not bool:
            raise ValueError('explicit action result required')
        with self._write() as db:
            self._active(db, session, fresh=True)
            action = db.execute('SELECT phase,state,session FROM actions WHERE action_id=? AND request_id=?',
                                (action_id, request_id)).fetchone()
            order = self._order(db, request_id)
            if (not action or action[1:] != ('inflight', session)
                    or order['state'] != ('picking' if action[0] == 'pick' else 'placing')):
                raise ValueError('action result is stale or uncorrelated')
            phase = action[0]
            if phase == 'pick' and applied:
                db.execute("UPDATE slots SET state='empty',product_type=NULL WHERE slot_id=?", (order['source_slot_id'],))
                state = 'carried'
            elif phase == 'place' and applied:
                db.execute("UPDATE slots SET state='occupied',product_type=? WHERE slot_id=?",
                           (order['product_type'], order['destination_slot_id']))
                state = 'completed'
            elif phase == 'pick':
                state = 'failed'
            else:
                state = 'unknown'
            db.execute('UPDATE actions SET state=? WHERE action_id=?', ('applied' if applied else 'rejected', action_id))
            db.execute('UPDATE orders SET state=? WHERE request_id=?', (state, request_id))
            if state in _TERMINAL:
                db.execute('DELETE FROM claims WHERE request_id=?', (request_id,))
            self._change(db)

    @staticmethod
    def _order(db, request_id):
        row = db.execute('SELECT request_id,product_type,source_slot_id,destination_slot_id,state '
                         'FROM orders WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            raise ValueError('order does not exist')
        request, product, source, dest, state = row
        return {'request_id': request, 'product_type': product, 'source_shelf': 'N01',
                'source_slot_id': source, 'source_node': 'D01', 'destination_shelf': PRODUCT_SHELVES[product],
                'destination_slot_id': dest, 'destination_node': 'D' + PRODUCT_SHELVES[product][1:], 'state': state}

    def get(self, request_id):
        _text(request_id, 'request ID')
        with self._write() as db:
            self._integrity(db)
            return self._order(db, request_id)

    def inspect(self):
        with self._write() as db:
            self._integrity(db)
            orders = [self._order(db, r[0]) for r in db.execute('SELECT request_id FROM orders ORDER BY request_id')]
            claims = [{'slot_id': row[0], 'request_id': row[1]} for row in
                      db.execute('SELECT slot_id,request_id FROM claims ORDER BY slot_id')]
            slots = [{'slot_id': r[0], 'shelf_id': r[1], 'slot_index': r[2], 'marker_id': r[3],
                      'state': r[4], 'product_type': r[5]}
                     for r in db.execute('SELECT slot_id,shelf_id,slot_index,marker_id,state,product_type '
                                         'FROM slots ORDER BY slot_id')]
            return {'slots': slots, 'orders': orders, 'claims': claims}
