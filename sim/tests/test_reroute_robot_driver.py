"""The ordinary virtual MQTT robot never advances reroutes without a driver guard."""
from contextlib import nullcontext
import json
import threading
import unittest
from unittest.mock import PropertyMock, patch

import paho.mqtt.client as mqtt

import sim.tests.test_reroute_motion as fixture
from sim.mqtt_order_robot import SimMqttOrderRobot


class RerouteRobotDriverTest(unittest.TestCase):
    def setUp(self):
        self.base = fixture.RerouteMotionTest(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.execution, self.executor = self.base.execution, self.base.executor
        self.payload, self.guard = self.base.payload, self.base.guard
        self.robot = SimMqttOrderRobot(self.executor, 1883, authorization_guard=lambda _payload: nullcontext())
        self.guard.robot_receiver.robot = self.robot
        self.ready = patch.object(SimMqttOrderRobot, 'ready', new_callable=PropertyMock, return_value=True)
        self.ready.start()
        self.addCleanup(self.ready.stop)
        self.addCleanup(self.robot.stop)

    def test_driver_moves_only_inside_guard_and_stops_after_guard_loss(self):
        self.assertTrue(self.robot.start_reroute(self.execution, self.payload, guard=self.guard)['new'])
        before = self.executor.snapshot()['pose']
        moved = self.robot.tick(.01)
        self.assertNotEqual(moved['pose'], before)
        self.guard.allow = False
        stopped = self.executor.snapshot()['pose']
        with self.assertRaises(RuntimeError):
            self.robot.tick(.01)
        self.assertEqual(self.executor.snapshot()['pose'], stopped)
        self.assertTrue(self.robot._failed.is_set())

    def test_driver_cannot_be_rebound_or_started_with_wrong_executor(self):
        self.assertTrue(self.robot.start_reroute(self.execution, self.payload, guard=self.guard)['new'])
        before = self.executor.snapshot()['pose']
        self.assertFalse(self.robot.start_reroute(self.execution, self.payload, guard=self.guard)['new'])
        self.assertEqual(self.executor.snapshot()['pose'], before)
        other = fixture.FakeServerGuard(self.execution)
        other.robot_receiver.robot = self.robot
        with self.assertRaises((ValueError, RuntimeError)):
            self.robot.start_reroute(self.execution, self.payload, guard=other)
        self.assertEqual(self.executor.snapshot()['pose'], before)

    def test_stopped_generation_is_passive_without_publishing_or_restarting(self):
        self.robot.start_reroute(self.execution, self.payload, guard=self.guard)
        done = self.robot.tick(10)
        self.assertEqual((done['status'], done['current_node']), ('stopped', 'D1'))
        self.guard.allow = False
        self.assertEqual(self.robot.tick(1)['pose'], done['pose'])

    def test_duplicate_without_installed_driver_cannot_resume(self):
        self.robot.start_reroute(self.execution, self.payload, guard=self.guard)
        before = self.executor.snapshot()['pose']
        self.robot._reroute_driver = None
        with self.assertRaises((ValueError, RuntimeError)):
            self.robot.start_reroute(self.execution, self.payload, guard=self.guard)
        self.assertEqual(self.executor.snapshot()['pose'], before)
        self.assertTrue(self.robot._failed.is_set())

    def test_lifecycle_worker_stop_and_tick_do_not_invert_robot_lock(self):
        self.robot.start_reroute(self.execution, self.payload, guard=self.guard)
        outer = threading.RLock()
        self.guard.outer_lock = outer
        worker_has_outer, tick_has_robot, worker_attempting = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def proposal_worker():
            try:
                with outer:
                    worker_has_outer.set()
                    if not tick_has_robot.wait(5):
                        raise RuntimeError('tick did not take its snapshot')
                    worker_attempting.set()
                    with self.robot._lock:
                        pass
            except Exception as error:
                errors.append(error)
        def stop_receiver():
            try:
                with outer:
                    pass
            except Exception as error:
                errors.append(error)
        original = self.executor.snapshot
        def guarded_snapshot():
            if threading.current_thread().name == 'generation-tick-test':
                tick_has_robot.set()
                if not worker_attempting.wait(5):
                    raise RuntimeError('worker did not attempt robot lock')
            return original()
        def advance():
            try:
                self.robot.tick(.01)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=proposal_worker, daemon=True)
        stopper = threading.Thread(target=stop_receiver, daemon=True)
        ticker = threading.Thread(target=advance, name='generation-tick-test', daemon=True)
        with patch.object(self.executor, 'snapshot', side_effect=guarded_snapshot):
            worker.start()
            self.assertTrue(worker_has_outer.wait(5))
            stopper.start()
            ticker.start()
            for thread in (worker, stopper, ticker):
                thread.join(timeout=8)
        self.assertFalse(any(thread.is_alive() for thread in (worker, stopper, ticker)),
                         'lifecycle, worker, and generation tick deadlocked')
        self.assertEqual(errors, [])

    def test_lifecycle_worker_and_start_do_not_invert_robot_lock(self):
        outer = threading.RLock()
        self.guard.outer_lock = outer
        worker_has_outer, begin = threading.Event(), threading.Event()
        errors = []
        def lifecycle_worker():
            try:
                with outer:
                    worker_has_outer.set()
                    if not begin.wait(5):
                        raise RuntimeError('start barrier timed out')
                    with self.robot._lock:
                        pass
            except Exception as error:
                errors.append(error)
        def start():
            try:
                begin.set()
                self.robot.start_reroute(self.execution, self.payload, guard=self.guard)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=lifecycle_worker, daemon=True)
        starter = threading.Thread(target=start, daemon=True)
        worker.start()
        self.assertTrue(worker_has_outer.wait(5))
        starter.start()
        worker.join(timeout=8)
        starter.join(timeout=8)
        self.assertFalse(worker.is_alive() or starter.is_alive(), 'generation start inverted the lock order')
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
