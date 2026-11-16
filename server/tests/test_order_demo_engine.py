import math
import unittest

from server.order_demo_engine import OrderDemoEngine


def example_map():
    return {
        'map_id': 'warehouse_test', 'coordinate_unit': 'meter',
        'nodes': [
            {'id': 'A', 'x': 0, 'y': 0},
            {'id': 'B', 'x': 1, 'y': 0},
            {'id': 'C', 'x': 1, 'y': 1},
            {'id': 'D', 'x': 2, 'y': 1},
        ],
        'edges': [
            {'from': 'A', 'to': 'B', 'points': [{'x': 0.5, 'y': 0.5}]},
            {'from': 'B', 'to': 'C', 'points': []},
            {'from': 'C', 'to': 'D', 'points': []},
        ],
    }


class OrderDemoEngineTests(unittest.TestCase):
    def test_order_plans_from_current_to_pickup_then_destination(self):
        engine = OrderDemoEngine(example_map())
        result = engine.create_order('B', 'D')
        self.assertEqual(result['route'], ['A', 'B', 'C', 'D'])
        self.assertEqual(result['status'], 'moving')
        self.assertEqual(result['phase'], 'to_pickup')
        self.assertEqual(result['current_node'], 'A')
        self.assertEqual(result['pose']['x'], 0)
        self.assertEqual(result['battery'], 0.8)

    def test_pose_moves_through_edge_points_then_changes_phase(self):
        engine = OrderDemoEngine(example_map())
        engine.create_order('B', 'D')
        state = engine.tick(math.sqrt(0.5))
        self.assertAlmostEqual(state['pose']['x'], 500)
        self.assertAlmostEqual(state['pose']['y'], 500)
        self.assertEqual(state['current_node'], 'A')
        state = engine.tick(math.sqrt(0.5))
        self.assertEqual(state['current_node'], 'B')
        self.assertEqual(state['phase'], 'to_destination')
        state = engine.tick(2)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['phase'], 'done')
        self.assertEqual(state['current_node'], 'D')
        self.assertAlmostEqual(state['pose']['x'], 2000)
        self.assertAlmostEqual(state['pose']['y'], 1000)
        self.assertAlmostEqual(state['pose']['th'], 0)

    def test_reverse_edges_use_reversed_geometry_for_later_orders(self):
        engine = OrderDemoEngine(example_map())
        engine.create_order('A', 'B')
        first = engine.tick(2)
        self.assertEqual(first['status'], 'completed')
        second = engine.create_order('B', 'A')
        self.assertNotEqual(first['order_id'], second['order_id'])
        self.assertEqual(second['route'], ['B', 'A'])
        mid = engine.tick(math.sqrt(0.5))
        self.assertAlmostEqual(mid['pose']['x'], 500)
        self.assertAlmostEqual(mid['pose']['y'], 500)
        self.assertEqual(mid['current_node'], 'B')
        done = engine.tick(1)
        self.assertEqual(done['current_node'], 'A')
        self.assertEqual(done['status'], 'completed')

    def test_invalid_order_and_busy_order_do_not_replace_current_one(self):
        engine = OrderDemoEngine(example_map())
        with self.assertRaises(ValueError):
            engine.create_order('unknown', 'B')
        first = engine.create_order('B', 'D')
        with self.assertRaises(RuntimeError):
            engine.create_order('A', 'C')
        self.assertEqual(engine.snapshot()['order_id'], first['order_id'])
        with self.assertRaises(ValueError):
            engine.tick(float('nan'))
        with self.assertRaises(ValueError):
            engine.tick(-1)
        with self.assertRaises(ValueError):
            engine.tick(10 ** 1000)

    def test_pickup_and_destination_at_current_node_completes(self):
        engine = OrderDemoEngine(example_map())
        state = engine.create_order('A', 'A')
        self.assertEqual(state['route'], ['A'])
        self.assertEqual(state['phase'], 'done')
        self.assertEqual(state['status'], 'completed')

    def test_disconnected_and_malformed_map_are_rejected(self):
        data = example_map()
        data['edges'].pop()
        engine = OrderDemoEngine(data)
        with self.assertRaises(ValueError):
            engine.create_order('B', 'D')
        self.assertEqual(engine.snapshot()['status'], 'idle')
        data = example_map()
        data['nodes'][0]['x'] = float('nan')
        with self.assertRaises(ValueError):
            OrderDemoEngine(data)
        data['nodes'][0]['x'] = 10 ** 1000
        with self.assertRaises(ValueError):
            OrderDemoEngine(data)


class ProductOrderEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = OrderDemoEngine(example_map())

    def acknowledge(self, state, applied=True):
        action = state['pending_action']
        return self.engine.apply_action_result(action['order_id'], action['action_id'],
                                               action['agv_id'], action['action'], applied)

    def test_moves_only_after_matching_grab_and_completes_only_after_matching_place(self):
        state = self.engine.create_product_order('Red', 'B', 'D', 'SHELF_RED', 0)
        self.assertEqual((state['status'], state['phase']), ('moving', 'to_pickup'))
        state = self.engine.tick(100)
        self.assertEqual((state['status'], state['phase'], state['current_node']),
                         ('waiting', 'waiting_grab', 'B'))
        action = state['pending_action']
        self.assertEqual((action['action'], action['product_type'], action['agv_id']),
                         ('grab', 'Red', 'AGV_01'))
        self.assertEqual(self.engine.tick(100)['pose'], state['pose'])
        self.assertFalse(self.engine.apply_action_result(action['order_id'], 'wrong',
                                                         action['agv_id'], action['action'], True))
        self.assertFalse(self.engine.apply_action_result(action['order_id'], action['action_id'],
                                                         action['agv_id'], action['action'], 1))
        self.assertTrue(self.acknowledge(state))
        self.assertFalse(self.acknowledge(state))
        state = self.engine.tick(100)
        self.assertEqual((state['status'], state['phase'], state['current_node']),
                         ('waiting', 'waiting_place', 'D'))
        self.assertNotEqual(action['action_id'], state['pending_action']['action_id'])
        self.assertEqual((state['pending_action']['action'], state['pending_action']['shelf_id'],
                          state['pending_action']['slot_index']), ('place', 'SHELF_RED', 0))
        self.assertTrue(self.acknowledge(state))
        self.assertEqual((self.engine.snapshot()['status'], self.engine.snapshot()['phase']),
                         ('completed', 'done'))
        self.assertIsNone(self.engine.snapshot()['pending_action'])

    def test_rejected_action_stops_and_unknown_blocks_replay(self):
        state = self.engine.create_product_order('Red', 'A', 'B', 'SHELF_RED', 0)
        self.assertEqual((state['status'], state['phase']), ('waiting', 'waiting_grab'))
        self.assertTrue(self.acknowledge(state, False))
        self.assertEqual(self.engine.snapshot()['status'], 'failed')
        self.assertFalse(self.acknowledge(state))
        with self.assertRaises(RuntimeError):
            self.engine.create_product_order('Red', 'A', 'B', 'SHELF_RED', 0)

        engine = OrderDemoEngine(example_map())
        state = engine.create_product_order('Red', 'A', 'B', 'SHELF_RED', 0)
        self.assertTrue(engine.action_unknown())
        self.assertEqual(engine.snapshot()['status'], 'unknown')
        action = state['pending_action']
        self.assertFalse(engine.apply_action_result(action['order_id'], action['action_id'],
                                                     action['agv_id'], action['action'], True))
        with self.assertRaises(RuntimeError):
            engine.create_order('A', 'B')

    def test_pickup_and_destination_at_current_node_still_require_both_actions(self):
        state = self.engine.create_product_order('Red', 'A', 'A', 'SHELF_RED', 0)
        self.assertEqual(state['phase'], 'waiting_grab')
        self.assertTrue(self.acknowledge(state))
        state = self.engine.snapshot()
        self.assertEqual(state['phase'], 'waiting_place')
        self.assertTrue(self.acknowledge(state))
        self.assertEqual(self.engine.snapshot()['status'], 'completed')

    def test_disconnect_during_product_motion_stops_without_replay(self):
        self.engine.create_product_order('Red', 'B', 'D', 'SHELF_RED', 0)
        before = self.engine.tick(0.1)
        self.assertTrue(self.engine.action_unknown())
        stopped = self.engine.tick(100)
        self.assertEqual(stopped['status'], 'unknown')
        self.assertEqual(stopped['pose'], before['pose'])
        self.assertIsNone(stopped['pending_action'])
        with self.assertRaises(RuntimeError):
            self.engine.create_order('A', 'B')
        normal = OrderDemoEngine(example_map())
        normal.create_order('B', 'D')
        self.assertFalse(normal.action_unknown())
        self.assertEqual(normal.snapshot()['status'], 'moving')

    def test_pending_action_copy_and_busy_invalid_order_do_not_replace_order(self):
        state = self.engine.create_product_order('Red', 'A', 'B', 'SHELF_RED', 0)
        state['pending_action']['action_id'] = 'tampered'
        self.assertNotEqual(self.engine.snapshot()['pending_action']['action_id'], 'tampered')
        with self.assertRaises(RuntimeError):
            self.engine.create_order('A', 'B')
        with self.assertRaises(RuntimeError):
            self.engine.create_product_order('Blue', 'A', 'B', 'SHELF_BLUE', 0)
        fresh = OrderDemoEngine(example_map())
        for product, shelf, slot in [('Unknown', 'SHELF_RED', 0), ('Red', '', 0),
                                     ('Red', 'SHELF_RED', True), ('Red', 'SHELF_RED', -1)]:
            with self.assertRaises(ValueError):
                fresh.create_product_order(product, 'A', 'B', shelf, slot)
        self.assertEqual(fresh.snapshot()['status'], 'idle')


if __name__ == '__main__':
    unittest.main()
