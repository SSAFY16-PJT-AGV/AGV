"""Optional isolated broker tests for the simulation-only progression workflow."""
from contextlib import contextmanager
import json
import os
import queue
import threading
import time
import unittest

import paho.mqtt.client as mqtt

from server.allocation_store import AllocationConflict, AllocationStore
from server.coordinator import Coordinator
from server.dispatch import RobotCandidate
from server.order_publisher import OrderPublisher
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress
from server.segments import plan_segment
from server.tests import test_fleet_mqtt_integration as fixture
from server.tests.test_segment_recovery_mqtt import two_segment_map
from sim.event_inbox import SimEventInbox
from sim.event_receiver import SimEventReceiver
from sim.order_inbox import SimOrderInbox
from sim.workflow import SimProgressWorkflow


@unittest.skipUnless(os.environ.get('MOSQUITTO_EXE'), 'set MOSQUITTO_EXE for isolated broker tests')
class WorkflowMqttTest(unittest.TestCase):
    setUp = fixture.FleetMqttIntegrationTest.setUp
    new_client = fixture.FleetMqttIntegrationTest.new_client
    connect = fixture.FleetMqttIntegrationTest.connect

    def prepare(self):
        data = two_segment_map()
        store = AllocationStore(self.path)
        store.initialize()
        store.create_task('T1')
        inbox = SimOrderInbox(self.path.parent / 'robot.sqlite3', 'sim-r1', 'integration@v1')
        inbox.initialize()
        received, subscribed = queue.Queue(), threading.Event()
        robot = self.new_client()
        def receive(client, userdata, message):
            try:
                received.put(inbox.accept(message.payload))
            except Exception as exc:
                received.put(exc)
        robot.on_message = receive
        robot.on_subscribe = lambda client, userdata, mid, codes, properties: subscribed.set()
        self.connect(robot, lambda client: client.subscribe('agv/sim-r1/order', qos=1))
        self.assertTrue(subscribed.wait(5))
        sender = self.new_client()
        self.connect(sender, None)
        coordinator = Coordinator(store, data, OrderPublisher(sender, enabled=True), ack_timeout=3)
        coordinator.prepare('T1', 'N5', [RobotCandidate('sim-r1', 'N1', True)], {'type': 'load'})
        self.assertEqual(coordinator.publish('T1')['state'], 'acked')
        first = received.get(timeout=5)
        self.assertTrue(first.new)
        inbox.mark_completed('T1', 1, first.digest, 'N3', rear_clear_confirmed=True)
        gate = RecoveryGate(self.path)
        gate.initialize()
        progress = SegmentProgress(store, data, simulation_enabled=True, recovery_gate=gate)
        progress.initialize()
        event_inbox = SimEventInbox(self.path, robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True)
        event_inbox.initialize()
        errors = queue.Queue()
        observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='workflow-observer', clean_session=False,
                               protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        self.addCleanup(fixture.stop_client, observer)
        receiver = SimEventReceiver(observer, progress, inbox=event_inbox, recovery_gate=gate,
            robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True, on_error=errors.put)
        observer.connect('127.0.0.1', self.port, keepalive=15)
        observer.loop_start()
        deadline = time.monotonic() + 5
        while not receiver.ready and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(receiver.ready)
        old = set(plan_segment(data, ['N1','N2','N3','N4','N5']).resources)
        following = set(plan_segment(data, ['N3','N4','N5']).resources)
        event = {'type':'rear_clear', 'event_id':'workflow-event-1', 'robot_session':'boot-1',
                 'order_id':'T1', 'seq':1, 'map_id':'integration@v1', 'stopped_at':'N3',
                 'cleared_resources':[list(x) for x in sorted(old - following)]}
        def prove():
            challenge = gate.begin('T1', 'boot-1')
            gate.complete('T1','boot-1',challenge,robot_id='sim-r1',order_id='T1',seq=1,
                          map_id='integration@v1',stopped_at='N3',order_digest=first.digest,stopped=True)
        def publish_event():
            result = robot.publish('agv/sim-r1/event', json.dumps(event).encode(), qos=1, retain=False)
            result.wait_for_publish(timeout=5)
            self.assertTrue(result.is_published())
        return store, data, received, gate, progress, event_inbox, receiver, observer, errors, following, prove, publish_event, first.digest

    def test_automatic_workflow_delivers_next_order_once(self):
        store, data, received, gate, progress, inbox, receiver, observer, errors, following, prove, send, digest = self.prepare()
        factory_calls, results, work_errors = [], queue.Queue(), queue.Queue()
        @contextmanager
        def publisher_factory(robot, task, seq):
            factory_calls.append((robot, task, seq))
            client = self.new_client()
            self.connect(client, None)
            try:
                yield OrderPublisher(client, enabled=True)
            finally:
                fixture.stop_client(client)
        with self.assertRaises(ValueError):
            SimProgressWorkflow(receiver, robot_sessions={'sim-r1':{'T1':'boot-1'}}, publisher_factory=publisher_factory)
        prove()
        self.assertEqual(factory_calls, [])
        workflow = SimProgressWorkflow(receiver, robot_sessions={'sim-r1':{'T1':'boot-1'}},
            publisher_factory=publisher_factory, simulation_enabled=True,
            on_result=results.put, on_error=work_errors.put)
        workflow.start()
        self.addCleanup(workflow.stop)
        send()
        second = received.get(timeout=5)
        self.assertTrue(second.new)
        self.assertEqual(second.seq, 2)
        self.assertEqual(results.get(timeout=5)['state'], 'acked')
        self.assertEqual(factory_calls, [('sim-r1','T1',2)])
        self.assertEqual(set(store.list_occupancy()), following)
        send()
        with self.assertRaises(queue.Empty):
            received.get(timeout=0.4)
        self.assertEqual(factory_calls, [('sim-r1','T1',2)])
        self.assertTrue(work_errors.empty())
        store.create_task('T2')
        other_client = self.new_client()
        self.connect(other_client, None)
        other = Coordinator(store, data, OrderPublisher(other_client, enabled=True))
        with self.assertRaises(AllocationConflict):
            other.prepare('T2', 'N5', [RobotCandidate('sim-r2','N4',True)], {'type':'load'})
        self.assertEqual(set(store.list_occupancy()), following)

    def test_restart_requires_fresh_proof_before_explicit_pending_send(self):
        store, data, received, gate, progress, inbox, receiver, observer, errors, following, prove, send, digest = self.prepare()
        prove()
        # With no workflow started, receiving an event prepares seq2 but sends nothing.
        send()
        deadline = time.monotonic() + 5
        while (inbox.get('sim-r1', 'workflow-event-1') is None
               or inbox.get('sim-r1', 'workflow-event-1')['state'] != 'applied') and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(inbox.get('sim-r1', 'workflow-event-1')['state'], 'applied')
        with self.assertRaises(queue.Empty):
            received.get(timeout=0.3)
        self.assertEqual(progress.get_order('T1', 2)['state'], 'pending')
        observer.disconnect()
        observer.loop_stop()
        self.assertFalse(receiver.ready)

        # A fresh process has a fresh gate and receiver; the old proof is gone.
        reopened = AllocationStore(self.path)
        reopened.initialize()
        gate2 = RecoveryGate(self.path)
        gate2.initialize()
        progress2 = SegmentProgress(reopened, data, simulation_enabled=True, recovery_gate=gate2)
        progress2.initialize()
        inbox2 = SimEventInbox(self.path, robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True)
        inbox2.initialize()
        observer2 = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='workflow-observer', clean_session=False,
                                protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        self.addCleanup(fixture.stop_client, observer2)
        receiver2 = SimEventReceiver(observer2, progress2, inbox=inbox2, recovery_gate=gate2,
            robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True)
        observer2.connect('127.0.0.1', self.port, keepalive=15)
        observer2.loop_start()
        deadline = time.monotonic() + 5
        while not receiver2.ready and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(receiver2.ready)
        results, work_errors = queue.Queue(), queue.Queue()
        @contextmanager
        def publisher_factory(robot, task, seq):
            client = self.new_client()
            self.connect(client, None)
            try:
                yield OrderPublisher(client, enabled=True)
            finally:
                fixture.stop_client(client)
        workflow = SimProgressWorkflow(receiver2, robot_sessions={'sim-r1':{'T1':'boot-1'}},
            publisher_factory=publisher_factory, simulation_enabled=True,
            on_result=results.put, on_error=work_errors.put)
        workflow.start()
        self.addCleanup(workflow.stop)
        workflow.submit_pending('T1', 2, 'boot-1')
        self.assertEqual(work_errors.get(timeout=5), 'recovery proof rejected')
        with self.assertRaises(queue.Empty):
            received.get(timeout=0.3)
        self.assertEqual(progress2.get_order('T1', 2)['state'], 'pending')
        self.assertEqual(set(reopened.list_occupancy()), following)
        challenge = gate2.begin('T1', 'boot-1')
        gate2.complete('T1','boot-1',challenge,robot_id='sim-r1',order_id='T1',seq=1,
                       map_id='integration@v1',stopped_at='N3',order_digest=digest,stopped=True)
        workflow.submit_pending('T1', 2, 'boot-1')
        second = received.get(timeout=5)
        self.assertTrue(second.new)
        self.assertEqual(second.seq, 2)
        self.assertEqual(results.get(timeout=5)['state'], 'acked')


if __name__ == '__main__':
    unittest.main()
