"""Durable web assignment must refer to the matching team slot claim."""
import tempfile
import unittest
import json
from pathlib import Path

from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_persistent_orders import (TeamShelfPersistentOrders,
    check_recovered_team_orders, check_recovered_team_snapshot, check_completed_action_bindings)
from server.tests.test_team_shelf_order_slots import snapshot

CATALOG = [{'id': 'milk', 'product_type': 'milk', 'shelf_id': 'N02',
            'pickup_node': 'D01', 'destination_node': 'D02', 'slot_count': 4}]


class TeamShelfPersistentOrdersTest(unittest.TestCase):
    def test_completed_slot_actions_match_engine_acknowledgements(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            slots = TeamShelfOrderSlots.create_new(path / 'slots.db')
            session = slots.begin_session('a' * 32)
            slots.apply_snapshot(session, snapshot(), fence=slots.begin_snapshot(session))
            journal = TeamShelfPersistentOrders(path / 'orders.db', 'map', CATALOG, slots)
            order = journal.submit('milk', 'key')
            request = order['request_id']
            journal.claim(request)
            slots.reserve(session, request, 'milk')
            result = {'assigned_robot': 'sim-r1', 'order_id': 'web-pickup', 'shelf_id': 'N02',
                      'slot_index': 0, 'reservation_id': request}
            journal.mark_active(request, result)
            pick = {'order_id': 'web-pickup', 'action_id': 'grab1', 'agv_id': 'AGV_01'}
            place = {'order_id': 'web-delivery', 'action_id': 'place1', 'agv_id': 'AGV_01'}
            slots.mark_dispatched(session, request, 'pick', 's1', binding=pick)
            slots.complete_action(session, request, 's1', True)
            slots.mark_dispatched(session, request, 'place', 's2', binding=place)
            slots.complete_action(session, request, 's2', True)
            checkpoint = {'pickup_task': 'web-pickup', 'delivery_task': 'web-delivery',
                          'robot_id': 'sim-r1', 'twin_id': 'AGV_01', 'product': 'milk',
                          'shelf': 'N02', 'slot': 0, 'destination': 'D02',
                          'action_ack': dict(place, action='place', applied=True)}
            journal.mark_completed(request, {'checkpoint': checkpoint})
            grab_ack = dict(pick, action='grab', applied=True)
            self.assertTrue(check_completed_action_bindings(journal, slots, lambda _: grab_ack))
            self.assertTrue(check_recovered_team_orders(journal, slots))
            with self.assertRaises(ValueError):
                check_completed_action_bindings(journal, slots,
                    lambda _: dict(grab_ack, action_id='another-grab'))
            with journal._write() as db:
                saved = json.loads(db.execute('SELECT result_json FROM orders WHERE request_id=?', (request,)).fetchone()[0])
                saved['slot_index'] = saved['checkpoint']['slot'] = 1
                db.execute('UPDATE orders SET result_json=? WHERE request_id=?', (json.dumps(saved), request))
            with self.assertRaises(ValueError):
                check_recovered_team_orders(journal, slots)

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

    def test_interrupted_order_keeps_claims_and_blocks_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            slots = TeamShelfOrderSlots.create_new(path / 'slots.db')
            session = slots.begin_session('a' * 32)
            slots.apply_snapshot(session, snapshot(), fence=slots.begin_snapshot(session))
            journal = TeamShelfPersistentOrders(path / 'orders.db', 'map', CATALOG, slots)
            order = journal.submit('milk', 'request-key')
            journal.claim(order['request_id'])
            slots.reserve(session, order['request_id'], 'milk')
            journal.recover_interrupted()
            slots = TeamShelfOrderSlots.open_existing(path / 'slots.db', expected_creation_id=slots.creation_id)
            journal = TeamShelfPersistentOrders(path / 'orders.db', 'map', CATALOG, slots)
            self.assertFalse(check_recovered_team_orders(journal, slots))
            self.assertEqual(journal.get_order_by_key('request-key')['state'], 'unknown')
            self.assertEqual({row['slot_id'] for row in slots.inspect()['claims']}, {1, 5})

    def test_slot_snapshot_mismatch_and_orphan_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            slots = TeamShelfOrderSlots.create_new(path / 'slots.db')
            session = slots.begin_session('a' * 32)
            original = snapshot()
            original[3] = {'slot_id': 4, 'state': 'empty', 'product_type': None}
            slots.apply_snapshot(session, original, fence=slots.begin_snapshot(session))
            journal = TeamShelfPersistentOrders(path / 'orders.db', 'map', CATALOG, slots)
            self.assertTrue(check_recovered_team_orders(journal, slots))
            self.assertTrue(check_recovered_team_snapshot(slots, 'a' * 32, original))
            wrong = [dict(row) for row in original]
            wrong[0].update(state='empty', product_type=None)
            with self.assertRaises(ValueError):
                check_recovered_team_snapshot(slots, 'a' * 32, wrong)
            with self.assertRaises(ValueError):
                check_recovered_team_snapshot(slots, 'b' * 32, original)
            slots.reserve(session, 'orphan', 'milk')
            with self.assertRaises(ValueError):
                check_recovered_team_orders(journal, slots)


if __name__ == '__main__':
    unittest.main()
