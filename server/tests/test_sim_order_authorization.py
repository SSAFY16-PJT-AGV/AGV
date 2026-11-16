"""Incoming virtual MQTT orders must match a currently reserved outbox."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from server.allocation_store import AllocationStore
from server.coordinator import Coordinator
from server.dispatch import RobotCandidate
from server.recovery import RecoveryGate
from server.recovery import _digest_json
from server.segment_progress import SegmentProgress
from server.segments import plan_segment
from server.sim_order_authorization import SimOrderAuthorizer
from server.tests.test_coordinator import FakePublisher, map_data
from sim.order_inbox import SimOrderInbox
from sim.order_executor import SimOrderExecutor
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.tests.test_mqtt_order_robot import FakeClient


class Receiver:
    def __init__(self, progress, gate):
        self._dispatch_lock = threading.RLock()
        self.progress, self.gate, self.ready = progress, gate, True
        self.robot_sessions = {'sim-r1': {'T1': 'boot-1'}}


class Runtime:
    def __init__(self, path, store, progress, gate):
        self.path, self.store, self.progress, self.gate = str(path), store, progress, gate
        self._lock = threading.RLock()
        self._tasks = {'T1': ('sim-r1', 'boot-1')}
        self.receiver = Receiver(progress, gate)
        self.is_ready = True
    def _ready(self):
        if not self.is_ready or not self.receiver.ready:
            raise RuntimeError('not ready')


class SimOrderAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'allocation.sqlite3'
        self.store = AllocationStore(path)
        self.store.initialize()
        self.map = map_data()
        self.gate = RecoveryGate(path)
        self.gate.initialize()
        self.progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=self.gate)
        self.progress.initialize()
        self.store.create_task('T1')
        self.publisher = FakePublisher()
        self.coordinator = Coordinator(self.store, self.map, self.publisher)
        self.allocation = self.coordinator.prepare('T1', 'N4', [RobotCandidate('sim-r1', 'N1', True)], {'type': 'load'})
        self.segment = plan_segment(self.map, self.allocation['nodes'])
        self.record = self.store.get_order('T1')
        self.payload = self.record['payload'].encode('utf-8')
        self.runtime = Runtime(path, self.store, self.progress, self.gate)
        self.authorizer = SimOrderAuthorizer(self.runtime)

    def attempted(self):
        self.store.mark_attempted('T1', self.record['payload'], self.allocation, self.segment.resources)

    def test_only_attempted_or_acknowledged_exact_order_is_allowed(self):
        self.assertFalse(self.authorizer(self.payload))
        self.attempted()
        self.assertTrue(self.authorizer(self.payload))
        self.store.mark_broker_acked('T1', self.record['payload'], self.allocation, self.segment.resources)
        self.assertTrue(self.authorizer(self.payload))

    def test_second_robot_guard_accepts_only_its_own_reserved_order(self):
        self.attempted()
        self.store.create_task('T2')
        publisher = FakePublisher()
        coordinator = Coordinator(self.store, self.map, publisher)
        allocation = coordinator.prepare('T2', 'N6', [RobotCandidate('sim-r2', 'N5', True)], {'type': 'load'})
        segment = plan_segment(self.map, allocation['nodes'])
        record = self.store.get_order('T2')
        self.store.mark_attempted('T2', record['payload'], allocation, segment.resources)
        payload2 = record['payload'].encode('utf-8')
        self.runtime._tasks['T2'] = ('sim-r2', 'boot-2')
        self.runtime.receiver.robot_sessions['sim-r2'] = {'T2': 'boot-2'}
        second = SimOrderAuthorizer(self.runtime, robot_id='sim-r2')
        self.assertTrue(second(payload2))
        self.assertFalse(second(self.payload))
        self.assertFalse(self.authorizer(payload2))

    def test_wrong_task_sequence_release_map_and_payload_fail_closed(self):
        self.attempted()
        order = json.loads(self.payload)
        for modified in (dict(order, order_id='missing'), dict(order, seq=2),
                         dict(order, released_until='N4'), dict(order, map_id='other@v4'),
                         dict(order, action_at_end={'type': 'unload'})):
            with self.subTest(modified=modified):
                self.assertFalse(self.authorizer(json.dumps(modified).encode()))
        self.assertFalse(self.authorizer(b'{invalid'))

    def test_missing_or_extra_occupied_resource_rejected(self):
        self.attempted()
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("DELETE FROM allocation_occupancy WHERE kind='node' AND resource_id='N2'")
            db.commit()
        self.assertFalse(self.authorizer(self.payload))

    def test_receiver_or_session_unavailable_rejects(self):
        self.attempted()
        self.runtime.receiver.ready = False
        self.assertFalse(self.authorizer(self.payload))
        self.runtime.receiver.ready = True
        self.runtime.receiver.robot_sessions['sim-r1']['T1'] = 'old-session'
        self.assertFalse(self.authorizer(self.payload))

    def test_revoked_recovery_epoch_or_blocked_task_rejects(self):
        self.attempted()
        self.gate._block('T1')
        self.assertFalse(self.authorizer(self.payload))

    def test_guard_holds_dispatch_lock_until_durable_acceptance_finishes(self):
        self.attempted()
        attempted, acquired = threading.Event(), threading.Event()
        def change():
            attempted.set()
            with self.runtime.receiver._dispatch_lock:
                acquired.set()
        with self.authorizer.guard(self.payload):
            thread = threading.Thread(target=change)
            thread.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(acquired.wait(0.1))
        thread.join(timeout=2)
        self.assertTrue(acquired.is_set())

    def test_delayed_old_qos1_duplicate_stops_without_ack_or_reexecution(self):
        # This describes the current conservative limitation, not a promise of
        # transparent duplicate handling once the reservation has advanced.
        self.coordinator.publish('T1')
        inbox = SimOrderInbox(Path(self.tmp.name) / 'robot.sqlite3', 'sim-r1', 'layout@v4')
        inbox.initialize()
        executor = SimOrderExecutor(inbox, self.map, 'N1')
        client = FakeClient()
        with patch('sim.mqtt_order_robot.mqtt.Client', return_value=client), \
             patch('sim.mqtt_order_robot.read_mqtt_security', return_value=(None, None, None)):
            robot = SimMqttOrderRobot(executor, 19123, authorization_guard=self.authorizer.guard).start()
            self.addCleanup(robot.stop)
            client.deliver(self.payload, mid=1)
            stopped = robot.tick(1)
            self.assertEqual((stopped['status'], stopped['current_node']), ('stopped', 'N3'))
            order = json.loads(self.payload)
            challenge = self.gate.begin('T1', 'boot-1')
            self.gate.complete('T1', 'boot-1', challenge, robot_id='sim-r1', order_id='T1', seq=1,
                               map_id='layout@v4', stopped_at='N3', order_digest=_digest_json(order), stopped=True)
            following = plan_segment(self.map, ['N3', 'N4'])
            event = {'type': 'rear_clear', 'event_id': 'event-1', 'robot_session': 'boot-1',
                     'order_id': 'T1', 'seq': 1, 'map_id': 'layout@v4', 'stopped_at': 'N3',
                     'cleared_resources': [list(item) for item in sorted(set(self.segment.resources)-set(following.resources))]}
            advanced = self.progress.advance('sim-r1', event)
            self.assertEqual((advanced['status'], advanced['next_seq']), ('advanced', 2))
            payload2 = advanced['payload']
            self.progress.mark_attempted('T1', 2, 'boot-1', payload2, following.resources)
            self.progress.mark_broker_acked('T1', 2, 'boot-1', payload2, following.resources)
            client.deliver(payload2.encode('utf-8'), mid=2)
            self.assertEqual(robot.tick(0)['order']['seq'], 2)
            position_before = executor.snapshot()['pose']
            self.assertFalse(inbox.accept(self.payload).new)
            client.deliver(self.payload, mid=3)
            with self.assertRaises(RuntimeError):
                robot.tick(0.1)
            self.assertEqual(executor.snapshot()['pose'], position_before)
            self.assertFalse(robot.ready)
            self.assertEqual(client.acks, [(1, 1), (2, 1)])


if __name__ == '__main__':
    unittest.main()
