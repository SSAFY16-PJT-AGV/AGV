import json
import os
import queue
import threading
import time
import unittest

from server.tests import test_fleet_mqtt_integration as fixture
from server.tests.test_segment_recovery_mqtt import two_segment_map


@unittest.skipUnless(os.environ.get('MOSQUITTO_EXE'), 'set MOSQUITTO_EXE for isolated broker')
class SimRuntimeTest(unittest.TestCase):
    setUp = fixture.FleetMqttIntegrationTest.setUp
    new_client = fixture.FleetMqttIntegrationTest.new_client
    connect = fixture.FleetMqttIntegrationTest.connect

    def runtime(self, **kwargs):
        from server.sim_runtime import SimRuntime
        return SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                          port=self.port, client_id='runtime-test', **kwargs)

    def test_runtime_first_order_and_single_database_owner(self):
        received, subscribed = queue.Queue(), threading.Event()
        robot = self.new_client()
        robot.on_message = lambda c, u, message: received.put(message.payload)
        robot.on_subscribe = lambda *args: subscribed.set()
        self.connect(robot, lambda c: c.subscribe('agv/sim-r1/order', qos=1))
        self.assertTrue(subscribed.wait(3))
        runtime = self.runtime()
        runtime.start()
        self.addCleanup(runtime.stop)
        self.assertTrue(runtime.status()['ready'])
        second = self.runtime()
        with self.assertRaises(RuntimeError):
            second.start()
        runtime.create_task('T1')
        with self.assertRaises(ValueError):
            runtime.create_task('T1')
        result = runtime.prepare('T1', 'N5', [
            {'robot_id': 'sim-r1', 'node': 'N1', 'available': True}], {'type': 'load'})
        self.assertEqual(result['released_until'], 'N3')
        self.assertEqual(runtime.publish('T1')['state'], 'acked')
        self.assertTrue(received.get(timeout=3))
        with self.assertRaises(Exception):
            runtime.publish('T1')
        runtime.stop()
        self.assertFalse(runtime.status()['ready'])
        second.start()
        self.addCleanup(second.stop)
        self.assertTrue(second.status()['ready'])

    def test_unmapped_robot_cannot_allocate_or_reconcile(self):
        runtime = self.runtime()
        runtime.start()
        self.addCleanup(runtime.stop)
        runtime.create_task('T1')
        with self.assertRaises(ValueError):
            runtime.prepare('T1', 'N5', [{'robot_id': 'sim-r2', 'node': 'N1', 'available': True}],
                            {'type': 'load'})
        with self.assertRaises(ValueError):
            runtime.begin('T1', 'wrong-session')
        self.assertIsNone(runtime.store.get_allocation('T1'))

    def test_loopback_host_only(self):
        with self.assertRaises(ValueError):
            self.runtime(host='example.com')

    def test_offline_after_prepare_prevents_initial_publication(self):
        runtime = self.runtime()
        runtime.start()
        self.addCleanup(runtime.stop)
        runtime.create_task('T1')
        runtime.prepare('T1', 'N5', [{'robot_id': 'sim-r1', 'node': 'N1', 'available': True}],
                        {'type': 'load'})
        sender = self.new_client()
        self.connect(sender, None)
        info = sender.publish('agv/sim-r1/conn', b'offline', qos=1)
        info.wait_for_publish(timeout=3)
        deadline = time.monotonic() + 3
        while 'T1' not in runtime.gate._blocked and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn('T1', runtime.gate._blocked)
        with self.assertRaises(Exception):
            runtime.publish('T1')
        self.assertEqual(runtime.store.get_order('T1')['state'], 'pending')

    def test_recovery_and_clearance_send_next_order(self):
        from server.recovery import _digest_json
        from server.segments import plan_segment
        received, subscribed = queue.Queue(), threading.Event()
        robot = self.new_client()
        robot.on_message = lambda c, u, message: received.put(json.loads(message.payload))
        robot.on_subscribe = lambda *args: subscribed.set()
        self.connect(robot, lambda c: c.subscribe('agv/sim-r1/order', qos=1))
        self.assertTrue(subscribed.wait(3))
        runtime = self.runtime(); runtime.start(); self.addCleanup(runtime.stop)
        runtime.create_task('T1')
        runtime.prepare('T1', 'N5', [{'robot_id': 'sim-r1', 'node': 'N1', 'available': True}], {'type': 'load'})
        runtime.publish('T1')
        order = received.get(timeout=3)
        runtime.stop()
        restarted = self.runtime(); restarted.start(); self.addCleanup(restarted.stop)
        challenge = restarted.begin('T1', 'boot-1')['challenge']
        self.assertTrue(restarted.complete('T1', 'boot-1', challenge, 'sim-r1', 'T1', 1,
                         'integration@v1', 'N3', _digest_json(order), True)['reconciled'])
        data = two_segment_map()
        old = set(plan_segment(data, ['N1', 'N2', 'N3', 'N4', 'N5']).resources)
        new = set(plan_segment(data, ['N3', 'N4', 'N5']).resources)
        event = {'type': 'rear_clear', 'event_id': 'clear-1', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'integration@v1', 'stopped_at': 'N3',
                 'cleared_resources': [list(item) for item in sorted(old - new)]}
        info = robot.publish('agv/sim-r1/event', json.dumps(event), qos=1)
        info.wait_for_publish(timeout=3)
        next_order = received.get(timeout=5)
        self.assertEqual((next_order['seq'], next_order['released_until']), (2, 'N5'))
        self.assertEqual(set(restarted.store.list_occupancy()), new)
        robot.publish('agv/sim-r1/event', json.dumps(event), qos=1).wait_for_publish(timeout=3)
        with self.assertRaises(queue.Empty):
            received.get(timeout=0.3)

    def test_session_change_revokes_existing_recovery_proof(self):
        from server.recovery import _digest_json
        from server.segments import plan_segment
        received, subscribed = queue.Queue(), threading.Event()
        robot = self.new_client()
        robot.on_message = lambda c, u, message: received.put(json.loads(message.payload))
        robot.on_subscribe = lambda *args: subscribed.set()
        self.connect(robot, lambda c: c.subscribe('agv/sim-r1/order', qos=1))
        self.assertTrue(subscribed.wait(3))
        runtime = self.runtime(); runtime.start(); self.addCleanup(runtime.stop)
        runtime.create_task('T1')
        runtime.prepare('T1', 'N5', [{'robot_id': 'sim-r1', 'node': 'N1', 'available': True}], {'type': 'load'})
        runtime.publish('T1')
        order = received.get(timeout=3)
        challenge = runtime.begin('T1', 'boot-1')['challenge']
        runtime.complete('T1', 'boot-1', challenge, 'sim-r1', 'T1', 1,
                         'integration@v1', 'N3', _digest_json(order), True)
        data = two_segment_map()
        old = set(plan_segment(data, ['N1', 'N2', 'N3', 'N4', 'N5']).resources)
        new = set(plan_segment(data, ['N3', 'N4', 'N5']).resources)
        event = {'type': 'rear_clear', 'event_id': 'changed-session', 'robot_session': 'boot-2',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'integration@v1', 'stopped_at': 'N3',
                 'cleared_resources': [list(item) for item in sorted(old - new)]}
        robot.publish('agv/sim-r1/event', json.dumps(event), qos=1).wait_for_publish(timeout=3)
        deadline = time.monotonic() + 3
        while 'T1' not in runtime.gate._blocked and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn('T1', runtime.gate._blocked)
        event.update(event_id='old-session', robot_session='boot-1')
        robot.publish('agv/sim-r1/event', json.dumps(event), qos=1).wait_for_publish(timeout=3)
        with self.assertRaises(queue.Empty):
            received.get(timeout=0.3)
        self.assertEqual(set(runtime.store.list_occupancy()), old)
