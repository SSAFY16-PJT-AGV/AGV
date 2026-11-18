"""Required source isolation for an opt-in synthetic demo (not hardware evidence).

These tests describe the proposed source-aware constructors. Legacy databases
are treated as Unreal data; they cannot be silently reused by a synthetic peer.
"""
import tempfile
import unittest
import inspect
import sqlite3
from contextlib import closing
from pathlib import Path

from server.order_demo_actions import DemoActionSession
from server.order_persistence import PersistentOrderStore
from server.order_pickup_inventory import PickupInventory
from server.tests.test_order_pickup_ack_fence import Engine
import threading
from unittest.mock import patch


CATALOG = [dict(id='red', product_type='Red', pickup_node='D01',
                destination_node='D04', shelf_id='SHELF_RED', slot_count=3)]
WORLD = 'a' * 32
OTHER_WORLD = 'b' * 32


class SourceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orders = Path(self.tmp.name) / 'orders.sqlite3'
        self.pickup = Path(self.tmp.name) / 'pickup.sqlite3'

    def test_default_unreal_owner_behavior_remains_available(self):
        session = DemoActionSession(Engine(), threading.RLock(), settle_seconds=0)
        owner = object()
        self.assertTrue(session.attach(owner, authenticated=True))
        self.assertIsNotNone(session.poll(owner, now=0))
        session.detach(owner)

    def test_invalid_source_does_not_create_any_database(self):
        for source in ('', 'physical', None, True):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    PersistentOrderStore(self.orders, 'map-v1', CATALOG, action_source=source)
                with self.assertRaises(ValueError):
                    PickupInventory.create_new(self.pickup, {'D01': 'Red'}, action_source=source)
                self.assertFalse(self.orders.exists())
                self.assertFalse(self.pickup.exists())

    def test_legacy_unreal_database_versions_are_unchanged(self):
        PersistentOrderStore(self.orders, 'map-v1', CATALOG)
        PickupInventory.create_new(self.pickup, {'D01': 'Red'})
        for path, expected in ((self.orders, 2), (self.pickup, 1)):
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], expected)

    def test_changed_schema_marker_cannot_be_used_by_open_store(self):
        self.assertIn('action_source', inspect.signature(PersistentOrderStore).parameters)
        orders = PersistentOrderStore(self.orders, 'map-v1', CATALOG, action_source='synthetic')
        pickup = PickupInventory.create_new(self.pickup, {'D01': 'Red'}, action_source='synthetic')
        for path, version in ((self.orders, 2), (self.pickup, 1)):
            with closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA user_version={}'.format(version))
        with self.assertRaises(ValueError):
            orders.bind_world_id(WORLD)
        with self.assertRaises(ValueError):
            pickup.begin_session(WORLD)

    def test_marker_change_after_connect_rejects_writes_without_mutation(self):
        orders = PersistentOrderStore(self.orders, 'map-v1', CATALOG, action_source='synthetic')
        pickup = PickupInventory.create_new(self.pickup, {'D01': 'Red'}, action_source='synthetic')
        for store, path, old_version, operation in (
                (orders, self.orders, 2, lambda: orders.bind_world_id(WORLD)),
                (pickup, self.pickup, 1, lambda: pickup.begin_session(WORLD))):
            with self.subTest(path=path.name):
                connect = store._connect
                def change_marker_after_connect():
                    db = connect()
                    with closing(sqlite3.connect(path)) as other:
                        other.execute('PRAGMA user_version={}'.format(old_version))
                    return db
                with patch.object(store, '_connect', side_effect=change_marker_after_connect):
                    with self.assertRaises(ValueError):
                        operation()
                with closing(sqlite3.connect(path)) as check:
                    self.assertIsNone(check.execute('SELECT world_id FROM metadata').fetchone()[0])

    def test_marker_change_after_connect_rejects_reads(self):
        for operation in ('world', 'shelves', 'pickup'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'data.sqlite3'
                if operation == 'pickup':
                    store = PickupInventory.create_new(path, {'D01': 'Red'}, action_source='synthetic')
                    old_version = 1
                    read = store.snapshot
                else:
                    store = PersistentOrderStore(path, 'map-v1', CATALOG, action_source='synthetic')
                    old_version = 2
                    read = (lambda: store.world_id) if operation == 'world' else store.load_shelves
                connect = store._connect
                def change_marker_after_connect():
                    db = connect()
                    with closing(sqlite3.connect(path)) as other:
                        other.execute('PRAGMA user_version={}'.format(old_version))
                    return db
                with patch.object(store, '_connect', side_effect=change_marker_after_connect):
                    with self.assertRaises(ValueError):
                        read()

    def test_read_marker_and_data_share_snapshot_when_writer_commits(self):
        for operation in ('world', 'shelves', 'pickup'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'data.sqlite3'
                if operation == 'pickup':
                    store = PickupInventory.create_new(path, {'D01': 'Red'}, action_source='synthetic')
                    store.begin_session(WORLD)
                    old_version, trigger = 1, 'SELECT world_id,session_id,revision'
                    read = lambda: store.snapshot()['world_id']
                    expected = WORLD
                else:
                    store = PersistentOrderStore(path, 'map-v1', CATALOG, action_source='synthetic')
                    store.bind_world_id(WORLD)
                    old_version = 2
                    if operation == 'world':
                        trigger = 'SELECT world_id FROM metadata'
                        read = lambda: store.world_id
                        expected = WORLD
                    else:
                        trigger = 'SELECT shelf_revision FROM metadata'
                        read = lambda: store.load_shelves()['revision']
                        expected = 0
                with closing(sqlite3.connect(path)) as writer:
                    self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                connect = store._connect
                committed = []
                def traced_connect():
                    db = connect()
                    def before_data_query(sql):
                        if sql.startswith(trigger) and not committed:
                            with closing(sqlite3.connect(path, isolation_level=None)) as other:
                                other.execute('BEGIN IMMEDIATE')
                                if operation == 'pickup':
                                    other.execute('UPDATE metadata SET world_id=?', (OTHER_WORLD,))
                                else:
                                    other.execute('UPDATE metadata SET world_id=?,shelf_revision=7', (OTHER_WORLD,))
                                other.execute('PRAGMA user_version={}'.format(old_version))
                                other.execute('COMMIT')
                                committed.append(True)
                    db.set_trace_callback(before_data_query)
                    return db
                with patch.object(store, '_connect', side_effect=traced_connect):
                    self.assertEqual(read(), expected)
                self.assertEqual(committed, [True], 'writer must commit before the data SELECT')
                with closing(sqlite3.connect(path)) as verify:
                    self.assertEqual(verify.execute('PRAGMA user_version').fetchone()[0], old_version)
                    self.assertEqual(verify.execute('SELECT world_id FROM metadata').fetchone()[0], OTHER_WORLD)

    def test_write_transaction_fences_competing_marker_change(self):
        orders = PersistentOrderStore(self.orders, 'map-v1', CATALOG, action_source='synthetic')
        pickup = PickupInventory.create_new(self.pickup, {'D01': 'Red'}, action_source='synthetic')
        for store, path, version, operation in (
                (orders, self.orders, 2, lambda: orders.bind_world_id(WORLD)),
                (pickup, self.pickup, 1, lambda: pickup.begin_session(WORLD))):
            with self.subTest(path=path.name):
                check = store._check_source
                attempts = []
                def checked(db):
                    check(db)
                    with closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as other:
                        with self.assertRaises(sqlite3.OperationalError) as failure:
                            other.execute('PRAGMA user_version={}'.format(version))
                        attempts.append(failure.exception.sqlite_errorcode)
                with patch.object(store, '_check_source', side_effect=checked):
                    operation()
                self.assertEqual(attempts, [sqlite3.SQLITE_BUSY])
                with closing(sqlite3.connect(path)) as verify:
                    self.assertEqual(verify.execute('SELECT world_id FROM metadata').fetchone()[0], WORLD)

    def test_existing_unreal_order_database_rejects_synthetic_source(self):
        original = PersistentOrderStore(self.orders, 'map-v1', CATALOG)
        original.bind_world_id(WORLD)
        self.assertIn('action_source', inspect.signature(PersistentOrderStore).parameters)
        with self.assertRaises(ValueError):
            PersistentOrderStore(self.orders, 'map-v1', CATALOG, action_source='synthetic')
        reopened = PersistentOrderStore(self.orders, 'map-v1', CATALOG)
        self.assertEqual(reopened.world_id, WORLD)
        self.assertEqual(reopened.load_shelves()['reservations'], [])

    def test_synthetic_order_database_rejects_default_unreal_and_different_world(self):
        self.assertIn('action_source', inspect.signature(PersistentOrderStore).parameters)
        synthetic = PersistentOrderStore(self.orders, 'map-v1', CATALOG, action_source='synthetic')
        synthetic.bind_world_id(WORLD)
        with self.assertRaises(ValueError):
            PersistentOrderStore(self.orders, 'map-v1', CATALOG)
        reopened = PersistentOrderStore(self.orders, 'map-v1', CATALOG, action_source='synthetic')
        self.assertEqual(reopened.world_id, WORLD)
        with self.assertRaises(ValueError):
            reopened.bind_world_id(OTHER_WORLD)
        self.assertEqual(reopened.world_id, WORLD)

    def test_existing_unreal_pickup_database_rejects_synthetic_source(self):
        original = PickupInventory.create_new(self.pickup, {'D01': 'Red'})
        original.begin_session(WORLD)
        self.assertIn('action_source', inspect.signature(PickupInventory.open_existing).parameters)
        with self.assertRaises(ValueError):
            PickupInventory.open_existing(self.pickup, {'D01': 'Red'},
                                          expected_creation_id=original.creation_id,
                                          action_source='synthetic')
        reopened = PickupInventory.open_existing(self.pickup, {'D01': 'Red'},
                                                 expected_creation_id=original.creation_id)
        self.assertEqual(reopened.snapshot()['world_id'], WORLD)

    def test_synthetic_pickup_database_rejects_unreal_and_different_world(self):
        self.assertIn('action_source', inspect.signature(PickupInventory.create_new).parameters)
        original = PickupInventory.create_new(self.pickup, {'D01': 'Red'}, action_source='synthetic')
        original.begin_session(WORLD)
        with self.assertRaises(ValueError):
            PickupInventory.open_existing(self.pickup, {'D01': 'Red'},
                                          expected_creation_id=original.creation_id)
        reopened = PickupInventory.open_existing(self.pickup, {'D01': 'Red'},
                                                 expected_creation_id=original.creation_id,
                                                 action_source='synthetic')
        with self.assertRaises(ValueError):
            reopened.begin_session(OTHER_WORLD)
        self.assertEqual(reopened.snapshot()['world_id'], WORLD)

    def test_default_unreal_session_rejects_synthetic_owner(self):
        session = DemoActionSession(Engine(), threading.RLock(), settle_seconds=0)
        self.assertIn('source', inspect.signature(session.attach).parameters)
        synthetic_owner = object()
        self.assertFalse(session.attach(synthetic_owner, authenticated=True, source='synthetic'))
        unreal_owner = object()
        self.assertTrue(session.attach(unreal_owner, authenticated=True))
        self.assertIsNotNone(session.poll(unreal_owner, now=0))
        session.detach(unreal_owner)

    def test_synthetic_session_rejects_unreal_owner(self):
        self.assertIn('action_source', inspect.signature(DemoActionSession).parameters)
        session = DemoActionSession(Engine(), threading.RLock(), settle_seconds=0,
                                    action_source='synthetic')
        self.assertFalse(session.attach(object(), authenticated=True))
        synthetic_owner = object()
        self.assertTrue(session.attach(synthetic_owner, authenticated=True, source='synthetic'))
        self.assertIsNotNone(session.poll(synthetic_owner, now=0))
        session.detach(synthetic_owner)

    def test_synthetic_result_requires_same_owner_and_correlated_action(self):
        engine = Engine()
        session = DemoActionSession(engine, threading.RLock(), settle_seconds=0,
                                    action_source='synthetic')
        owner = object()
        self.assertTrue(session.attach(owner, authenticated=True, source='synthetic'))
        self.assertIsNotNone(session.poll(owner, now=0))
        data = dict(engine.action, applied=True)
        result = {'type': 'robot.action.result', 'data': data}
        self.assertFalse(session.receive(object(), result, now=1))
        for key in ('order_id', 'action_id', 'agv_id', 'action'):
            altered = dict(data, **{key: 'wrong'})
            self.assertFalse(session.receive(owner, {'type': 'robot.action.result', 'data': altered}, now=1))
            self.assertEqual(engine.applied, 0)
        self.assertTrue(session.receive(owner, result, now=1))
        self.assertFalse(session.receive(owner, result, now=1))
        self.assertEqual(engine.applied, 1)
        session.detach(owner)

    def test_synthetic_disconnect_preserves_inflight_pickup_as_unknown(self):
        self.assertIn('action_source', inspect.signature(PickupInventory.create_new).parameters)
        store = PickupInventory.create_new(self.pickup, {'D01': 'Red'}, action_source='synthetic')
        token = store.begin_session(WORLD)
        fence = store.begin_snapshot(token)
        store.apply_snapshot(token, [{'source_id': 'D01', 'product_type': 'Red', 'available': 1}],
                             snapshot_fence=fence)
        rid = store.reserve(token, 'request-1', 'D01', 'Red')['reservation_id']
        engine = Engine()
        def prepare(action):
            store.mark_dispatched(token, rid, action['order_id'], action['action_id'], action['agv_id'])
        session = DemoActionSession(engine, threading.RLock(), settle_seconds=0, action_source='synthetic',
                                    on_prepare_action=prepare, on_detach=lambda: store.end_session(token))
        owner = object()
        self.assertTrue(session.attach(owner, authenticated=True, source='synthetic'))
        self.assertIsNotNone(session.poll(owner, now=0))
        self.assertEqual(store.snapshot()['reservations'][0]['state'], 'inflight')
        session.detach(owner)
        state = store.snapshot()
        self.assertEqual(state['reservations'][0]['state'], 'unknown')
        self.assertEqual(state['sources']['D01']['available'], 1)
        self.assertEqual(state['sources']['D01']['reserved'], 1)
        self.assertFalse(state['fresh'])
        self.assertGreaterEqual(engine.unknown, 1)


if __name__ == '__main__':
    unittest.main()
