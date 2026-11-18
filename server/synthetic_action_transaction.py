"""Fresh simulated grab attempt boundary, without a peer or robot transport.

The result is an uncertain storage record, never proof of execution or safety.
Allocation DB, MQTT and any simulated world are outside this transaction.
"""
from contextlib import closing, contextmanager
import json
import re
import secrets
import sqlite3
import threading

from server.order_persistence import _text
from server.synthetic_action_db_pair import SyntheticActionDbPair, _file_identity, _read_marker


_CONSTRUCTION = object()


class _ProgressBlocked(RuntimeError):
    """The saved synthetic outcome blocks further progress, but not duplicate reads."""


def _assignment_json(raw):
    if type(raw) is not str or len(raw.encode('utf-8')) > 65536:
        raise ValueError('invalid simulated assignment')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate simulated assignment field')
            value[key] = item
        return value
    def reject_constant(_value):
        raise ValueError('nonfinite simulated assignment value')
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid simulated assignment') from exc


def _open_connection(pair):
    paths = (pair._path / 'orders.sqlite3', pair._path / 'pickup-inventory.sqlite3')
    db = sqlite3.connect(paths[0].resolve().as_uri() + '?mode=rw', uri=True,
                         timeout=0, isolation_level=None)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('ATTACH DATABASE ? AS pickup', (paths[1].resolve().as_uri() + '?mode=rw',))
        return db
    except BaseException:
        db.close()
        raise


class SyntheticActionSession:
    """One fresh in-process session; cannot be reopened or resumed."""

    def __init__(self, key, pair, robot_twins):
        if key is not _CONSTRUCTION:
            raise ValueError('create a fresh isolated synthetic session')
        self._pair = pair
        self._twins = dict(robot_twins)
        self._generation = secrets.token_hex(32)
        self._lock = threading.Lock()
        self._closed = False
        self._blocked = False
        self._schema_baseline = None

    @classmethod
    def create_new(cls, path, *, map_id, catalog, pickup_sources, world_id, robot_twins):
        if (not isinstance(robot_twins, dict) or not robot_twins or len(robot_twins) > 16
                or any(type(robot) is not str or re.fullmatch(r'sim-[A-Za-z0-9_-]{1,80}', robot) is None
                       or type(twin) is not str or re.fullmatch(r'AGV_[A-Za-z0-9_-]{1,80}', twin) is None
                       for robot, twin in robot_twins.items())
                or len(set(robot_twins.values())) != len(robot_twins)):
            raise ValueError('explicit distinct synthetic robot/twin mapping required')
        pair = SyntheticActionDbPair.create_new(path, map_id=map_id, catalog=catalog,
                                                pickup_sources=pickup_sources, world_id=world_id)
        session = cls(_CONSTRUCTION, pair, robot_twins)
        try:
            with session._transaction(initializing=True) as db:
                db.execute('CREATE TABLE synthetic_action_attempt ('
                           'action_id TEXT PRIMARY KEY NOT NULL,request_id TEXT NOT NULL,task_id TEXT NOT NULL, '
                           'agv_id TEXT NOT NULL,action TEXT NOT NULL,state TEXT NOT NULL, '
                           'generation TEXT NOT NULL,pair_id TEXT NOT NULL,world_id TEXT NOT NULL, '
                           'pickup_reservation_id TEXT NOT NULL,slot_reservation_id TEXT NOT NULL)')
                db.execute('CREATE TABLE synthetic_action_session ('
                           'singleton INTEGER PRIMARY KEY CHECK(singleton=1),generation TEXT NOT NULL, '
                           'progress_blocked INTEGER NOT NULL CHECK(progress_blocked IN (0,1)))')
                db.execute('INSERT INTO synthetic_action_session VALUES (1,?,0)', (session._generation,))
                db.execute('UPDATE pickup.metadata SET session_id=? WHERE singleton=1', (session._generation,))
            return session
        except BaseException:
            session.close()
            raise

    @classmethod
    def open_existing(cls, path, **kwargs):
        raise RuntimeError('a synthetic session cannot resume after restart')

    @property
    def session_id(self):
        """Correlation generation, not caller or robot authentication."""
        return self._generation

    @property
    def world_id(self):
        return self._pair.world_id

    @staticmethod
    def _schema(db):
        return tuple(tuple(db.execute('SELECT type,name,tbl_name,sql FROM {}.sqlite_master '
                                      'ORDER BY type,name'.format(name)).fetchall())
                     for name in ('main', 'pickup'))

    def _verify(self, db):
        pair = self._pair
        paths = (pair._path / 'orders.sqlite3', pair._path / 'pickup-inventory.sqlite3')
        if (tuple(_file_identity(path) for path in paths) != pair._identities
                or _file_identity(pair._path / 'pair-ready.json') != pair._marker_identity
                or _read_marker(pair._path) != pair._metadata):
            raise ValueError('synthetic files changed')
        expected = tuple(pair._metadata[key] for key in
                         ('pair_id', 'world_id', 'order_creation_id', 'pickup_creation_id'))
        for name, app_id, version in (('main', 0x41475653, 3), ('pickup', 1095194192, 2)):
            if (db.execute('PRAGMA {}.application_id'.format(name)).fetchone() != (app_id,)
                    or db.execute('PRAGMA {}.user_version'.format(name)).fetchone() != (version,)
                    or db.execute('PRAGMA {}.journal_mode'.format(name)).fetchone()[0].lower()
                    not in ('delete', 'truncate', 'persist')
                    or db.execute('PRAGMA {}.synchronous'.format(name)).fetchone()[0] < 2
                    or db.execute('PRAGMA {}.quick_check'.format(name)).fetchall() != [('ok',)]
                    or db.execute('PRAGMA {}.foreign_key_check'.format(name)).fetchall()):
                raise ValueError('synthetic source, mode, or database integrity differs')
            rows = db.execute('SELECT pair_id,world_id,order_creation_id,pickup_creation_id '
                              'FROM {}.synthetic_pair_binding'.format(name)).fetchall()
            if rows != [expected]:
                raise ValueError('synthetic pair binding differs')
            if db.execute("SELECT 1 FROM {}.sqlite_master WHERE type='trigger' LIMIT 1".format(name)).fetchone():
                raise ValueError('synthetic transaction does not allow triggers')
        if db.execute('SELECT map_id,catalog_json,world_id,pickup_id FROM main.metadata '
                      'WHERE singleton=1').fetchall() != [(pair._map_id, pair._catalog_json, pair.world_id,
                                                          pair._metadata['pickup_creation_id'])]:
            raise ValueError('synthetic order metadata differs')
        if db.execute('SELECT sources,world_id,creation_id FROM pickup.metadata '
                      'WHERE singleton=1').fetchall() != [(pair._sources_json, pair.world_id,
                                                          pair._metadata['pickup_creation_id'])]:
            raise ValueError('synthetic pickup metadata differs')
        if self._schema_baseline is not None and self._schema(db) != self._schema_baseline:
            raise ValueError('synthetic database schema changed')
        if self._schema_baseline is not None:
            if db.execute("SELECT 1 FROM pickup.reservations WHERE state='reserved' AND "
                          '(order_id IS NOT NULL OR action_id IS NOT NULL OR agv_id IS NOT NULL '
                          'OR session_id IS NOT NULL OR dispatch_revision IS NOT NULL) LIMIT 1').fetchone():
                raise ValueError('reserved synthetic pickup contains prior dispatch metadata')
            rows = db.execute('SELECT action_id,request_id,task_id,agv_id,action,state,generation,pair_id,world_id, '
                              'pickup_reservation_id,slot_reservation_id FROM main.synthetic_action_attempt '
                              'LIMIT ?', (len(self._twins) + 1,)).fetchall()
            if len(rows) > len(self._twins):
                raise ValueError('too many unresolved synthetic action attempts')
            seen = [set() for _ in range(5)]
            active_pickups = set()
            all_pickups = {}
            expected_blocked = 0
            products = {item['id']: item for item in json.loads(pair._catalog_json)}
            metadata = db.execute('SELECT session_id,revision FROM pickup.metadata WHERE singleton=1').fetchone()
            if (metadata is None or metadata[0] != self._generation
                    or type(metadata[1]) is not int or metadata[1] < 0):
                raise ValueError('synthetic pickup session or revision differs')
            for row in rows:
                action_id, request_id, task_id, agv_id, action, state, generation, pair_id, world_id, pickup, slot = row
                if (action != 'grab' or state not in ('uncertain', 'applied', 'not_applied', 'unknown')
                        or generation != self._generation
                        or pair_id != pair.pair_id or world_id != pair.world_id
                        or agv_id not in self._twins.values()):
                    raise ValueError('synthetic action attempt integrity differs')
                for value, name in ((action_id, 'action ID'), (request_id, 'request ID'),
                                    (task_id, 'task ID'), (agv_id, 'AGV ID'),
                                    (pickup, 'pickup reservation ID'), (slot, 'slot reservation ID')):
                    _text(value, name, 128)
                for value, values in zip((request_id, task_id, agv_id, pickup, slot), seen):
                    if value in values:
                        raise ValueError('duplicate unresolved synthetic action attempt')
                    values.add(value)
                saved = db.execute('SELECT product_id,state,result_json FROM main.orders WHERE request_id=?',
                                   (request_id,)).fetchone()
                if saved is None or saved[1] != 'active':
                    raise ValueError('synthetic attempt order is missing or inactive')
                product = products.get(saved[0])
                assignment = _assignment_json(saved[2])
                if (not isinstance(product, dict) or not isinstance(assignment, dict)
                        or assignment.get('order_id') != task_id
                        or type(assignment.get('assigned_robot')) is not str
                        or self._twins.get(assignment['assigned_robot']) != agv_id
                        or assignment.get('shelf_id') != product['shelf_id']
                        or type(assignment.get('slot_index')) is not int
                        or not 0 <= assignment['slot_index'] < product['slot_count']
                        or assignment.get('reservation_id') != slot
                        or assignment.get('pickup_reservation_id') != pickup):
                    raise ValueError('synthetic attempt assignment differs')
                if db.execute('SELECT request_id,shelf_id,slot_index,state FROM main.slot_reservations '
                              'WHERE reservation_id=?', (slot,)).fetchone() != (
                                  request_id, product['shelf_id'], assignment['slot_index'], 'reserved'):
                    raise ValueError('synthetic attempt shelf reservation differs')
                pickup_state = {'uncertain': 'inflight', 'applied': 'consumed',
                                'not_applied': 'released', 'unknown': 'unknown'}[state]
                if state in ('unknown', 'not_applied'):
                    expected_blocked = 1
                if state in ('uncertain', 'unknown'):
                    active_pickups.add(pickup)
                all_pickups[pickup] = pickup_state
                if (metadata[1] < 1 or db.execute(
                        'SELECT request_id,source_id,product_type,state,order_id,action_id,agv_id,session_id, '
                        'dispatch_revision FROM pickup.reservations WHERE reservation_id=?', (pickup,)).fetchone() != (
                            request_id, product['pickup_node'], product['product_type'], pickup_state, task_id,
                            action_id, agv_id, self._generation, metadata[1])):
                    raise ValueError('synthetic attempt pickup reservation differs')
            inflight = db.execute("SELECT reservation_id,state FROM pickup.reservations "
                                  "WHERE state IN ('inflight','unknown') LIMIT ?", (len(self._twins) + 1,)).fetchall()
            if (len(inflight) != len(active_pickups)
                    or {reservation for reservation, _ in inflight} != active_pickups
                    or any(all_pickups.get(reservation) != state for reservation, state in inflight)):
                raise ValueError('orphan or unknown synthetic pickup reservation')
            if db.execute('SELECT generation,progress_blocked FROM main.synthetic_action_session '
                          'WHERE singleton=1').fetchall() != [(self._generation, expected_blocked)]:
                raise ValueError('synthetic progress fence differs')
            orphan = db.execute("SELECT 1 FROM pickup.reservations r WHERE r.state='consumed' "
                                "AND NOT EXISTS (SELECT 1 FROM main.synthetic_action_attempt a "
                                "WHERE a.pickup_reservation_id=r.reservation_id AND a.state='applied') LIMIT 1").fetchone()
            orphan_released = db.execute("SELECT 1 FROM pickup.reservations r WHERE r.state='released' AND "
                                         '(r.order_id IS NOT NULL OR r.action_id IS NOT NULL OR r.agv_id IS NOT NULL '
                                         'OR r.session_id IS NOT NULL OR r.dispatch_revision IS NOT NULL) '
                                         'AND NOT EXISTS (SELECT 1 FROM main.synthetic_action_attempt a '
                                         "WHERE a.pickup_reservation_id=r.reservation_id AND a.state='not_applied') "
                                         'LIMIT 1').fetchone()
            if orphan or orphan_released:
                raise ValueError('orphan resolved synthetic pickup reservation')

    @contextmanager
    def _transaction(self, *, initializing=False):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('synthetic transaction already in progress')
        try:
            if self._closed or self._blocked:
                raise RuntimeError('synthetic session is blocked')
            try:
                self._pair._validate_live()
                with closing(_open_connection(self._pair)) as db:
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        self._verify(db)
                        if not initializing and db.execute('SELECT session_id FROM pickup.metadata '
                                                           'WHERE singleton=1').fetchall() != [(self._generation,)]:
                            raise ValueError('synthetic session generation differs')
                        yield db
                        self._verify(db)
                        if initializing:
                            self._schema_baseline = self._schema(db)
                        db.execute('COMMIT')
                    except BaseException:
                        if db.in_transaction:
                            db.execute('ROLLBACK')
                        raise
                # A result may already be durable if this check fails: block,
                # preserve it, and never treat it as permission to continue.
                self._pair._validate_live()
            except _ProgressBlocked:
                raise
            except BaseException as exc:
                self._blocked = True
                if isinstance(exc, sqlite3.Error):
                    raise ValueError('synthetic transaction unavailable') from exc
                raise
        finally:
            self._lock.release()

    def stage_attempt(self, *, request_id, task_id, action_id, agv_id, action):
        for value, name in ((request_id, 'request ID'), (task_id, 'task ID'),
                            (action_id, 'action ID'), (agv_id, 'AGV ID')):
            _text(value, name, 128)
        if action != 'grab':
            raise ValueError('only synthetic pickup attempts are supported')
        with self._transaction() as db:
            if db.execute('SELECT progress_blocked FROM main.synthetic_action_session WHERE singleton=1').fetchone() != (0,):
                raise _ProgressBlocked('synthetic progress is blocked by a prior outcome')
            row = db.execute("SELECT product_id,result_json FROM main.orders WHERE request_id=? AND state='active'",
                             (request_id,)).fetchone()
            if row is None:
                raise ValueError('active simulated order required')
            assignment = _assignment_json(row[1])
            products = {item['id']: item for item in json.loads(self._pair._catalog_json)}
            product = products.get(row[0])
            if (not isinstance(assignment, dict) or not isinstance(product, dict)
                    or assignment.get('order_id') != task_id
                    or type(assignment.get('assigned_robot')) is not str
                    or self._twins.get(assignment.get('assigned_robot')) != agv_id
                    or assignment.get('shelf_id') != product['shelf_id']
                    or type(assignment.get('slot_index')) is not int
                    or not 0 <= assignment['slot_index'] < product['slot_count']):
                raise ValueError('simulated action assignment differs')
            slot_id, pickup_id = assignment.get('reservation_id'), assignment.get('pickup_reservation_id')
            if (type(slot_id) is not str or type(pickup_id) is not str
                    or db.execute('SELECT request_id,shelf_id,slot_index,state FROM main.slot_reservations '
                                  'WHERE reservation_id=?', (slot_id,)).fetchone() !=
                    (request_id, product['shelf_id'], assignment['slot_index'], 'reserved')):
                raise ValueError('simulated shelf reservation differs')
            if db.execute('SELECT request_id,source_id,product_type,state FROM pickup.reservations '
                          'WHERE reservation_id=?', (pickup_id,)).fetchone() != (
                              request_id, product['pickup_node'], product['product_type'], 'reserved'):
                raise ValueError('simulated pickup reservation differs')
            revision = db.execute('SELECT revision FROM pickup.metadata WHERE singleton=1').fetchone()[0]
            count = db.execute('SELECT available FROM pickup.counts WHERE source_id=?',
                               (product['pickup_node'],)).fetchone()
            held = db.execute("SELECT COUNT(*) FROM pickup.reservations WHERE source_id=? AND state IN "
                              "('reserved','inflight','unknown')", (product['pickup_node'],)).fetchone()[0]
            if type(revision) is not int or revision < 1 or count is None or type(count[0]) is not int or count[0] < held:
                raise ValueError('simulated pickup observation cannot cover held reservations')
            if db.execute('SELECT 1 FROM main.synthetic_action_attempt '
                          'WHERE action_id=? OR request_id=? OR task_id=? OR agv_id=?',
                          (action_id, request_id, task_id, agv_id)).fetchone() is not None:
                raise ValueError('synthetic action attempt already exists')
            updated = db.execute("UPDATE pickup.reservations SET state='inflight',order_id=?,action_id=?,agv_id=?, "
                                 "session_id=?,dispatch_revision=? WHERE reservation_id=? AND state='reserved' "
                                 'AND order_id IS NULL AND action_id IS NULL AND agv_id IS NULL '
                                 'AND session_id IS NULL AND dispatch_revision IS NULL',
                                 (task_id, action_id, agv_id, self._generation, revision, pickup_id)).rowcount
            if updated != 1:
                raise ValueError('simulated pickup reservation changed')
            db.execute('INSERT INTO main.synthetic_action_attempt VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (action_id, request_id, task_id, agv_id, action, 'uncertain', self._generation,
                        self._pair.pair_id, self._pair.world_id, pickup_id, slot_id))
        return {'action_id': action_id, 'state': 'uncertain'}

    def record_result(self, *, request_id, task_id, action_id, agv_id, world_id, session_id, outcome):
        """Record an in-process synthetic claim; no external source is authenticated."""
        for value, name in ((request_id, 'request ID'), (task_id, 'task ID'),
                            (action_id, 'action ID'), (agv_id, 'AGV ID')):
            _text(value, name, 128)
        if outcome not in ('applied', 'not_applied', 'unknown'):
            raise ValueError('invalid synthetic result')
        with self._transaction() as db:
            if world_id != self._pair.world_id or session_id != self._generation:
                raise ValueError('synthetic result world or generation differs')
            row = db.execute('SELECT request_id,task_id,agv_id,state,pickup_reservation_id '
                             'FROM main.synthetic_action_attempt WHERE action_id=?', (action_id,)).fetchone()
            if row is None or row[:3] != (request_id, task_id, agv_id):
                raise ValueError('synthetic result identity differs')
            if row[3] == outcome:
                return {'action_id': action_id, 'outcome': outcome, 'duplicate': True}
            if row[3] != 'uncertain':
                raise ValueError('synthetic result conflicts with recorded outcome')
            if db.execute('SELECT progress_blocked FROM main.synthetic_action_session WHERE singleton=1').fetchone() != (0,):
                raise _ProgressBlocked('synthetic progress is blocked by a prior outcome')
            pickup_id = row[4]
            pickup = db.execute('SELECT source_id FROM pickup.reservations WHERE reservation_id=?',
                                (pickup_id,)).fetchone()
            if pickup is None:
                raise ValueError('synthetic pickup reservation missing')
            if outcome == 'applied':
                count = db.execute('SELECT available FROM pickup.counts WHERE source_id=?', pickup).fetchone()
                held = db.execute("SELECT COUNT(*) FROM pickup.reservations WHERE source_id=? AND state IN "
                                  "('reserved','inflight','unknown')", pickup).fetchone()[0]
                if count is None or type(count[0]) is not int or count[0] < held or count[0] < 1:
                    raise ValueError('synthetic pickup stock cannot cover reservations')
                db.execute('UPDATE pickup.counts SET available=available-1 WHERE source_id=?', pickup)
                db.execute('UPDATE pickup.metadata SET mutation=mutation+1 WHERE singleton=1')
            pickup_state = {'applied': 'consumed', 'not_applied': 'released', 'unknown': 'unknown'}[outcome]
            if db.execute("UPDATE pickup.reservations SET state=? WHERE reservation_id=? AND state='inflight'",
                          (pickup_state, pickup_id)).rowcount != 1:
                raise ValueError('synthetic pickup result no longer matches')
            if db.execute("UPDATE main.synthetic_action_attempt SET state=? WHERE action_id=? AND state='uncertain'",
                          (outcome, action_id)).rowcount != 1:
                raise ValueError('synthetic attempt result no longer matches')
            if outcome in ('not_applied', 'unknown'):
                db.execute('UPDATE main.synthetic_action_session SET progress_blocked=1 WHERE singleton=1')
        return {'action_id': action_id, 'outcome': outcome, 'duplicate': False}

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._pair.close()
