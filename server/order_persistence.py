"""Durable, fail-closed order and shelf journal for the isolated simulation.

This journal records observations, not physical inventory or drive permits.
Its caller must hold the simulation's exclusive process lock before invoking
restart recovery and must obtain a fresh authenticated Unreal snapshot before
using saved shelf observations to authorize new orders.
"""
from contextlib import closing, contextmanager
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid


_APPLICATION_ID = 0x41475653
_SCHEMA_VERSION = 2
_SYNTHETIC_SCHEMA_VERSION = 3


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _text(value, label, limit=256):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid ' + label)
    return value


class PersistentOrderStore:
    """A file-backed journal. Every mutation is a SQLite write transaction."""

    def __init__(self, path, map_id, catalog, *, action_source='unreal'):
        if type(action_source) is not str or action_source not in ('unreal', 'synthetic'):
            raise ValueError('invalid simulated action source')
        self._action_source = action_source
        self._schema_version = (_SCHEMA_VERSION if action_source == 'unreal'
                                else _SYNTHETIC_SCHEMA_VERSION)
        name = os.fspath(path)
        if not isinstance(name, str) or not name or name == ':memory:':
            raise ValueError('file-backed simulation journal required')
        self.path = os.path.realpath(name)
        self.map_id = _text(map_id, 'map identity')
        if not isinstance(catalog, (list, tuple)) or not catalog or len(catalog) > 128:
            raise ValueError('bounded product catalog required')
        entries, shelves = {}, {}
        for product in copy.deepcopy(catalog):
            if not isinstance(product, dict):
                raise ValueError('invalid product entry')
            product_id = _text(product.get('id'), 'product ID')
            product_type = _text(product.get('product_type'), 'product type')
            shelf = _text(product.get('shelf_id'), 'shelf ID')
            _text(product.get('pickup_node'), 'pickup node')
            _text(product.get('destination_node'), 'destination node')
            count = product.get('slot_count')
            if type(count) is not int or not 1 <= count <= 128 or product_id in entries:
                raise ValueError('invalid product slot count or duplicate ID')
            if shelf in shelves and shelves[shelf] != (product_type, count):
                raise ValueError('conflicting shelf catalog')
            shelves[shelf] = (product_type, count)
            entries[product_id] = product
        self.products, self.shelves = entries, shelves
        try:
            self._catalog_json = _json([entries[key] for key in sorted(entries)])
        except (TypeError, ValueError) as exc:
            raise ValueError('catalog is not serializable') from exc
        self._lock = threading.RLock()
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            created = False
        else:
            os.close(fd)
            created = True
        self._schema_initialized = not created
        try:
            if created:
                self._create()
                self._schema_initialized = True
            else:
                self._validate()
        except sqlite3.Error as exc:
            raise ValueError('simulation journal is corrupt or unavailable') from exc

    def _connect(self):
        db = sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=rw',
                             uri=True, timeout=5, isolation_level=None)
        try:
            if self._schema_initialized and (
                    db.execute('PRAGMA application_id').fetchone() != (_APPLICATION_ID,)
                    or db.execute('PRAGMA user_version').fetchone() != (self._schema_version,)):
                raise ValueError('simulation journal source or schema differs')
            return db
        except BaseException:
            db.close()
            raise

    def _check_source(self, db):
        if self._schema_initialized and (
                db.execute('PRAGMA application_id').fetchone() != (_APPLICATION_ID,)
                or db.execute('PRAGMA user_version').fetchone() != (self._schema_version,)):
            raise ValueError('simulation journal source or schema differs')

    @contextmanager
    def _read(self):
        with closing(self._connect()) as db:
            db.execute('BEGIN')
            self._check_source(db)
            yield db

    @contextmanager
    def _write(self):
        with self._lock, closing(self._connect()) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            try:
                self._check_source(db)
                yield db
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _create(self):
        with self._write() as db:
            db.execute('PRAGMA application_id={}'.format(_APPLICATION_ID))
            db.execute('PRAGMA user_version={}'.format(self._schema_version))
            db.execute('CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                       'map_id TEXT NOT NULL, catalog_json TEXT NOT NULL, shelf_revision INTEGER NOT NULL, '
                       'simulation_id TEXT, world_id TEXT, pickup_id TEXT)')
            db.execute('INSERT INTO metadata VALUES (1,?,?,0,NULL,NULL,NULL)', (self.map_id, self._catalog_json))
            db.execute('CREATE TABLE orders (sequence INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'request_id TEXT UNIQUE NOT NULL, request_key TEXT UNIQUE, fingerprint TEXT NOT NULL, '
                       'product_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN '
                       "('queued','dispatching','active','completed','failed','unknown')), "
                       'result_json TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, '
                       'updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('CREATE TABLE order_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'request_id TEXT NOT NULL REFERENCES orders(request_id), state TEXT NOT NULL, '
                       'detail_json TEXT, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('CREATE TABLE shelf_slots (shelf_id TEXT NOT NULL, product_type TEXT NOT NULL, '
                       'slot_index INTEGER NOT NULL, observed INTEGER CHECK(observed IN (0,1) OR observed IS NULL), '
                       'PRIMARY KEY(shelf_id,slot_index))')
            db.execute('CREATE TABLE slot_reservations (reservation_id TEXT PRIMARY KEY NOT NULL, '
                       'shelf_id TEXT NOT NULL, slot_index INTEGER NOT NULL, request_id TEXT REFERENCES orders(request_id), '
                       "state TEXT NOT NULL CHECK(state IN ('reserved','confirmed')), "
                       'FOREIGN KEY(shelf_id,slot_index) REFERENCES shelf_slots(shelf_id,slot_index))')
            db.execute('CREATE UNIQUE INDEX unique_held_slot ON slot_reservations(shelf_id,slot_index)')
            for shelf, (kind, count) in self.shelves.items():
                db.executemany('INSERT INTO shelf_slots VALUES (?,?,?,NULL)',
                               [(shelf, kind, i) for i in range(count)])

    def _validate(self):
        with self._read() as db:
            db.execute('PRAGMA foreign_keys=ON')
            if (db.execute('PRAGMA application_id').fetchone() != (_APPLICATION_ID,)
                    or db.execute('PRAGMA user_version').fetchone() != (self._schema_version,)
                    or db.execute('PRAGMA quick_check').fetchall() != [('ok',)]
                    or db.execute('PRAGMA foreign_key_check').fetchall()):
                raise ValueError('simulation journal schema or integrity mismatch')
            row = db.execute('SELECT map_id,catalog_json,shelf_revision FROM metadata WHERE singleton=1').fetchone()
            if (row is None or row[:2] != (self.map_id, self._catalog_json)
                    or type(row[2]) is not int or row[2] < 0):
                raise ValueError('simulation journal map or catalog differs')
            expected = {(shelf, kind, i) for shelf, (kind, count) in self.shelves.items() for i in range(count)}
            actual = set(db.execute('SELECT shelf_id,product_type,slot_index FROM shelf_slots'))
            index = next((item for item in db.execute('PRAGMA index_list(slot_reservations)')
                          if item[1] == 'unique_held_slot'), None)
            columns = [row[2] for row in db.execute('PRAGMA index_info(unique_held_slot)')]
            if actual != expected or index is None or index[2] != 1 or columns != ['shelf_id', 'slot_index']:
                raise ValueError('simulation shelf schema differs')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_events'").fetchone() is None:
                raise ValueError('simulation journal events are missing')
            identity = db.execute('SELECT simulation_id FROM metadata WHERE singleton=1').fetchone()[0]
            if identity is not None and (not isinstance(identity, str) or re.fullmatch(r'[0-9a-f]{64}', identity) is None):
                raise ValueError('simulation identity is invalid')
            pickup = db.execute('SELECT pickup_id FROM metadata WHERE singleton=1').fetchone()[0]
            if pickup is not None and (not isinstance(pickup, str) or re.fullmatch(r'[0-9a-f]{64}', pickup) is None):
                raise ValueError('pickup identity is invalid')
            world = db.execute('SELECT world_id FROM metadata WHERE singleton=1').fetchone()[0]
            if world is not None and (not isinstance(world, str) or re.fullmatch(r'[0-9a-f]{32}', world) is None):
                raise ValueError('Unreal world identity is invalid')

    @property
    def simulation_id(self):
        with self._read() as db:
            row = db.execute('SELECT simulation_id FROM metadata WHERE singleton=1').fetchone()
            if row is None:
                raise ValueError('simulation journal identity is missing')
            return row[0]

    def bind_simulation_id(self, identity):
        if not isinstance(identity, str) or re.fullmatch(r'[0-9a-f]{64}', identity) is None:
            raise ValueError('invalid simulation store identity')
        with self._write() as db:
            old = db.execute('SELECT simulation_id FROM metadata WHERE singleton=1').fetchone()
            if old is None or old[0] not in (None, identity):
                raise ValueError('simulation store identity differs')
            if old[0] is None:
                db.execute('UPDATE metadata SET simulation_id=? WHERE singleton=1', (identity,))
        return identity

    @property
    def pickup_id(self):
        with self._read() as db:
            row = db.execute('SELECT pickup_id FROM metadata WHERE singleton=1').fetchone()
            if row is None:
                raise ValueError('pickup journal identity is missing')
            return row[0]

    def bind_pickup_id(self, identity):
        if not isinstance(identity, str) or re.fullmatch(r'[0-9a-f]{64}', identity) is None:
            raise ValueError('invalid pickup store identity')
        with self._write() as db:
            old = db.execute('SELECT pickup_id FROM metadata WHERE singleton=1').fetchone()
            if old is None or old[0] not in (None, identity):
                raise ValueError('pickup store identity differs')
            if old[0] is None:
                db.execute('UPDATE metadata SET pickup_id=? WHERE singleton=1', (identity,))
        return identity

    @property
    def world_id(self):
        with self._read() as db:
            row = db.execute('SELECT world_id FROM metadata WHERE singleton=1').fetchone()
            if row is None:
                raise ValueError('simulation journal identity is missing')
            return row[0]

    def bind_world_id(self, identity):
        if not isinstance(identity, str) or re.fullmatch(r'[0-9a-fA-F]{32}', identity) is None:
            raise ValueError('invalid Unreal world identity')
        identity = identity.lower()
        with self._write() as db:
            old = db.execute('SELECT world_id FROM metadata WHERE singleton=1').fetchone()
            if old is None or old[0] not in (None, identity):
                raise ValueError('Unreal world identity differs')
            if old[0] is None:
                db.execute('UPDATE metadata SET world_id=? WHERE singleton=1', (identity,))
        return identity

    @staticmethod
    def _order(row, *, created=False):
        if row is None:
            raise KeyError('order not found')
        request_id, product, state, result, error = row
        return {'request_id': request_id, 'product_id': product, 'state': state,
                'result': json.loads(result) if result is not None else None,
                'error': error, 'created': created}

    def submit(self, product_id, request_key=None):
        _text(product_id, 'product ID')
        if product_id not in self.products:
            raise ValueError('unknown simulated product')
        if request_key is not None:
            _text(request_key, 'idempotency key', 128)
        fingerprint = 'product:' + product_id
        with self._write() as db:
            if request_key is not None:
                old = db.execute('SELECT request_id,product_id,state,result_json,error,fingerprint '
                                 'FROM orders WHERE request_key=?', (request_key,)).fetchone()
                if old is not None:
                    if old[5] != fingerprint:
                        raise ValueError('request key belongs to a different order')
                    return self._order(old[:5])
            request_id = uuid.uuid4().hex
            db.execute('INSERT INTO orders(request_id,request_key,fingerprint,product_id,state) '
                       "VALUES (?,?,?,?,'queued')", (request_id, request_key, fingerprint, product_id))
            db.execute("INSERT INTO order_events(request_id,state) VALUES (?,'queued')", (request_id,))
            return self._order((request_id, product_id, 'queued', None, None), created=True)

    def get_order(self, request_id):
        _text(request_id, 'request ID')
        with self._read() as db:
            return self._order(db.execute('SELECT request_id,product_id,state,result_json,error '
                                          'FROM orders WHERE request_id=?', (request_id,)).fetchone())

    def list_orders(self):
        with self._read() as db:
            return [self._order(row) for row in db.execute('SELECT request_id,product_id,state,result_json,error '
                                                            'FROM orders ORDER BY sequence')]

    def claim_next(self):
        with self._write() as db:
            row = db.execute("SELECT request_id,product_id,state,result_json,error FROM orders "
                             "WHERE state='queued' ORDER BY sequence LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE orders SET state='dispatching',updated_at=CURRENT_TIMESTAMP WHERE request_id=?", (row[0],))
            db.execute("INSERT INTO order_events(request_id,state) VALUES (?,'dispatching')", (row[0],))
            return self._order((row[0], row[1], 'dispatching', row[3], row[4]))

    def peek_next(self):
        with self._read() as db:
            row = db.execute("SELECT request_id,product_id,state,result_json,error FROM orders "
                             "WHERE state='queued' ORDER BY sequence LIMIT 1").fetchone()
            return None if row is None else self._order(row)

    def claim(self, request_id):
        _text(request_id, 'request ID')
        with self._write() as db:
            row = db.execute("SELECT request_id,product_id,state,result_json,error FROM orders "
                             "WHERE state='queued' ORDER BY sequence LIMIT 1").fetchone()
            if row is None or row[0] != request_id:
                raise RuntimeError('only the first queued order may be dispatched')
            db.execute("UPDATE orders SET state='dispatching',updated_at=CURRENT_TIMESTAMP WHERE request_id=?", (request_id,))
            db.execute("INSERT INTO order_events(request_id,state) VALUES (?,'dispatching')", (request_id,))
            return self._order((row[0], row[1], 'dispatching', row[3], row[4]))

    def _transition(self, request_id, state, allowed, result=None, error=None):
        _text(request_id, 'request ID')
        encoded = None if result is None else _json(result)
        if error is not None:
            _text(error, 'error', 512)
        with self._write() as db:
            old = db.execute('SELECT state,result_json,product_id FROM orders WHERE request_id=?', (request_id,)).fetchone()
            if old is None:
                raise KeyError(request_id)
            if old[0] not in allowed:
                raise RuntimeError('unsafe simulated order state transition')
            if state == 'active' and old[0] == 'dispatching':
                reservation = db.execute('SELECT request_id,shelf_id,slot_index,state FROM slot_reservations '
                                         'WHERE reservation_id=?', (result['reservation_id'],)).fetchone()
                product = self.products[old[2]]
                if (reservation != (request_id, result['shelf_id'], result['slot_index'], 'reserved')
                        or result['shelf_id'] != product['shelf_id']
                        or result['slot_index'] >= product['slot_count']):
                    raise ValueError('assigned order does not match its slot reservation')
            if encoded is not None and old[1] is not None:
                previous = json.loads(old[1])
                incoming = json.loads(encoded)
                if not isinstance(previous, dict) or not isinstance(incoming, dict):
                    raise ValueError('order result must be an object')
                for key in ('assigned_robot', 'order_id', 'shelf_id', 'slot_index', 'reservation_id'):
                    if key in previous and key in incoming and previous[key] != incoming[key]:
                        raise ValueError('order assignment identity changed')
                encoded = _json(dict(previous, **incoming))
            db.execute('UPDATE orders SET state=?, result_json=COALESCE(?,result_json), error=?, '
                       'updated_at=CURRENT_TIMESTAMP WHERE request_id=?', (state, encoded, error, request_id))
            db.execute('INSERT INTO order_events(request_id,state,detail_json) VALUES (?,?,?)',
                       (request_id, state, encoded))
        return self.get_order(request_id)

    def mark_active(self, request_id, result):
        if (not isinstance(result, dict) or any(not isinstance(result.get(key), str) or not result[key]
                                               for key in ('assigned_robot', 'order_id', 'shelf_id', 'reservation_id'))
                or type(result.get('slot_index')) is not int or result['slot_index'] < 0):
            raise ValueError('assigned order result required')
        return self._transition(request_id, 'active', ('dispatching',), result)

    def mark_completed(self, request_id, result):
        if not isinstance(result, dict):
            raise ValueError('completed order result required')
        return self._transition(request_id, 'completed', ('active',), result)

    def update_active(self, request_id, result):
        """Persist an observation; it is not a physical-position proof."""
        if not isinstance(result, dict):
            raise ValueError('active order observation required')
        return self._transition(request_id, 'active', ('active',), result)

    def mark_failed(self, request_id, error):
        return self._transition(request_id, 'failed', ('queued', 'dispatching', 'active'), error=error)

    def mark_unknown(self, request_id, error):
        return self._transition(request_id, 'unknown', ('dispatching', 'active'), error=error)

    def recover_interrupted(self):
        """Call only after obtaining exclusive process ownership for this DB."""
        with self._write() as db:
            ids = [row[0] for row in db.execute("SELECT request_id FROM orders WHERE state IN ('dispatching','active')")]
            for request_id in ids:
                db.execute("UPDATE orders SET state='unknown',error='server interrupted',updated_at=CURRENT_TIMESTAMP "
                           'WHERE request_id=?', (request_id,))
                db.execute("INSERT INTO order_events(request_id,state) VALUES (?,'unknown')", (request_id,))
            return ids

    def sync_shelves(self, shelves):
        if not isinstance(shelves, list) or len(shelves) != len(self.shelves):
            raise ValueError('incomplete shelf snapshot')
        parsed = {}
        for entry in shelves:
            if not isinstance(entry, dict) or set(entry) != {'shelf_id', 'product_type', 'slots'}:
                raise ValueError('invalid shelf snapshot')
            shelf, kind, slots = entry['shelf_id'], entry['product_type'], entry['slots']
            if (not isinstance(shelf, str) or shelf not in self.shelves or shelf in parsed
                    or kind != self.shelves[shelf][0] or not isinstance(slots, list)
                    or len(slots) != self.shelves[shelf][1] or any(type(v) is not bool for v in slots)):
                raise ValueError('invalid shelf contents')
            parsed[shelf] = list(slots)
        if set(parsed) != set(self.shelves):
            raise ValueError('missing shelf')
        with self._write() as db:
            for shelf, index in db.execute("SELECT shelf_id,slot_index FROM slot_reservations WHERE state='confirmed'"):
                if not parsed[shelf][index]:
                    raise RuntimeError('confirmed shelf content differs from current Unreal world')
            for shelf, slots in parsed.items():
                db.executemany('UPDATE shelf_slots SET observed=? WHERE shelf_id=? AND slot_index=?',
                               [(int(value), shelf, index) for index, value in enumerate(slots)])
            db.execute('UPDATE metadata SET shelf_revision=shelf_revision+1 WHERE singleton=1')
            return db.execute('SELECT shelf_revision FROM metadata WHERE singleton=1').fetchone()[0]

    def reserve_slot(self, shelf_id, product_type, expected_revision, *, request_id=None):
        if shelf_id not in self.shelves or self.shelves[shelf_id][0] != product_type:
            raise ValueError('invalid shelf or product type')
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('verified shelf revision required')
        with self._write() as db:
            if db.execute('SELECT shelf_revision FROM metadata WHERE singleton=1').fetchone() != (expected_revision,):
                raise RuntimeError('shelf snapshot revision changed')
            if request_id is not None:
                order = db.execute('SELECT product_id,state FROM orders WHERE request_id=?', (request_id,)).fetchone()
                if (order is None or order[1] not in ('queued', 'dispatching')
                        or self.products.get(order[0], {}).get('shelf_id') != shelf_id):
                    raise ValueError('slot reservation does not match the queued order')
            row = db.execute('SELECT s.slot_index FROM shelf_slots s LEFT JOIN slot_reservations r '
                             'ON s.shelf_id=r.shelf_id AND s.slot_index=r.slot_index '
                             'WHERE s.shelf_id=? AND s.observed=0 AND r.reservation_id IS NULL '
                             'ORDER BY s.slot_index LIMIT 1', (shelf_id,)).fetchone()
            if row is None:
                raise RuntimeError('no confirmed empty simulated shelf slot')
            reservation = uuid.uuid4().hex
            db.execute("INSERT INTO slot_reservations VALUES (?,?,?,?,'reserved')",
                       (reservation, shelf_id, row[0], request_id))
            return {'reservation_id': reservation, 'shelf_id': shelf_id, 'slot_index': row[0]}

    def confirm_slot(self, reservation_id):
        _text(reservation_id, 'reservation ID')
        with self._write() as db:
            row = db.execute('SELECT shelf_id,slot_index,state FROM slot_reservations WHERE reservation_id=?',
                             (reservation_id,)).fetchone()
            if row is None or row[2] != 'reserved':
                raise ValueError('reservation is missing or already confirmed')
            db.execute("UPDATE slot_reservations SET state='confirmed' WHERE reservation_id=?", (reservation_id,))
            db.execute('UPDATE shelf_slots SET observed=1 WHERE shelf_id=? AND slot_index=?', (row[0], row[1]))
            db.execute('UPDATE metadata SET shelf_revision=shelf_revision+1 WHERE singleton=1')
        return True

    def load_shelves(self):
        with self._read() as db:
            revision = db.execute('SELECT shelf_revision FROM metadata WHERE singleton=1').fetchone()[0]
            shelves = {key: {'product_type': kind, 'slots': [None] * count}
                       for key, (kind, count) in self.shelves.items()}
            for shelf, index, observed in db.execute('SELECT shelf_id,slot_index,observed FROM shelf_slots'):
                shelves[shelf]['slots'][index] = None if observed is None else bool(observed)
            reservations = [{'reservation_id': rid, 'shelf_id': shelf, 'slot_index': index,
                             'state': state, 'request_id': request}
                            for rid, shelf, index, state, request in db.execute(
                                'SELECT reservation_id,shelf_id,slot_index,state,request_id '
                                'FROM slot_reservations ORDER BY rowid')]
            return {'revision': revision, 'shelves': shelves, 'reservations': reservations}
