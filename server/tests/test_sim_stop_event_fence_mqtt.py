"""Stop fences new events but drains an already admitted transaction."""
from contextlib import contextmanager
import json
import os
from queue import Empty
import sqlite3
from contextlib import closing
import threading
import unittest
from unittest.mock import patch

from server.recovery import _digest_json
from server.segments import plan_segment
from server.sim_runtime import SimRuntime
from server.tests import test_sim_runtime_command_observations_mqtt as fixture
from server.tests.test_segment_recovery_mqtt import two_segment_map


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class StopEventFenceMqttTests(unittest.TestCase):
    setUp = fixture.RuntimeCommandObservationMqttTests.setUp
    peer = fixture.RuntimeCommandObservationMqttTests.peer
    wait_for = fixture.RuntimeCommandObservationMqttTests.wait_for

    def setup_order(self, *, reconnect=False):
        robot, orders = self.peer()
        data = two_segment_map()
        runtime = SimRuntime(self.path, data, {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='stop-event-fence')
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        runtime.prepare('T1', 'N5', [{'robot_id': 'sim-r1', 'node': 'N1', 'available': True}],
                        {'type': 'load'})
        runtime.publish('T1')
        order = json.loads(orders.get(timeout=5))
        if reconnect:
            runtime._client.disconnect()
            self.wait_for(lambda: runtime.receiver._closed, 'initial disconnect')
            runtime.reconnect()
        challenge = runtime.begin('T1', 'boot-1')['challenge']
        runtime.complete('T1', 'boot-1', challenge, 'sim-r1', 'T1', 1,
                         'integration@v1', 'N3', _digest_json(order), True)
        before = set(runtime.store.list_occupancy())
        old = set(plan_segment(data, ['N1', 'N2', 'N3', 'N4', 'N5']).resources)
        new = set(plan_segment(data, ['N3', 'N4', 'N5']).resources)
        event = {'type': 'rear_clear', 'event_id': 'stop-boundary', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'integration@v1', 'stopped_at': 'N3',
                 'cleared_resources': [list(item) for item in sorted(old - new)]}
        return runtime, robot, orders, event, before, new

    def exercise_new_event_after_stop_fence(self, *, reconnect):
        runtime, robot, orders, event, before, _new = self.setup_order(reconnect=reconnect)
        entered, release, delivered, stopped = (threading.Event() for _ in range(4))
        errors = []
        original_stop, original_message = runtime.workflow.stop, runtime._client.on_message
        original_ack, ack_calls = runtime._client.ack, []
        def delayed_stop():
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release workflow stop')
            return original_stop()
        def observed_message(*args):
            try:
                return original_message(*args)
            finally:
                delivered.set()
        def stop_runtime():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                stopped.set()
        client = runtime._client
        client.on_message = observed_message
        def observed_ack(*args, **kwargs):
            ack_calls.append(args)
            return original_ack(*args, **kwargs)
        client.ack = observed_ack
        try:
            with patch.object(runtime.workflow, 'stop', side_effect=delayed_stop):
                stopper = threading.Thread(target=stop_runtime)
                stopper.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertTrue(runtime._stopped)
                    robot.publish('agv/sim-r1/event', json.dumps(event), qos=1).wait_for_publish(timeout=5)
                    self.assertTrue(delivered.wait(5))
                    self.assertEqual(ack_calls, [])
                    self.assertEqual(set(runtime.store.list_occupancy()), before)
                    with closing(sqlite3.connect(self.path)) as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM sim_event_inbox WHERE event_id='stop-boundary'").fetchone()[0], 0)
                    self.assertFalse(stopped.is_set())
                finally:
                    release.set()
                    stopper.join(10)
        finally:
            client.on_message = original_message
            client.ack = original_ack
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertIn('T1', runtime.gate._blocked)
        with self.assertRaises(Empty):
            orders.get(timeout=.3)

    def test_new_event_after_initial_stop_fence_does_not_change_occupancy(self):
        self.exercise_new_event_after_stop_fence(reconnect=False)

    def test_new_event_after_reconnected_stop_fence_does_not_change_occupancy(self):
        self.exercise_new_event_after_stop_fence(reconnect=True)

    def test_transaction_admitted_before_stop_finishes_before_owner_release(self):
        runtime, robot, orders, event, before, new = self.setup_order()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original_transaction = runtime.progress._transaction
        @contextmanager
        def delayed_transaction():
            with original_transaction() as db:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test did not release admitted transaction')
                yield db
        def stop_runtime():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                stopped.set()
        with patch.object(runtime.progress, '_transaction', side_effect=delayed_transaction):
            robot.publish('agv/sim-r1/event', json.dumps(event), qos=1).wait_for_publish(timeout=5)
            self.assertTrue(entered.wait(5))
            stopper = threading.Thread(target=stop_runtime)
            stopper.start()
            try:
                self.wait_for(lambda: runtime._stopped, 'stop after event admission')
                self.assertFalse(stopped.is_set())
                self.assertIsNotNone(runtime._owner.file)
            finally:
                release.set()
                stopper.join(10)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertNotEqual(before, new)
        self.assertEqual(set(runtime.store.list_occupancy()), new)
        self.assertTrue(stopped.is_set())
        self.assertIsNone(runtime._owner.file)
        self.assertIn('T1', runtime.gate._blocked)
        with self.assertRaises(Empty):
            orders.get(timeout=.3)

    def test_gate_invalidation_failure_keeps_owner_after_transport_cleanup(self):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='stop-gate-failure')
        runtime.start()
        try:
            with patch.object(runtime.gate, 'invalidate_many', side_effect=RuntimeError('injected')):
                with self.assertRaises(RuntimeError):
                    runtime.stop()
            self.assertTrue(runtime._stopped)
            self.assertIsNotNone(runtime._owner.file)
            self.assertFalse(runtime._client.is_connected())
        finally:
            runtime._owner.release()
