"""Durable web assignment must refer to the matching team slot claim."""
import tempfile
import unittest
import json
import sqlite3
from pathlib import Path

from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_persistent_orders import (TeamShelfPersistentOrders,
    check_recovered_team_orders, check_recovered_team_snapshot, check_completed_action_bindings)
from server.team_shelf_persistent_orders import check_completed_reroute_handoffs
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
            second = journal.submit('milk', 'key2')['request_id']
            journal.claim(second)
            slots.reserve(session, second, 'milk')
            journal.mark_active(second, {'assigned_robot': 'sim-r1', 'order_id': 'web-2-pickup',
                'shelf_id': 'N02', 'slot_index': 1, 'reservation_id': second})
            pick2 = {'order_id': 'web-2-pickup', 'action_id': 'grab2', 'agv_id': 'AGV_01'}
            place2 = {'order_id': 'web-2-delivery', 'action_id': 'place2', 'agv_id': 'AGV_01'}
            slots.mark_dispatched(session, second, 'pick', 's3', binding=pick2)
            slots.complete_action(session, second, 's3', True)
            slots.mark_dispatched(session, second, 'place', 's4', binding=place2)
            slots.complete_action(session, second, 's4', True)
            checkpoint2 = {'pickup_task': 'web-2-pickup', 'delivery_task': 'web-2-delivery',
                'robot_id': 'sim-r1', 'session': 'demo-session', 'twin_id': 'AGV_01', 'product': 'milk',
                'shelf': 'N02', 'slot': 1, 'destination': 'D02',
                'action_ack': dict(place2, action='place', applied=True)}
            journal.mark_completed(second, {'checkpoint': checkpoint2})
            # Reroute authorization requires the same completed web order,
            # team-slot action, and the source/target transfer in the sim DB.
            with sqlite3.connect(':memory:') as db:
                db.execute('CREATE TABLE sim_reroute_generations(task_id TEXT)')
                db.execute('CREATE TABLE sim_reroute_finishes(task_id TEXT)')
                db.execute('CREATE TABLE tasks(task_id TEXT,status TEXT)')
                db.execute('CREATE TABLE sim_handoff_transfers(source_task_id TEXT,target_task_id TEXT,robot_id TEXT,robot_session TEXT,ack_json TEXT)')
                checkpoint['session'] = 'demo-session'
                with journal._write() as orders_db:
                    saved = json.loads(orders_db.execute('SELECT result_json FROM orders WHERE request_id=?', (request,)).fetchone()[0])
                    saved['checkpoint'] = checkpoint
                    orders_db.execute('UPDATE orders SET result_json=? WHERE request_id=?', (json.dumps(saved), request))
                db.execute("INSERT INTO tasks VALUES('web-pickup','DONE')")
                db.execute("INSERT INTO sim_reroute_generations VALUES('web-pickup')")
                db.execute("INSERT INTO sim_reroute_finishes VALUES('web-pickup')")
                db.execute('INSERT INTO sim_handoff_transfers VALUES(?,?,?,?,?)',
                           ('web-pickup','web-delivery','sim-r1','demo-session',json.dumps(grab_ack)))
                db.execute('INSERT INTO sim_handoff_transfers VALUES(?,?,?,?,?)',
                           ('web-2-pickup','web-2-delivery','sim-r1','demo-session',
                            json.dumps(dict(pick2, action='grab', applied=True))))
                self.assertTrue(check_completed_reroute_handoffs(journal, slots, db))
                db.execute("UPDATE sim_handoff_transfers SET ack_json='{}' WHERE source_task_id='web-pickup'")
                with self.assertRaises(ValueError):
                    check_completed_reroute_handoffs(journal, slots, db)
                db.execute('UPDATE sim_handoff_transfers SET ack_json=? WHERE source_task_id=?',
                           (json.dumps(grab_ack), 'web-pickup'))
                # Latest completed delivery is still ASSIGNED until another
                # job takes over its terminal node; it must be cross-checked.
                db.execute("INSERT INTO tasks VALUES('web-delivery','ASSIGNED')")
                db.execute("INSERT INTO sim_reroute_generations VALUES('web-delivery')")
                db.execute("INSERT INTO sim_reroute_finishes VALUES('web-delivery')")
                self.assertTrue(check_completed_reroute_handoffs(journal, slots, db))
                db.execute("INSERT INTO tasks VALUES('other-delivery','ASSIGNED')")
                db.execute("INSERT INTO sim_reroute_generations VALUES('other-delivery')")
                db.execute("INSERT INTO sim_reroute_finishes VALUES('other-delivery')")
                with self.assertRaises(ValueError):
                    check_completed_reroute_handoffs(journal, slots, db)
                db.execute("DELETE FROM sim_reroute_finishes WHERE task_id='other-delivery'")
                # A prior rerouted delivery hands its held node to the next
                # pickup. Its place ACK must match that order's slot binding.
                db.execute("UPDATE tasks SET status='DONE' WHERE task_id='web-delivery'")
                db.execute('INSERT INTO sim_handoff_transfers VALUES(?,?,?,?,?)',
                           ('web-delivery','web-2-pickup','sim-r1','demo-session',
                            json.dumps(dict(place, action='place', applied=True))))
                self.assertTrue(check_completed_reroute_handoffs(journal, slots, db))
                db.execute("UPDATE sim_handoff_transfers SET ack_json='{}' WHERE source_task_id='web-delivery'")
                with self.assertRaises(ValueError):
                    check_completed_reroute_handoffs(journal, slots, db)
                db.execute('UPDATE sim_handoff_transfers SET ack_json=? WHERE source_task_id=?',
                           (json.dumps(dict(place, action='place', applied=True)), 'web-delivery'))
                db.execute("INSERT INTO tasks VALUES('unbound','DONE')")
                db.execute("INSERT INTO sim_reroute_generations VALUES('unbound')")
                db.execute("INSERT INTO sim_reroute_finishes VALUES('unbound')")
                with self.assertRaises(ValueError):
                    check_completed_reroute_handoffs(journal, slots, db)
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
