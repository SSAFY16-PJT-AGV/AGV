"""Fleet orders reserve different slots from the authenticated shelf state."""
import threading
import unittest

from server.order_fleet import FleetProductControlEngine
from server.order_shelf_inventory import ShelfInventory
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


if __name__ == '__main__':
    unittest.main()
