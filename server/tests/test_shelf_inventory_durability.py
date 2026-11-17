"""Persistent simulated shelf reservations survive server object recreation."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest

from server.order_persistence import PersistentOrderStore
from server.order_shelf_inventory import ShelfInventory


CATALOG = [{'id': 'red', 'product_type': 'Red', 'pickup_node': 'P',
            'destination_node': 'D', 'shelf_id': 'RED', 'slot_count': 6}]


class ShelfDurabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='slot-durable-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'orders.sqlite3'

    def inventory(self):
        store = PersistentOrderStore(self.path, 'test-map@v1', CATALOG)
        return ShelfInventory({'RED': 'Red'}, slot_count=6, store=store)

    @staticmethod
    def sync(inventory, slots):
        owner = object()
        nonce = inventory.begin_session(owner)
        inventory.apply_snapshot(owner, {'request_id': nonce, 'shelves': [
            {'shelf_id': 'RED', 'product_type': 'Red', 'slots': slots}]})
        return owner

    def test_unresolved_reservation_survives_new_process_and_requires_fresh_snapshot(self):
        first = self.inventory()
        self.sync(first, [False] * 6)
        request_id = PersistentOrderStore(self.path, 'test-map@v1', CATALOG).submit('red')['request_id']
        one = first.reserve('RED', 'Red', request_id=request_id)
        self.assertEqual(one['slot_index'], 0)
        restored = self.inventory()
        self.assertFalse(restored.ready)
        persisted = PersistentOrderStore(self.path, 'test-map@v1', CATALOG).load_shelves()
        self.assertEqual(persisted['reservations'][0]['request_id'], request_id)
        with self.assertRaises(RuntimeError):
            restored.reserve('RED', 'Red')
        self.sync(restored, [False] * 6)
        two = restored.reserve('RED', 'Red')
        self.assertEqual(two['slot_index'], 1)
        self.assertNotEqual(one['reservation_id'], two['reservation_id'])
        with self.assertRaises(ValueError):
            restored.confirm(one['reservation_id'])

    def test_confirmed_slot_is_not_erased_by_an_empty_new_world_snapshot(self):
        first = self.inventory()
        self.sync(first, [False] * 6)
        first.confirm(first.reserve('RED', 'Red')['reservation_id'])
        restored = self.inventory()
        with self.assertRaises(RuntimeError):
            self.sync(restored, [False] * 6)
        self.assertFalse(restored.ready)
        nonce = restored.request_snapshot(restored._owner)
        restored.apply_snapshot(restored._owner, {'request_id': nonce, 'shelves': [
            {'shelf_id': 'RED', 'product_type': 'Red', 'slots': [True, False, False, False, False, False]}]})
        self.assertEqual(restored.reserve('RED', 'Red')['slot_index'], 1)

    def test_unconfirmed_occupied_snapshot_does_not_become_successful_ack(self):
        first = self.inventory()
        self.sync(first, [False] * 6)
        held = first.reserve('RED', 'Red')
        restored = self.inventory()
        self.sync(restored, [True, False, False, False, False, False])
        rows = PersistentOrderStore(self.path, 'test-map@v1', CATALOG).load_shelves()['reservations']
        self.assertEqual(rows[0]['reservation_id'], held['reservation_id'])
        self.assertEqual(rows[0]['state'], 'reserved')
        self.assertEqual(restored.reserve('RED', 'Red')['slot_index'], 1)

    def test_other_writer_refresh_makes_old_snapshot_unusable(self):
        first, second = self.inventory(), self.inventory()
        self.sync(first, [False] * 6)
        self.sync(second, [False] * 6)
        with self.assertRaises(RuntimeError):
            first.reserve('RED', 'Red')
        self.assertEqual(second.reserve('RED', 'Red')['slot_index'], 0)

    def test_concurrent_durable_requests_keep_distinct_slots_after_restart(self):
        first = self.inventory()
        self.sync(first, [False] * 6)
        store = PersistentOrderStore(self.path, 'test-map@v1', CATALOG)
        requests = [store.submit('red')['request_id'] for _ in range(2)]
        barrier = threading.Barrier(2)
        def reserve(request_id):
            barrier.wait(timeout=3)
            return first.reserve('RED', 'Red', request_id=request_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, requests))
        self.assertEqual({item['slot_index'] for item in results}, {0, 1})
        restored = self.inventory()
        self.sync(restored, [False] * 6)
        self.assertEqual(restored.reserve('RED', 'Red')['slot_index'], 2)
        held = PersistentOrderStore(self.path, 'test-map@v1', CATALOG).load_shelves()['reservations']
        self.assertEqual({item['request_id'] for item in held if item['request_id']}, set(requests))


if __name__ == '__main__':
    unittest.main()
