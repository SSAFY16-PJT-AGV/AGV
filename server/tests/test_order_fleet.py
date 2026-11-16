"""Fleet dispatch and action correlation for the isolated order demo."""
import copy
import threading
import unittest

from server.order_demo_actions import DemoActionSession
from server.order_fleet import FleetProductControlEngine
from server.tests.test_order_product_control import map_data


class FakeEngine:
    control_ready = True

    def __init__(self, node, twin):
        self.twin = twin
        self.state = {'status': 'idle', 'phase': None, 'current_node': node,
                      'pose': {'x': 0, 'y': 0, 'th': 0}, 'pending_action': None,
                      'orders_remaining': 16, 'product_type': None, 'route': [],
                      'battery': .8}

    def snapshot(self):
        return copy.deepcopy(self.state)

    def create_product_order(self, product_type, pickup, destination, shelf, slot):
        self.state.update(status='moving', phase='to_pickup', product_type=product_type)
        return self.snapshot()

    def tick(self, dt):
        return self.snapshot()

    def action_unknown(self):
        self.state['status'] = 'unknown'

    def apply_action_result(self, order_id, action_id, agv_id, action, applied):
        pending = self.state['pending_action']
        if not pending or pending['action_id'] != action_id or agv_id != self.twin:
            return False
        self.state['pending_action'] = None
        return True


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.one, self.two = FakeEngine('A', 'AGV_01'), FakeEngine('D', 'AGV_02')
        self.fleet = FleetProductControlEngine({'sim-r1': self.one, 'sim-r2': self.two}, map_data())

    def test_two_orders_select_available_nearest_robot(self):
        first = self.fleet.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)
        second = self.fleet.create_product_order('Blue', 'A', 'D', 'SHELF_BLUE', 0)
        self.assertEqual(first['assigned_robot'], 'sim-r1')
        self.assertEqual(second['assigned_robot'], 'sim-r2')
        self.assertFalse(self.fleet.snapshot()['accepting_orders'])
        with self.assertRaises(RuntimeError):
            self.fleet.create_product_order('Green', 'P', 'D', 'SHELF_GREEN', 0)

    def test_action_selection_stays_stable_until_correlated_ack(self):
        action2 = {'order_id': 'two', 'action_id': 'a2', 'agv_id': 'AGV_02', 'action': 'grab'}
        action1 = {'order_id': 'one', 'action_id': 'a1', 'agv_id': 'AGV_01', 'action': 'grab'}
        self.two.state['pending_action'] = action2
        self.assertEqual(self.fleet.snapshot()['pending_action'], action2)
        self.one.state['pending_action'] = action1
        self.assertEqual(self.fleet.snapshot()['pending_action'], action2)
        self.assertFalse(self.fleet.apply_action_result('one', 'a1', 'AGV_01', 'grab', True))
        self.assertTrue(self.fleet.apply_action_result('two', 'a2', 'AGV_02', 'grab', True))
        self.assertEqual(self.fleet.snapshot()['pending_action'], action1)

    def test_action_session_preserves_each_robot_pose_and_rejects_other_robot_ack(self):
        self.one.state['pose'] = {'x': 1000, 'y': 2000, 'th': 0}
        self.two.state['pose'] = {'x': 9000, 'y': 8000, 'th': 0}
        action1 = {'order_id': 'one', 'action_id': 'a1', 'agv_id': 'AGV_01', 'action': 'grab'}
        action2 = {'order_id': 'two', 'action_id': 'a2', 'agv_id': 'AGV_02', 'action': 'place'}
        self.one.state['pending_action'] = action1
        self.two.state['pending_action'] = action2
        session = DemoActionSession(self.fleet, threading.RLock(), settle_seconds=1, timeout_seconds=10)
        owner = object()
        self.assertTrue(session.attach(owner, True))
        self.assertIsNone(session.poll(owner, 1))
        first = session.poll(owner, 2)
        self.assertEqual((first['data']['agv_id'], first['data']['expected_x'], first['data']['expected_y']),
                         ('AGV_01', 1, 2))
        self.assertEqual(first['data']['action_id'], 'a1')
        result2 = {'type': 'robot.action.result', 'data': dict(action2, applied=True)}
        self.assertFalse(session.receive(owner, result2, now=2))
        self.assertEqual(self.two.state['pending_action'], action2)
        self.assertTrue(session.receive(owner, {'type': 'robot.action.result',
                                                'data': dict(action1, applied=True)}, now=2))
        self.assertIsNone(session.poll(owner, 3))
        second = session.poll(owner, 4)
        self.assertEqual((second['data']['agv_id'], second['data']['expected_x'], second['data']['expected_y']),
                         ('AGV_02', 9, 8))
        self.assertEqual(second['data']['action_id'], 'a2')
        self.assertTrue(session.receive(owner, result2, now=4))

    def test_one_shelf_slot_cannot_be_ordered_twice_in_one_world(self):
        self.fleet.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)
        with self.assertRaises(RuntimeError):
            self.fleet.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)


if __name__ == '__main__':
    unittest.main()
