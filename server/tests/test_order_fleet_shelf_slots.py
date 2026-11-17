"""Fleet orders reserve different slots from the authenticated shelf state."""
import threading
import tempfile
import unittest
from pathlib import Path

from server.order_fleet import FleetProductControlEngine
from server.order_shelf_inventory import ShelfInventory
from server.order_persistence import PersistentOrderStore
from server.order_demo_app import PRODUCTS
from server.tests.test_order_fleet import FakeEngine
from server.tests.test_order_product_control import map_data


class FleetShelfSlotTests(unittest.TestCase):
    def setUp(self):
        self.inventory = ShelfInventory({'SHELF_RED': 'Red'})
        self.owner = object()
        request = self.inventory.begin_session(self.owner)
        self.inventory.apply_snapshot(self.owner, {'request_id': request, 'shelves': [
            {'shelf_id': 'SHELF_RED', 'product_type': 'Red',
             'slots': [True, False, False, True, False, False]}]})
        self.engines = {'sim-r1': FakeEngine('A', 'AGV_01'), 'sim-r2': FakeEngine('D', 'AGV_02')}
        self.fleet = FleetProductControlEngine(self.engines, map_data(), inventory=self.inventory)

    def test_simultaneous_same_product_orders_receive_distinct_empty_slots(self):
        barrier, results, errors = threading.Barrier(3), [], []
        def submit():
            barrier.wait()
            try:
                results.append(self.fleet.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0))
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=submit) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(5)
        self.assertEqual(errors, [])
        self.assertEqual({r['assigned_robot'] for r in results}, {'sim-r1', 'sim-r2'})
        self.assertEqual({r['slot_index'] for r in results}, {1, 2})

    def test_invalidated_snapshot_blocks_new_orders(self):
        self.inventory.invalidate(self.owner)
        with self.assertRaises(RuntimeError):
            self.fleet.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)

    def test_same_destination_waits_then_departing_owner_gets_next_order(self):
        self.engines['sim-r1'].state.update(status='moving', destination_node='D')
        self.assertFalse(self.fleet.can_dispatch_destination('D'))
        self.engines['sim-r1'].state.update(status='completed', current_node='D')
        self.engines['sim-r2'].state.update(status='idle', current_node='P')
        self.assertTrue(self.fleet.can_dispatch_destination('D'))
        order = self.fleet.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)
        self.assertEqual(order['assigned_robot'], 'sim-r1')

    def test_slot_reservation_is_linked_to_durable_order_before_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PersistentOrderStore(Path(folder) / 'orders.sqlite3', 'test@v1', PRODUCTS)
            inventory = ShelfInventory({'SHELF_RED': 'Red', 'SHELF_BLUE': 'Blue'}, store=store)
            owner = object()
            nonce = inventory.begin_session(owner)
            inventory.apply_snapshot(owner, {'request_id': nonce, 'shelves': [
                {'shelf_id': 'SHELF_RED', 'product_type': 'Red', 'slots': [False] * 6},
                {'shelf_id': 'SHELF_BLUE', 'product_type': 'Blue', 'slots': [False] * 6}]})
            request = store.submit('red')
            store.claim(request['request_id'])
            self.engines['sim-r1'].state['order_id'] = 'web-pickup'
            self.engines['sim-r2'].state['order_id'] = 'r2-web-pickup'
            fleet = FleetProductControlEngine(self.engines, map_data(), inventory=inventory)
            result = fleet.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0,
                                                request_id=request['request_id'])
            reservations = store.load_shelves()['reservations']
            self.assertEqual(reservations[0]['request_id'], request['request_id'])
            self.assertEqual(reservations[0]['reservation_id'], result['reservation_id'])
            store.mark_active(request['request_id'], result)


if __name__ == '__main__':
    unittest.main()
