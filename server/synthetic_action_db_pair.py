"""Fresh synthetic action DB pairs; not physical evidence or authorization.

The two SQLite commits are independent. A partially created pair is retained
and cannot be opened for use. The OS lock excludes only cooperating callers.
"""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading

from server.order_persistence import PersistentOrderStore, _json, _text
from server.order_pickup_inventory import PickupInventory
from server.sim_runtime import _DatabaseOwner


_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_WORLD = re.compile(r'[0-9a-f]{32}\Z')
_BINDING_SQL = ('CREATE TABLE synthetic_pair_binding ('
                'singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                'pair_id TEXT NOT NULL,world_id TEXT NOT NULL, '
                'order_creation_id TEXT NOT NULL,pickup_creation_id TEXT NOT NULL)')


def _expected_inputs(map_id, catalog, sources, world):
    _text(map_id, 'map identity')
    if type(world) is not str or _WORLD.fullmatch(world) is None:
        raise ValueError('explicit synthetic world identity required')
    if not isinstance(catalog, (list, tuple)) or not catalog or len(catalog) > 128:
        raise ValueError('bounded product catalog required')
    if not isinstance(sources, dict) or not sources or len(sources) > 128:
        raise ValueError('explicit pickup sources required')
    products = {}
    expected_sources = {}
    for row in catalog:
        if not isinstance(row, dict):
            raise ValueError('invalid product')
        product, source, kind = row.get('id'), row.get('pickup_node'), row.get('product_type')
        for value in (product, source, kind):
            _text(value, 'product mapping')
        if product in products or (source in expected_sources and expected_sources[source] != kind):
            raise ValueError('invalid or conflicting product mapping')
        products[product] = row
        expected_sources[source] = kind
    if sources != expected_sources:
        raise ValueError('pickup sources and catalog differ')
    try:
        catalog_json = _json([products[key] for key in sorted(products)])
        sources_json = json.dumps(sources, sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid simulated catalog') from exc
    return catalog_json, sources_json


def _file_identity(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('existing regular synthetic pair file required')
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _read_marker(path):
    marker = path / 'pair-ready.json'
    identity = _file_identity(marker)
    with marker.open('rb') as stream:
        raw = stream.read(2049)
    if len(raw) > 2048 or _file_identity(marker) != identity:
        raise ValueError('synthetic pair marker unavailable')
    try:
        def unique(pairs):
            data = {}
            for key, value in pairs:
                if key in data:
                    raise ValueError('duplicate marker field')
                data[key] = value
            return data
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
    except (UnicodeError, ValueError) as exc:
        raise ValueError('synthetic pair marker invalid') from exc
    if (not isinstance(data, dict) or set(data) !=
            {'pair_id', 'world_id', 'order_creation_id', 'pickup_creation_id'}
            or any(type(data[key]) is not str or _HEX64.fullmatch(data[key]) is None
                   for key in ('pair_id', 'order_creation_id', 'pickup_creation_id'))
            or type(data['world_id']) is not str or _WORLD.fullmatch(data['world_id']) is None):
        raise ValueError('synthetic pair marker invalid')
    return data


def _inspect(path, *, map_id, catalog_json, sources_json, pair_id, world_id, identities=None):
    """Read two separate snapshots under the cooperative pair lock."""
    marker = _read_marker(path)
    if marker['pair_id'] != pair_id or marker['world_id'] != world_id:
        raise ValueError('synthetic pair or world differs')
    paths = (path / 'orders.sqlite3', path / 'pickup-inventory.sqlite3')
    before = tuple(_file_identity(item) for item in paths)
    if identities is not None and before != identities:
        raise ValueError('synthetic pair database was replaced')
    expected = (marker['pair_id'], marker['world_id'], marker['order_creation_id'],
                marker['pickup_creation_id'])
    try:
        with ExitStack() as stack:
            databases = []
            for item, app_id, version in ((paths[0], 0x41475653, 3), (paths[1], 1095194192, 2)):
                db = stack.enter_context(closing(sqlite3.connect(item.resolve().as_uri() + '?mode=ro',
                                                                  uri=True, isolation_level=None, timeout=5)))
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                if (db.execute('PRAGMA application_id').fetchone() != (app_id,)
                        or db.execute('PRAGMA user_version').fetchone() != (version,)
                        or db.execute('PRAGMA quick_check').fetchall() != [('ok',)]
                        or db.execute('PRAGMA foreign_key_check').fetchall()):
                    raise ValueError('synthetic database integrity differs')
                rows = db.execute('SELECT pair_id,world_id,order_creation_id,pickup_creation_id '
                                  'FROM synthetic_pair_binding').fetchall()
                if rows != [expected]:
                    raise ValueError('synthetic pair database binding differs')
                databases.append(db)
            orders, pickup = databases
            if orders.execute('SELECT map_id,catalog_json,world_id,pickup_id FROM metadata '
                              'WHERE singleton=1').fetchall() != [(map_id, catalog_json, world_id,
                                                                   marker['pickup_creation_id'])]:
                raise ValueError('synthetic order database metadata differs')
            if pickup.execute('SELECT sources,world_id,creation_id FROM metadata '
                              'WHERE singleton=1').fetchall() != [(sources_json, world_id,
                                                                   marker['pickup_creation_id'])]:
                raise ValueError('synthetic pickup database metadata differs')
    except sqlite3.Error as exc:
        raise ValueError('synthetic pair database unavailable or invalid') from exc
    if tuple(_file_identity(item) for item in paths) != before or _read_marker(path) != marker:
        raise ValueError('synthetic pair changed during validation')
    return dict(marker, operable=False)


class _OrderReadView:
    """Read API with no retained writable store or mutable result fields."""
    __slots__ = ('_pair',)

    def __init__(self, pair):
        self._pair = pair

    @property
    def world_id(self):
        return self._pair._read_target(0, lambda db: db.execute(
            'SELECT world_id FROM metadata WHERE singleton=1').fetchone()[0])

    @property
    def pickup_id(self):
        return self._pair._read_target(0, lambda db: db.execute(
            'SELECT pickup_id FROM metadata WHERE singleton=1').fetchone()[0])

    @property
    def simulation_id(self):
        return self._pair._read_target(0, lambda db: db.execute(
            'SELECT simulation_id FROM metadata WHERE singleton=1').fetchone()[0])

    def get_order(self, request_id):
        _text(request_id, 'request ID')
        return self._pair._read_target(0, lambda db: PersistentOrderStore._order(db.execute(
            'SELECT request_id,product_id,state,result_json,error FROM orders WHERE request_id=?',
            (request_id,)).fetchone()))

    def list_orders(self):
        return self._pair._read_target(0, lambda db: [PersistentOrderStore._order(row) for row in db.execute(
            'SELECT request_id,product_id,state,result_json,error FROM orders ORDER BY sequence')])

    def peek_next(self):
        def read(db):
            row = db.execute("SELECT request_id,product_id,state,result_json,error FROM orders "
                             "WHERE state='queued' ORDER BY sequence LIMIT 1").fetchone()
            return None if row is None else PersistentOrderStore._order(row)
        return self._pair._read_target(0, read)

    def load_shelves(self):
        def read(db):
            revision = db.execute('SELECT shelf_revision FROM metadata WHERE singleton=1').fetchone()[0]
            shelves = {row['shelf_id']: {'product_type': row['product_type'], 'slots': [None] * row['slot_count']}
                       for row in json.loads(self._pair._catalog_json)}
            for shelf, index, observed in db.execute('SELECT shelf_id,slot_index,observed FROM shelf_slots'):
                shelves[shelf]['slots'][index] = None if observed is None else bool(observed)
            names = ('reservation_id', 'shelf_id', 'slot_index', 'state', 'request_id')
            reservations = [dict(zip(names, row)) for row in db.execute(
                'SELECT reservation_id,shelf_id,slot_index,state,request_id FROM slot_reservations ORDER BY rowid')]
            return {'revision': revision, 'shelves': shelves, 'reservations': reservations}
        return self._pair._read_target(0, read)


class _PickupReadView:
    __slots__ = ('_pair',)

    def __init__(self, pair):
        self._pair = pair

    @property
    def creation_id(self):
        return self._pair._read_target(1, lambda db: db.execute(
            'SELECT creation_id FROM metadata WHERE singleton=1').fetchone()[0])

    def snapshot(self):
        def read(db):
            world, revision = db.execute('SELECT world_id,revision FROM metadata WHERE singleton=1').fetchone()
            counts = dict(db.execute('SELECT source_id,available FROM counts'))
            rows = db.execute('SELECT reservation_id,request_id,source_id,product_type,state,order_id,action_id,agv_id '
                              'FROM reservations ORDER BY rowid').fetchall()
            names = ('reservation_id', 'request_id', 'source_id', 'product_type', 'state',
                     'order_id', 'action_id', 'agv_id')
            sources = {}
            for source, kind in json.loads(self._pair._sources_json).items():
                held = sum(row[2] == source and row[4] in ('reserved', 'inflight', 'unknown') for row in rows)
                sources[source] = {'product_type': kind, 'available': counts[source],
                                   'reserved': held, 'allocatable': None}
            # The pair has no active pickup session and cannot establish freshness.
            return {'world_id': world, 'fresh': False, 'revision': revision,
                    'sources': sources, 'reservations': [dict(zip(names, row)) for row in rows]}
        return self._pair._read_target(1, read)


class SyntheticActionDbPair:
    """A single-process, fresh synthetic pair; no automatic restart support."""

    def __init__(self, *, path, owner, metadata, catalog_json, sources_json, map_id):
        self._path, self._owner = path, owner
        self._lock = threading.RLock()
        self._closed = False
        self._metadata = metadata
        self._catalog_json, self._sources_json, self._map_id = catalog_json, sources_json, map_id
        self._identities = tuple(_file_identity(path / name) for name in ('orders.sqlite3', 'pickup-inventory.sqlite3'))
        self._marker_identity = _file_identity(path / 'pair-ready.json')
        self.orders, self.pickup = _OrderReadView(self), _PickupReadView(self)

    @property
    def pair_id(self):
        return self._metadata['pair_id']

    @property
    def world_id(self):
        return self._metadata['world_id']

    @property
    def order_creation_id(self):
        return self._metadata['order_creation_id']

    @property
    def operable(self):
        return False

    @classmethod
    def create_new(cls, path, *, map_id, catalog, pickup_sources, world_id):
        catalog_json, sources_json = _expected_inputs(map_id, catalog, pickup_sources, world_id)
        path = Path(path)
        path.mkdir(mode=0o700)  # O_EXCL semantics: never adopt an existing directory.
        owner = _DatabaseOwner(path / 'orders.sqlite3')
        owner.acquire()
        try:
            orders = PersistentOrderStore(path / 'orders.sqlite3', map_id, catalog, action_source='synthetic')
            pickup = PickupInventory.create_new(path / 'pickup-inventory.sqlite3', pickup_sources,
                                                action_source='synthetic')
            orders.bind_world_id(world_id)
            token = pickup.begin_session(world_id)
            pickup.end_session(token)
            orders.bind_pickup_id(pickup.creation_id)
            metadata = {'pair_id': secrets.token_hex(32), 'world_id': world_id,
                        'order_creation_id': secrets.token_hex(32), 'pickup_creation_id': pickup.creation_id}
            values = tuple(metadata[key] for key in ('pair_id', 'world_id', 'order_creation_id', 'pickup_creation_id'))
            for store in (orders, pickup):
                with store._write() as db:
                    db.execute(_BINDING_SQL)
                    db.execute('INSERT INTO synthetic_pair_binding VALUES (1,?,?,?,?)', values)
            marker = path / 'pair-ready.json'
            with marker.open('xb') as stream:
                stream.write(json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode('ascii'))
                stream.flush()
                os.fsync(stream.fileno())
            pair = cls(path=path, owner=owner, metadata=metadata,
                       catalog_json=catalog_json, sources_json=sources_json, map_id=map_id)
            pair._validate_live()
            return pair
        except BaseException:
            owner.release()
            raise

    def _validate_live(self):
        if self._closed:
            raise RuntimeError('synthetic pair is closed')
        if _file_identity(self._path / 'pair-ready.json') != self._marker_identity:
            raise ValueError('synthetic pair marker was replaced')
        _inspect(self._path, map_id=self._map_id, catalog_json=self._catalog_json,
                 sources_json=self._sources_json, pair_id=self.pair_id, world_id=self.world_id,
                 identities=self._identities)
        if _file_identity(self._path / 'pair-ready.json') != self._marker_identity:
            raise ValueError('synthetic pair marker was replaced')

    def _read_target(self, target, operation):
        with self._lock:
            self._validate_live()
            path = self._path / ('orders.sqlite3' if target == 0 else 'pickup-inventory.sqlite3')
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                             timeout=5, isolation_level=None)) as db:
                    db.execute('PRAGMA query_only=ON')
                    db.execute('BEGIN')
                    app_id, version = ((0x41475653, 3) if target == 0 else (1095194192, 2))
                    if (db.execute('PRAGMA application_id').fetchone() != (app_id,)
                            or db.execute('PRAGMA user_version').fetchone() != (version,)):
                        raise ValueError('synthetic database source differs')
                    expected = tuple(self._metadata[key] for key in
                                     ('pair_id', 'world_id', 'order_creation_id', 'pickup_creation_id'))
                    if db.execute('SELECT pair_id,world_id,order_creation_id,pickup_creation_id '
                                  'FROM synthetic_pair_binding').fetchall() != [expected]:
                        raise ValueError('synthetic database binding differs')
                    result = operation(db)
            except sqlite3.Error as exc:
                raise ValueError('synthetic database read unavailable') from exc
            self._validate_live()
            return result

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._owner.release()

    @classmethod
    def inspect_existing(cls, path, *, map_id, catalog, pickup_sources, expected_pair_id, expected_world_id):
        catalog_json, sources_json = _expected_inputs(map_id, catalog, pickup_sources, expected_world_id)
        if type(expected_pair_id) is not str or _HEX64.fullmatch(expected_pair_id) is None:
            raise ValueError('independent synthetic pair identity required')
        path = Path(path)
        if path.is_symlink() or not path.is_dir():
            raise ValueError('existing synthetic pair directory required')
        # Do not create files while inspecting an incomplete pair.
        _file_identity(path / 'pair-ready.json')
        _file_identity(path / 'orders.sqlite3')
        _file_identity(path / 'pickup-inventory.sqlite3')
        owner = _DatabaseOwner(path / 'orders.sqlite3')
        _file_identity(Path(owner.path))
        owner.acquire()
        try:
            return _inspect(path, map_id=map_id, catalog_json=catalog_json, sources_json=sources_json,
                            pair_id=expected_pair_id, world_id=expected_world_id)
        finally:
            owner.release()

    @classmethod
    def open_existing(cls, path, **kwargs):
        cls.inspect_existing(path, **kwargs)
        raise RuntimeError('synthetic world is unavailable after restart')
