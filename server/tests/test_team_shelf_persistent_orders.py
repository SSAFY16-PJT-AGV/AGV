"""Durable web assignment must refer to the matching team slot claim."""
import tempfile
import unittest
from pathlib import Path

from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_persistent_orders import TeamShelfPersistentOrders
from server.tests.test_team_shelf_order_slots import snapshot

CATALOG = [{'id': 'milk', 'product_type': 'milk', 'shelf_id': 'N02',
            'pickup_node': 'D01', 'destination_node': 'D02', 'slot_count': 4}]


class TeamShelfPersistentOrdersTest(unittest.TestCase):
    def test_assignment_is_bound_to_the_correct_team_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            slots = TeamShelfOrderSlots.create_new(Path(folder) / 'slots.sqlite3')
            session = slots.begin_session('a' * 32)
            slots.apply_snapshot(session, snapshot(), fence=slots.begin_snapshot(session))
            orders = TeamShelfPersistentOrders(Path(folder) / 'orders.sqlite3', 'map', CATALOG, slots)
            item = orders.submit('milk', 'key')
            request = item['request_id']
            orders.claim(request)
            slots.reserve(session, request, 'milk')
            result = {'assigned_robot': 'sim-r1', 'order_id': 'web-pickup',
                      'shelf_id': 'N02', 'slot_index': 0, 'reservation_id': request}
            with self.assertRaises(ValueError):
                orders.mark_active(request, dict(result, slot_index=1))
            self.assertEqual(orders.get_order(request)['state'], 'dispatching')
            orders.mark_active(request, result)
            self.assertEqual(orders.get_order(request)['state'], 'active')

    def test_other_slot_db_cannot_open_durable_orders(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            slots = TeamShelfOrderSlots.create_new(path / 'one.db')
            TeamShelfPersistentOrders(path / 'orders.db', 'map', CATALOG, slots)
            other = TeamShelfOrderSlots.create_new(path / 'two.db')
            with self.assertRaises(ValueError):
                TeamShelfPersistentOrders(path / 'orders.db', 'map', CATALOG, other)


if __name__ == '__main__':
    unittest.main()
