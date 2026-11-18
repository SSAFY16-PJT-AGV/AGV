"""TDD contract for a new isolated synthetic pair; no device evidence.

This spec deliberately requires fresh storage and blocks automatic reuse after
restart. Existing UE databases are never migrated or removed by these tests.
"""
from contextlib import closing
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import subprocess
import sys
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from server.order_persistence import PersistentOrderStore
from server.order_pickup_inventory import PickupInventory


CATALOG = [dict(id='red', product_type='Red', pickup_node='D01',
                destination_node='D04', shelf_id='SHELF_RED', slot_count=3)]
SOURCES = {'D01': 'Red'}
WORLD = 'a' * 32
OTHER_WORLD = 'b' * 32


class SyntheticDbPairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'synthetic-pair'

    def api(self):
        try:
            return importlib.import_module('server.synthetic_action_db_pair').SyntheticActionDbPair
        except ModuleNotFoundError as exc:
            if exc.name != 'server.synthetic_action_db_pair':
                raise
            self.fail('synthetic pair source/world binding is not implemented')

    def create(self, path=None, world=WORLD):
        return self.api().create_new(path or self.path, map_id='map-v1', catalog=CATALOG,
                                     pickup_sources=SOURCES, world_id=world)

    def inspect(self, pair, path=None, world=WORLD):
        return self.api().inspect_existing(path or self.path, map_id='map-v1', catalog=CATALOG,
                                           pickup_sources=SOURCES, expected_pair_id=pair.pair_id,
                                           expected_world_id=world)

    def test_new_pair_binds_both_databases_and_independently_expected_world(self):
        pair = self.create()
        self.assertRegex(pair.pair_id, r'^[0-9a-f]{64}$')
        self.assertRegex(pair.order_creation_id, r'^[0-9a-f]{64}$')
        self.assertEqual(pair.world_id, WORLD)
        self.assertEqual(pair.orders.world_id, WORLD)
        self.assertEqual(pair.pickup.snapshot()['world_id'], WORLD)
        self.assertEqual(pair.orders.pickup_id, pair.pickup.creation_id)
        bindings = []
        for name in ('orders.sqlite3', 'pickup-inventory.sqlite3'):
            with closing(sqlite3.connect(self.path / name)) as db:
                bindings.append(db.execute('SELECT pair_id,world_id,order_creation_id,pickup_creation_id '
                                           'FROM synthetic_pair_binding').fetchone())
        self.assertEqual(bindings, [(pair.pair_id, WORLD, pair.order_creation_id, pair.pickup.creation_id)] * 2)
        with self.assertRaises(ValueError):
            PersistentOrderStore(self.path / 'orders.sqlite3', 'map-v1', CATALOG)
        with self.assertRaises(ValueError):
            PickupInventory.open_existing(self.path / 'pickup-inventory.sqlite3', SOURCES,
                                          expected_creation_id=pair.pickup.creation_id)
        pair.close()
        report = self.inspect(pair)
        self.assertEqual(report['pair_id'], pair.pair_id)
        self.assertEqual(report['world_id'], WORLD)
        self.assertFalse(report['operable'])

    def test_existing_directory_or_legacy_database_is_never_reused_or_changed(self):
        self.path.mkdir()
        orders = self.path / 'orders.sqlite3'
        old = PersistentOrderStore(orders, 'map-v1', CATALOG)
        old.bind_world_id(WORLD)
        before = orders.read_bytes()
        with self.assertRaises((FileExistsError, ValueError)):
            self.create()
        self.assertEqual(orders.read_bytes(), before)
        self.assertFalse((self.path / 'pickup-inventory.sqlite3').exists())
        self.assertEqual(PersistentOrderStore(orders, 'map-v1', CATALOG).world_id, WORLD)

    def test_partial_creation_is_preserved_and_cannot_be_opened_or_recreated(self):
        api = self.api()
        with patch.object(PickupInventory, 'create_new', side_effect=RuntimeError('test creation failure')):
            with self.assertRaisesRegex(RuntimeError, 'test creation failure'):
                self.create()
        orders = self.path / 'orders.sqlite3'
        self.assertTrue(orders.exists(), 'the already created DB must not be removed')
        before = orders.read_bytes()
        with self.assertRaises((FileExistsError, ValueError)):
            self.create()
        with self.assertRaises(ValueError):
            api.inspect_existing(self.path, map_id='map-v1', catalog=CATALOG, pickup_sources=SOURCES,
                                 expected_pair_id='0' * 64, expected_world_id=WORLD)
        self.assertEqual(orders.read_bytes(), before)

    def test_final_validation_failure_preserves_marker_and_inspection_remains_nonoperable(self):
        api = self.api()
        with patch.object(api, '_validate_live', side_effect=RuntimeError('injected final validation failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected final validation failure'):
                self.create()
        marker = json.loads((self.path / 'pair-ready.json').read_text(encoding='utf-8'))
        report = api.inspect_existing(self.path, map_id='map-v1', catalog=CATALOG,
                                      pickup_sources=SOURCES, expected_pair_id=marker['pair_id'],
                                      expected_world_id=WORLD)
        self.assertFalse(report['operable'])
        with self.assertRaises(RuntimeError):
            api.open_existing(self.path, map_id='map-v1', catalog=CATALOG,
                              pickup_sources=SOURCES, expected_pair_id=marker['pair_id'],
                              expected_world_id=WORLD)
        self.assertTrue((self.path / 'orders.sqlite3').exists())
        self.assertTrue((self.path / 'pickup-inventory.sqlite3').exists())

    def test_same_world_other_pairs_pickup_database_is_rejected(self):
        pair1 = self.create()
        pair2 = self.create(self.root / 'other-pair')
        self.assertNotEqual(pair1.pair_id, pair2.pair_id)
        pair1.close()
        pair2.close()
        shutil.copyfile(self.root / 'other-pair' / 'pickup-inventory.sqlite3',
                        self.path / 'pickup-inventory.sqlite3')
        with self.assertRaises(ValueError):
            self.inspect(pair1)

    def test_mismatched_world_in_either_database_is_rejected(self):
        pair = self.create()
        pair.close()
        with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
            db.execute('UPDATE metadata SET world_id=?', (OTHER_WORLD,))
            db.commit()
        with self.assertRaises(ValueError):
            self.inspect(pair)

    def test_wrong_external_pair_or_world_is_rejected(self):
        pair = self.create()
        pair.close()
        api = self.api()
        for wrong_pair, wrong_world in (('f' * 64, WORLD), (pair.pair_id, OTHER_WORLD)):
            with self.subTest(wrong_pair=wrong_pair, wrong_world=wrong_world):
                with self.assertRaises(ValueError):
                    api.inspect_existing(self.path, map_id='map-v1', catalog=CATALOG, pickup_sources=SOURCES,
                                         expected_pair_id=wrong_pair, expected_world_id=wrong_world)

    def test_reopen_never_enables_a_lost_synthetic_world(self):
        pair = self.create()
        pair.close()
        before = {p.name: p.read_bytes() for p in self.path.glob('*.sqlite3')}
        report = self.inspect(pair)
        self.assertFalse(report['operable'])
        with self.assertRaises(RuntimeError):
            self.api().open_existing(self.path, map_id='map-v1', catalog=CATALOG, pickup_sources=SOURCES,
                                     expected_pair_id=pair.pair_id, expected_world_id=WORLD)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.path.glob('*.sqlite3')})

    def test_live_pair_excludes_inspection_and_close_revokes_store_access(self):
        pair = self.create()
        orders = pair.orders
        with self.assertRaises(RuntimeError):
            self.inspect(pair)
        pair.close()
        with self.assertRaises(RuntimeError):
            _ = orders.world_id
        self.assertFalse(self.inspect(pair)['operable'])

    def test_simultaneous_creation_never_shares_a_directory(self):
        api = self.api()
        barrier = threading.Barrier(2)
        def attempt():
            barrier.wait(5)
            try:
                return api.create_new(self.path, map_id='map-v1', catalog=CATALOG,
                                      pickup_sources=SOURCES, world_id=WORLD)
            except FileExistsError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        winners = [value for value in outcomes if value is not None]
        self.assertEqual(len(winners), 1)
        winners[0].close()
        self.assertFalse(self.inspect(winners[0])['operable'])

    def test_live_use_rejects_other_database_binding_change(self):
        pair = self.create()
        try:
            with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
                db.execute('UPDATE synthetic_pair_binding SET pair_id=?', ('f' * 64,))
                db.commit()
            with self.assertRaises(ValueError):
                _ = pair.orders.world_id
        finally:
            pair.close()

    def test_live_use_rejects_same_content_file_replacement(self):
        for filename in ('orders.sqlite3', 'pickup-inventory.sqlite3', 'pair-ready.json'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'pair'
                pair = self.create(path)
                try:
                    target = path / filename
                    duplicate = path / 'duplicate.tmp'
                    shutil.copyfile(target, duplicate)
                    os.replace(duplicate, target)
                    with self.assertRaises(ValueError):
                        _ = pair.orders.world_id
                finally:
                    pair.close()

    def test_pair_does_not_expose_writes_without_atomic_binding_guard(self):
        pair = self.create()
        try:
            for operation in (lambda: pair.orders.submit('red'),
                              lambda: pair.orders.bind_world_id(WORLD),
                              lambda: pair.pickup.begin_session(WORLD)):
                with self.assertRaises(AttributeError):
                    operation()
            self.assertEqual(pair.orders.list_orders(), [])
            self.assertEqual(pair.pickup.snapshot()['reservations'], [])
        finally:
            pair.close()

    def test_public_read_views_do_not_hold_writable_store_handles(self):
        pair = self.create()
        try:
            orders_view, pickup_view = pair.orders, pair.pickup
            with self.assertRaises(AttributeError):
                orders_view._store.submit('red')
            with self.assertRaises(AttributeError):
                pickup_view._store.begin_session(WORLD)
            self.assertEqual(orders_view.list_orders(), [])
            self.assertEqual(pickup_view.snapshot()['reservations'], [])
        finally:
            pair.close()
        with self.assertRaises(RuntimeError):
            orders_view.list_orders()
        with self.assertRaises(RuntimeError):
            pickup_view.snapshot()

    def test_read_view_result_cannot_be_shadowed_across_close(self):
        pair = self.create()
        self.addCleanup(pair.close)
        reader = pair.orders
        self.assertEqual(reader.world_id, WORLD)
        with self.assertRaises(AttributeError):
            reader.world_id = 'spoofed'
        pair.close()
        with self.assertRaises(RuntimeError):
            _ = reader.world_id

    def test_public_identity_and_nonoperable_flag_are_immutable_after_close(self):
        pair = self.create()
        self.addCleanup(pair.close)
        original = (pair.pair_id, pair.world_id, pair.order_creation_id)
        self.assertFalse(pair.operable)
        for name, value in (('pair_id', 'f' * 64), ('world_id', OTHER_WORLD),
                            ('order_creation_id', 'e' * 64), ('operable', True)):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    setattr(pair, name, value)
        pair.close()
        self.assertFalse(pair.operable)
        self.assertEqual((pair.pair_id, pair.world_id, pair.order_creation_id), original)
        for name in ('pair_id', 'world_id', 'order_creation_id', 'operable'):
            with self.subTest(name=name, phase='closed'):
                with self.assertRaises(AttributeError):
                    setattr(pair, name, None)

    def test_read_views_only_open_read_only_sqlite_connections(self):
        pair = self.create()
        self.addCleanup(pair.close)
        connect = sqlite3.connect
        paths = []
        def capture(path, *args, **kwargs):
            paths.append(path)
            return connect(path, *args, **kwargs)
        with patch('server.synthetic_action_db_pair.sqlite3.connect', side_effect=capture):
            self.assertEqual(pair.orders.world_id, WORLD)
            self.assertEqual(pair.pickup.snapshot()['world_id'], WORLD)
        self.assertTrue(paths)
        self.assertTrue(all(path.endswith('?mode=ro') for path in paths))

    def test_read_connection_itself_rejects_sqlite_mutation(self):
        pair = self.create()
        self.addCleanup(pair.close)
        with self.assertRaises(ValueError):
            pair._read_target(0, lambda db: db.execute("UPDATE metadata SET world_id='b'"))
        self.assertEqual(pair.orders.world_id, WORLD)

    def test_read_views_return_current_orders_shelves_and_pickup_metadata(self):
        pair = self.create()
        self.addCleanup(pair.close)
        with closing(sqlite3.connect(self.path / 'orders.sqlite3')) as db:
            db.execute("INSERT INTO orders(request_id,request_key,fingerprint,product_id,state) "
                       "VALUES ('request-1',NULL,'product:red','red','queued')")
            db.execute("UPDATE shelf_slots SET observed=0 WHERE shelf_id='SHELF_RED'")
            db.commit()
        orders = pair.orders.list_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]['request_id'], 'request-1')
        self.assertEqual(pair.orders.get_order('request-1'), orders[0])
        self.assertEqual(pair.orders.peek_next(), orders[0])
        self.assertEqual(pair.orders.load_shelves()['shelves']['SHELF_RED']['slots'], [False] * 3)
        pickup = pair.pickup.snapshot()
        self.assertEqual(pickup['world_id'], WORLD)
        self.assertFalse(pickup['fresh'])
        self.assertIsNone(pickup['sources']['D01']['allocatable'])

    def test_separate_process_cannot_inspect_a_live_pair(self):
        pair = self.create()
        code = '''
import json, sys
from server.synthetic_action_db_pair import SyntheticActionDbPair
data = json.load(sys.stdin)
try:
    SyntheticActionDbPair.inspect_existing(**data)
except RuntimeError:
    print("locked")
else:
    raise AssertionError("live pair was not locked")
'''
        data = dict(path=str(self.path), map_id='map-v1', catalog=CATALOG, pickup_sources=SOURCES,
                    expected_pair_id=pair.pair_id, expected_world_id=WORLD)
        try:
            result = subprocess.run([sys.executable, '-c', code], input=json.dumps(data), text=True,
                                    capture_output=True, timeout=20, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'locked')
        finally:
            pair.close()
        self.assertFalse(self.inspect(pair)['operable'])

    def test_fresh_process_can_inspect_but_not_resume_lost_world(self):
        pair = self.create()
        pair.close()
        data = dict(path=str(self.path), map_id='map-v1', catalog=CATALOG, pickup_sources=SOURCES,
                    expected_pair_id=pair.pair_id, expected_world_id=WORLD)
        code = '''
import json, sys
from server.synthetic_action_db_pair import SyntheticActionDbPair
data = json.load(sys.stdin)
report = SyntheticActionDbPair.inspect_existing(**data)
assert report["operable"] is False
try:
    SyntheticActionDbPair.open_existing(**data)
except RuntimeError:
    print("blocked")
else:
    raise AssertionError("lost world was resumed")
'''
        result = subprocess.run([sys.executable, '-c', code], input=json.dumps(data), text=True,
                                capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'blocked')

    def test_external_database_exclusive_lock_blocks_inspection(self):
        pair = self.create()
        pair.close()
        with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3', isolation_level=None)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            try:
                with self.assertRaises(ValueError):
                    self.inspect(pair)
            finally:
                writer.execute('ROLLBACK')
        self.assertFalse(self.inspect(pair)['operable'])


if __name__ == '__main__':
    unittest.main()
