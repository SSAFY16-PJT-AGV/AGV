"""A one-client disconnect between virtual fleet control ticks."""
import unittest
from unittest.mock import patch

from server.order_fleet import FleetProductControlEngine
from server.order_product_control import ProductControlEngine
from server.tests.test_order_product_control import PRODUCTS, Robot, Runtime, Workflow, map_data


class FleetDisconnectRaceTests(unittest.TestCase):
    def test_second_robot_disconnect_after_first_tick_keeps_both_jobs_recoverable(self):
        Workflow.created = []
        runtime = Runtime()
        first_robot, second_robot = Robot(), Robot()
        with patch('server.order_product_control.PickupWorkflow', Workflow):
            first = ProductControlEngine(runtime, first_robot, map_data(), PRODUCTS)
            second = ProductControlEngine(runtime, second_robot, map_data(), PRODUCTS,
                                          robot_id='sim-r2', twin_id='AGV_02',
                                          session='demo-session-r2', task_prefix='r2-web')
            for engine in (first, second):
                engine.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)
            fleet = FleetProductControlEngine({'sim-r1': first, 'sim-r2': second}, map_data())
            first_tick = first.tick_recoverable
            def disconnect_second(dt):
                result = first_tick(dt)
                second_robot.ready = False
                return result
            first.tick_recoverable = disconnect_second
            fleet.tick(.1)
            self.assertEqual(second.snapshot()['status'], 'moving')
            self.assertEqual(Workflow.created[1].ticks, 0)
            self.assertTrue(fleet.suspend())
            self.assertEqual([first.snapshot()['status'], second.snapshot()['status']],
                             ['suspended', 'suspended'])

    def test_disconnect_between_fleet_and_robot_readiness_checks_pauses_clean_job(self):
        class RaceRobot(Robot):
            def __init__(self):
                super().__init__()
                self.armed = False
                self.reads = 0
            @property
            def ready(self):
                if not self.armed:
                    return True
                self.reads += 1
                # Fleet checks r2 twice; r2's own control check detects loss.
                return self.reads <= 2
            def recovery_checkpoint(self):
                return ('safe',)
        Workflow.created = []
        runtime = Runtime()
        first_robot, second_robot = Robot(), RaceRobot()
        with patch('server.order_product_control.PickupWorkflow', Workflow):
            first = ProductControlEngine(runtime, first_robot, map_data(), PRODUCTS)
            second = ProductControlEngine(runtime, second_robot, map_data(), PRODUCTS,
                                          robot_id='sim-r2', twin_id='AGV_02',
                                          session='demo-session-r2', task_prefix='r2-web')
            for engine in (first, second):
                engine.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)
            fleet = FleetProductControlEngine({'sim-r1': first, 'sim-r2': second}, map_data())
            second_robot.armed = True
            fleet.tick(.1)
            self.assertEqual(second.snapshot()['status'], 'suspended')
            self.assertEqual(Workflow.created[1].ticks, 0)

    def test_inflight_uncertain_publish_is_never_auto_paused(self):
        class UncertainRobot(Robot):
            def recovery_checkpoint(self):
                raise RuntimeError('uncertain event publish')
        Workflow.created = []
        runtime, robot = Runtime(), UncertainRobot()
        with patch('server.order_product_control.PickupWorkflow', Workflow):
            engine = ProductControlEngine(runtime, robot, map_data(), PRODUCTS)
            engine.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)
            robot.ready = False
            engine.tick_recoverable(.1)
            self.assertEqual(engine.snapshot()['status'], 'unknown')
            self.assertEqual(Workflow.created[0].ticks, 0)


if __name__ == '__main__':
    unittest.main()
