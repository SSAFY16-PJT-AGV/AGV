import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from server.order_shelf_inventory import ShelfInventory


class ShelfInventoryTest(unittest.TestCase):
    def setUp(self):
        self.inv = ShelfInventory({'RED': 'Red', 'BLUE': 'Blue'}, slot_count=6)
        self.owner = object()

    def data(self, request_id, red=None, blue=None):
        return {'request_id': request_id, 'shelves': [
            {'shelf_id': 'RED', 'product_type': 'Red', 'slots': red or [False] * 6},
            {'shelf_id': 'BLUE', 'product_type': 'Blue', 'slots': blue or [False] * 6}]}

    def sync(self):
        request_id = self.inv.begin_session(self.owner)
        self.inv.apply_snapshot(self.owner, self.data(request_id))

    def test_reservation_requires_fresh_authenticated_session_snapshot(self):
        with self.assertRaises(RuntimeError):
            self.inv.reserve('RED', 'Red')
        request_id = self.inv.begin_session(self.owner)
        with self.assertRaises(RuntimeError):
            self.inv.reserve('RED', 'Red')
        with self.assertRaises(ValueError):
            self.inv.apply_snapshot(object(), self.data(request_id))
        self.inv.apply_snapshot(self.owner, self.data(request_id, red=[True, False, False, False, False, False]))
        reservation = self.inv.reserve('RED', 'Red')
        self.assertEqual(reservation['slot_index'], 1)
        with self.assertRaises(ValueError):
            self.inv.apply_snapshot(self.owner, self.data(request_id))

    def test_two_simultaneous_orders_cannot_reserve_same_slot(self):
        self.sync()
        barrier = threading.Barrier(2)
        def reserve():
            barrier.wait(timeout=5)
            return self.inv.reserve('RED', 'Red')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))
        self.assertEqual({item['slot_index'] for item in results}, {0, 1})
        self.assertNotEqual(results[0]['reservation_id'], results[1]['reservation_id'])

    def test_invalid_inventories_cannot_mark_session_ready(self):
        bad_shelves = [
            [{'shelf_id': 'RED', 'product_type': 'Red', 'slots': [False] * 6}],
            [{'shelf_id': 'RED', 'product_type': 'Red', 'slots': [False] * 6}] * 2,
            [{'shelf_id': 'RED', 'product_type': 'Wrong', 'slots': [False] * 6},
             {'shelf_id': 'BLUE', 'product_type': 'Blue', 'slots': [False] * 6}],
            [{'shelf_id': 'RED', 'product_type': 'Red', 'slots': [0] * 6},
             {'shelf_id': 'BLUE', 'product_type': 'Blue', 'slots': [False] * 6}],
            [{'shelf_id': 'RED', 'product_type': 'Red', 'slots': [False] * 5},
             {'shelf_id': 'BLUE', 'product_type': 'Blue', 'slots': [False] * 6}],
        ]
        for shelves in bad_shelves:
            with self.subTest(shelves=shelves):
                req = self.inv.begin_session(self.owner)
                with self.assertRaises(ValueError):
                    self.inv.apply_snapshot(self.owner, {'request_id': req, 'shelves': shelves})
                self.assertFalse(self.inv.ready)

    def test_unconfirmed_slot_is_never_freed_by_new_owner_snapshot(self):
        self.sync()
        first = self.inv.reserve('RED', 'Red')
        self.inv.invalidate(self.owner)
        next_owner = object()
        req = self.inv.begin_session(next_owner)
        self.inv.apply_snapshot(next_owner, self.data(req))
        second = self.inv.reserve('RED', 'Red')
        self.assertEqual((first['slot_index'], second['slot_index']), (0, 1))
        with self.assertRaises(ValueError):
            self.inv.confirm(first['reservation_id'])

    def test_confirmed_place_blocks_late_snapshot_from_erasing_occupancy(self):
        self.sync()
        first = self.inv.reserve('RED', 'Red')
        refresh = self.inv.request_snapshot(self.owner)
        self.inv.confirm(first['reservation_id'])
        with self.assertRaises(ValueError):
            self.inv.apply_snapshot(self.owner, self.data(refresh))
        fresh = self.inv.request_snapshot(self.owner)
        self.inv.apply_snapshot(self.owner, self.data(fresh, red=[True, False, False, False, False, False]))
        self.assertEqual(self.inv.reserve('RED', 'Red')['slot_index'], 1)

    def test_full_and_wrong_type_fail_without_reserving(self):
        req = self.inv.begin_session(self.owner)
        self.inv.apply_snapshot(self.owner, self.data(req, red=[True] * 6))
        with self.assertRaises(RuntimeError):
            self.inv.reserve('RED', 'Red')
        with self.assertRaises(ValueError):
            self.inv.reserve('RED', 'Blue')

    def test_can_reserve_checks_snapshot_and_unresolved_reservations(self):
        with self.assertRaises(RuntimeError):
            self.inv.can_reserve('RED', 'Red')
        self.sync()
        self.assertTrue(self.inv.can_reserve('RED', 'Red'))
        for _ in range(6):
            self.inv.reserve('RED', 'Red')
        self.assertFalse(self.inv.can_reserve('RED', 'Red'))
        self.assertTrue(self.inv.can_reserve('BLUE', 'Blue'))
        with self.assertRaises(ValueError):
            self.inv.can_reserve('RED', 'Blue')
        self.inv.invalidate(self.owner)
        new = object()
        request_id = self.inv.begin_session(new)
        self.inv.apply_snapshot(new, self.data(request_id))
        self.assertFalse(self.inv.can_reserve('RED', 'Red'))


if __name__ == '__main__':
    unittest.main()
