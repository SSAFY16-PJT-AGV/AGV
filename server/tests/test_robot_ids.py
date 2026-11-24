"""Robot ID classes for the order/reservation path and the sim-only modules."""
import unittest

from server.robot_ids import is_fleet_robot, is_physical_robot, is_sim_robot


class RobotIdTests(unittest.TestCase):
    def test_classes(self):
        self.assertTrue(is_sim_robot('sim-r1') and is_fleet_robot('sim-r1'))
        self.assertTrue(is_physical_robot('physical-1') and is_fleet_robot('physical-1'))
        for bad in ('sim-', 'physical-', 'physical- ', 'jetank-1', 'agv1', '', None, 1):
            with self.subTest(bad=bad):
                self.assertFalse(is_fleet_robot(bad))
        self.assertFalse(is_sim_robot('physical-1'))
        self.assertFalse(is_physical_robot('sim-r1'))

    def test_order_topic_accepts_physical_robots(self):
        from server.order_publisher import _topic
        self.assertEqual(_topic('physical-1'), 'agv/physical-1/order')
        for bad in ('jetank-1', 'physical-1/x', 'physical-#'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _topic(bad)

    def test_synthetic_action_modules_stay_sim_only(self):
        from server.order_product_control import ProductControlEngine
        with self.assertRaisesRegex(ValueError, 'simulated'):
            ProductControlEngine(None, None, {}, {'p': 'x'}, robot_id='physical-1')


if __name__ == '__main__':
    unittest.main()
