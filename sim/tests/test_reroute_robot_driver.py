"""The ordinary virtual MQTT robot never advances reroutes without a driver guard."""
from contextlib import nullcontext, contextmanager
import json
import threading
import unittest
from unittest.mock import PropertyMock, patch

import paho.mqtt.client as mqtt

import sim.tests.test_reroute_motion as fixture
from sim.mqtt_order_robot import SimMqttOrderRobot
import sim.tests.test_reroute_execution as generation_fixture
from contextlib import closing
import sqlite3


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

    def _multi_segment_robot(self):
        data = generation_fixture.fixture.fixture.cycle_map()
        data['zones'].append({'id': 'wait-X', 'type': 'waiting', 'node': 'X'})
        other = generation_fixture.RerouteExecutionTest(methodName='runTest')
        with patch.object(generation_fixture.fixture.fixture, 'cycle_map', return_value=data):
            other.setUp()
        self.addCleanup(other.doCleanups)
        other.base.send_commit(other.base.commit)
        other.send(other.request)
        execution, executor = other.execution, other.base.executor
        payload = generation_fixture.fixture.canonical(other.request)
        guard = fixture.FakeServerGuard(execution)
        robot = SimMqttOrderRobot(executor, 1883, authorization_guard=lambda _payload: nullcontext())
        guard.robot_receiver.robot = robot
        self.addCleanup(robot.stop)
        robot.start_reroute(execution, payload, guard=guard)
        first = robot.tick(10)
        self.assertEqual((first['status'], first['current_node']), ('stopped', 'X'))
        order = dict(first['order'], seq=3, released_until='D1')
        return robot, execution, guard, generation_fixture.fixture.canonical(order)

    def test_mqtt_next_generation_segment_uses_server_guard_and_advances(self):
        robot, execution, guard, order_payload = self._multi_segment_robot()
        robot._queue.put((order_payload, 31, 1))
        @contextmanager
        def checked_next(_guard, _payload, _order):
            self.assertEqual(robot._reroute_next_owner, threading.get_ident())
            self.assertIs(getattr(execution._accept_context, 'active', False), True)
            yield
        with patch.object(fixture.FakeServerGuard, 'guard_next', checked_next), \
                patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS) as ack:
            state = robot.tick(10)
        self.assertEqual((state['status'], state['current_node'], state['order']['seq']), ('stopped', 'D1', 3))
        ack.assert_called_once_with(31, 1)
        self.assertIsNone(robot._reroute_next_owner)
        self.assertIsNot(getattr(execution._accept_context, 'active', False), True)
        with closing(sqlite3.connect(execution.inbox.path)) as db:
            self.assertEqual(db.execute("SELECT seq,state FROM sim_inbox_orders WHERE order_id='T1' ORDER BY seq")
                             .fetchall(), [(1, 'completed'), (2, 'completed'), (3, 'completed')])

    def test_denied_next_generation_segment_does_not_move_or_ack(self):
        robot, execution, guard, order_payload = self._multi_segment_robot()
        before = robot.executor.snapshot()
        guard.allow = False
        robot._queue.put((order_payload, 32, 1))
        with patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS) as ack:
            with self.assertRaises(RuntimeError):
                robot.tick(10)
        self.assertEqual(robot.executor.snapshot()['pose'], before['pose'])
        ack.assert_not_called()
        self.assertIsNone(robot._reroute_next_owner)
        self.assertIsNot(getattr(execution._accept_context, 'active', False), True)

    def test_ack_failure_after_next_acceptance_preserves_pose_and_blocks_retry(self):
        robot, execution, _guard, order_payload = self._multi_segment_robot()
        before = robot.executor.snapshot()['pose']
        robot._queue.put((order_payload, 33, 1))
        with patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_NO_CONN):
            with self.assertRaises(RuntimeError):
                robot.tick(10)
        self.assertEqual(robot.executor.snapshot()['pose'], before)
        self.assertTrue(robot._failed.is_set())
        with closing(sqlite3.connect(execution.inbox.path)) as db:
            self.assertEqual(db.execute("SELECT state FROM sim_inbox_orders WHERE order_id='T1' AND seq=3")
                             .fetchone(), ('accepted',))
        with patch.object(SimMqttOrderRobot, 'ready', new_callable=PropertyMock, return_value=False):
            with self.assertRaises(RuntimeError):
                robot.tick(10)
        self.assertEqual(robot.executor.snapshot()['pose'], before)

    def test_duplicate_current_segment_acknowledges_without_restarting_route(self):
        robot, _execution, _guard, order_payload = self._multi_segment_robot()
        robot._queue.put((order_payload, 34, 1))
        with patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS) as ack:
            first = robot.tick(.01)
            self.assertEqual(first['status'], 'moving')
            robot._queue.put((order_payload, 35, 1))
            state = robot.tick(.01)
        self.assertEqual(state['status'], 'moving')
        self.assertEqual(state['order']['seq'], 3)
        self.assertEqual(state['pose'], first['pose'])
        self.assertEqual(ack.call_count, 2)

    def test_duplicate_before_next_packet_preserves_queue_and_does_not_tick(self):
        robot, execution, _guard, next_payload = self._multi_segment_robot()
        before = robot.executor.snapshot()
        duplicate = json.dumps(before['order'], separators=(',', ':')).encode('utf-8')
        robot._queue.put((duplicate, 41, 1))
        robot._queue.put((next_payload, 42, 1))
        @contextmanager
        def guard_checked(guard, _payload):
            actual = guard.robot_receiver.robot
            if not actual._queue.empty() and not actual._reroute_next_inflight:
                raise ValueError('unprocessed packet blocks a new movement tick')
            yield
        @contextmanager
        def duplicate_checked(guard, _payload, _order):
            actual = guard.robot_receiver.robot
            self.assertEqual(getattr(actual, '_reroute_next_owner', None), threading.get_ident())
            self.assertIs(getattr(getattr(execution, '_accept_context', None), 'active', None), True)
            yield
        with patch.object(fixture.FakeServerGuard, 'guard', guard_checked), \
                patch.object(fixture.FakeServerGuard, 'guard_duplicate', duplicate_checked), \
                patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS) as ack:
            after_duplicate = robot.tick(10)
            self.assertEqual(after_duplicate['pose'], before['pose'])
            self.assertEqual(after_duplicate['order']['seq'], 2)
            self.assertEqual(robot._queue.qsize(), 1)
            self.assertEqual(ack.call_count, 1)
            finished = robot.tick(10)
        self.assertEqual((finished['status'], finished['current_node'], finished['order']['seq']), ('stopped', 'D1', 3))
        self.assertEqual(ack.call_count, 2)
        self.assertIsNone(robot._reroute_next_owner)
        self.assertIsNot(getattr(execution._accept_context, 'active', False), True)

    def test_direct_other_thread_tick_cannot_use_duplicate_acceptance_context(self):
        robot, execution, _guard, order_payload = self._multi_segment_robot()
        robot._queue.put((order_payload, 43, 1))
        with patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS):
            moving = robot.tick(.01)
        driver = robot._reroute_driver
        robot._queue.put((order_payload, 44, 1))
        robot._queue.put((order_payload, 45, 1))
        entered, release, errors = threading.Event(), threading.Event(), []
        @contextmanager
        def guarded_tick(guard, _payload):
            if not guard.robot_receiver.robot._queue.empty():
                raise ValueError('queued packet blocks direct movement')
            yield
        @contextmanager
        def guarded_duplicate(guard, _payload, _order):
            current = guard.robot_receiver.robot
            self.assertEqual(getattr(current, '_reroute_next_owner', None), threading.get_ident())
            self.assertIs(getattr(getattr(execution, '_accept_context', None), 'active', None), True)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test barrier timed out')
            yield
        def process_duplicate():
            try:
                robot.tick(.1)
            except Exception as exc:
                errors.append(exc)
        with patch.object(fixture.FakeServerGuard, 'guard', guarded_tick), \
                patch.object(fixture.FakeServerGuard, 'guard_duplicate', guarded_duplicate), \
                patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS):
            worker = threading.Thread(target=process_duplicate)
            worker.start()
            try:
                self.assertTrue(entered.wait(5), 'duplicate did not enter its dedicated guard')
                with self.assertRaises(ValueError):
                    execution.tick(driver[1], .1, guard=driver[2])
                self.assertEqual(robot.executor.snapshot()['pose'], moving['pose'])
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_new_segment_before_duplicate_waits_for_separate_packet_validation(self):
        robot, _execution, _guard, next_payload = self._multi_segment_robot()
        before = robot.executor.snapshot()
        robot._queue.put((next_payload, 46, 1))
        robot._queue.put((next_payload, 47, 1))
        @contextmanager
        def guarded_tick(guard, _payload):
            if not guard.robot_receiver.robot._queue.empty():
                raise ValueError('queued packet blocks movement')
            yield
        with patch.object(fixture.FakeServerGuard, 'guard', guarded_tick), \
                patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS) as ack:
            accepted = robot.tick(10)
            self.assertEqual(accepted['status'], 'moving')
            self.assertEqual(accepted['pose'], before['pose'])
            self.assertEqual(robot._queue.qsize(), 1)
            self.assertEqual(ack.call_count, 1)
            duplicate = robot.tick(10)
            self.assertEqual(duplicate['pose'], before['pose'])
            self.assertEqual(ack.call_count, 2)
            finished = robot.tick(10)
        self.assertEqual((finished['status'], finished['current_node']), ('stopped', 'D1'))

    def test_changed_duplicate_is_rejected_before_motion_or_ack(self):
        robot, _execution, _guard, order_payload = self._multi_segment_robot()
        robot._queue.put((order_payload, 36, 1))
        with patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS):
            first = robot.tick(.01)
        altered = json.loads(order_payload)
        altered['action_at_end'] = {'type': 'load'}
        robot._queue.put((json.dumps(altered).encode('utf-8'), 37, 1))
        with patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS) as ack:
            with self.assertRaises(RuntimeError):
                robot.tick(.01)
        self.assertEqual(robot.executor.snapshot()['pose'], first['pose'])
        ack.assert_not_called()

    def test_driver_loss_during_next_authorization_prevents_acceptance(self):
        robot, execution, guard, order_payload = self._multi_segment_robot()
        before = robot.executor.snapshot()['pose']
        entered, release, errors = threading.Event(), threading.Event(), []
        @contextmanager
        def delayed(_guard, _payload, _order):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test barrier timed out')
            yield
        def advance():
            try:
                robot.tick(1)
            except Exception as exc:
                errors.append(exc)
        robot._queue.put((order_payload, 38, 1))
        with patch.object(fixture.FakeServerGuard, 'guard_next', delayed), \
                patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS) as ack:
            worker = threading.Thread(target=advance)
            worker.start()
            self.assertTrue(entered.wait(5))
            with robot._lock:
                robot._reroute_driver = None
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            ack.assert_not_called()
        self.assertEqual(robot.executor.snapshot()['pose'], before)
        with closing(sqlite3.connect(execution.inbox.path)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sim_inbox_orders WHERE order_id='T1' AND seq=3").fetchone())

    def test_concurrent_tick_during_next_authorization_does_not_advance(self):
        robot, _execution, _guard, order_payload = self._multi_segment_robot()
        before = robot.executor.snapshot()['pose']
        checkpoint = robot.connected_recovery_checkpoint()
        entered, release, errors = threading.Event(), threading.Event(), []
        @contextmanager
        def delayed(_guard, _payload, _order):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test barrier timed out')
            yield
        def advance():
            try:
                robot.tick(.01)
            except Exception as exc:
                errors.append(exc)
        robot._queue.put((order_payload, 39, 1))
        with patch.object(fixture.FakeServerGuard, 'guard_next', delayed), \
                patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_SUCCESS):
            worker = threading.Thread(target=advance)
            worker.start()
            self.assertTrue(entered.wait(5))
            with self.assertRaises(RuntimeError):
                robot.tick(.01)
            self.assertEqual(robot.executor.snapshot()['pose'], before)
            with self.assertRaises(RuntimeError):
                robot.connected_recovery_checkpoint()
            self.assertFalse(robot.check_recovery_checkpoint(checkpoint))
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_concurrent_tick_cannot_move_before_ack_failure_is_latched(self):
        robot, _execution, _guard, order_payload = self._multi_segment_robot()
        before = robot.executor.snapshot()['pose']
        entered, release, second_done, errors = threading.Event(), threading.Event(), threading.Event(), []
        original_fail = robot._fail
        def paused_fail():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test failure barrier timed out')
            original_fail()
        def tick(done=None):
            try:
                robot.tick(.1)
            except Exception as exc:
                errors.append(exc)
            finally:
                if done is not None:
                    done.set()
        robot._queue.put((order_payload, 40, 1))
        with patch.object(SimMqttOrderRobot, 'ready', new_callable=PropertyMock,
                          side_effect=lambda: not robot._failed.is_set()), \
                patch.object(robot, '_fail', side_effect=paused_fail), \
                patch.object(robot._client, 'ack', return_value=mqtt.MQTT_ERR_NO_CONN):
            first = threading.Thread(target=tick)
            second = threading.Thread(target=tick, args=(second_done,))
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            self.assertFalse(second_done.wait(.2))
            release.set()
            first.join(5); second.join(5)
            self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(errors), 2)
        self.assertEqual(robot.executor.snapshot()['pose'], before)


if __name__ == '__main__':
    unittest.main()
