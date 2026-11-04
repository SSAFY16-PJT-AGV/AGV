"""Optional, isolated MQTT end-to-end test; no hardware or existing broker."""
import json
import os
import queue
import threading
import time
import unittest
from types import SimpleNamespace

import paho.mqtt.client as mqtt

from server.allocation_store import AllocationConflict, AllocationStore
from server.coordinator import Coordinator
from server.dispatch import RobotCandidate
from server.order_publisher import OrderPublisher
from server.progress_publisher import ProgressPublisher
from server.recovery import RecoveryError, RecoveryGate
from server.segment_progress import SegmentProgress
from server.segments import plan_segment
from server.tests import test_fleet_mqtt_integration as fleet_fixture
from sim.order_inbox import SimOrderInbox
from sim.event_receiver import SimEventReceiver
from sim.event_inbox import SimEventInbox


def two_segment_map():
    return {
        'map_id': 'integration', 'version': 1, 'unit': 'mm',
        'nodes': [{'id': 'N{}'.format(i), 'pos': [10 * i, 0]} for i in range(1, 6)],
        'edges': [{'from': 'N{}'.format(i), 'to': 'N{}'.format(i + 1),
                   'shape': [[10 * i, 0], [10 * (i + 1), 0]],
                   'bidirectional': True, 'lanes': 1} for i in range(1, 5)],
        'zones': [{'id': 'cross1', 'type': 'shared', 'nodes': ['N2']},
                  {'id': 'cross2', 'type': 'shared', 'nodes': ['N4']},
                  {'id': 'wait3', 'type': 'waiting', 'node': 'N3'},
                  {'id': 'wait5', 'type': 'waiting', 'node': 'N5'}],
    }


@unittest.skipUnless(os.environ.get('MOSQUITTO_EXE'), 'set MOSQUITTO_EXE to run isolated broker integration')
class SegmentRecoveryMqttTest(unittest.TestCase):
    # Reuse only the test-broker fixture, not an existing application broker.
    setUp = fleet_fixture.FleetMqttIntegrationTest.setUp
    new_client = fleet_fixture.FleetMqttIntegrationTest.new_client
    connect = fleet_fixture.FleetMqttIntegrationTest.connect

    def test_persistent_session_queued_event_is_stored_before_suback(self):
        store = AllocationStore(self.path)
        store.initialize()
        gate = RecoveryGate(self.path)
        gate.initialize()
        calls = []
        progress = SimpleNamespace(_gate=gate, store=store,
            advance=lambda *args, **kwargs: calls.append((args, kwargs)))
        inbox = SimEventInbox(self.path, robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True)
        inbox.initialize()
        subscribed = threading.Event()
        first = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='queued-observer', clean_session=False,
                            protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        self.addCleanup(fleet_fixture.stop_client, first)
        first.on_subscribe = lambda client, userdata, mid, codes, properties: subscribed.set()
        self.connect(first, lambda c: c.subscribe([('agv/+/event', 1), ('agv/+/conn', 1)]))
        self.assertTrue(subscribed.wait(5))
        first.disconnect()
        first.loop_stop()

        robot = self.new_client()
        self.connect(robot, None)
        event = {'type': 'rear_clear', 'event_id': 'queued-event', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'integration@v1', 'stopped_at': 'N3',
                 'cleared_resources': [['node', 'N1']]}
        info = robot.publish('agv/sim-r1/event', json.dumps(event).encode(), qos=1)
        info.wait_for_publish(timeout=5)
        self.assertTrue(info.is_published())
        second = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='queued-observer', clean_session=False,
                             protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        self.addCleanup(fleet_fixture.stop_client, second)
        errors, early = [], []
        receiver = SimEventReceiver(second, progress, inbox=inbox, recovery_gate=gate,
            robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True, on_error=errors.append)
        original_message = second.on_message
        def record_message(client, userdata, message):
            early.append(not receiver.ready)
            original_message(client, userdata, message)
        second.on_message = record_message
        second.connect('127.0.0.1', self.port, keepalive=15)
        second.loop_start()
        deadline = time.monotonic() + 5
        while (not receiver.ready or inbox.get('sim-r1', 'queued-event') is None) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(receiver.ready)
        self.assertEqual(early, [True], 'expected broker to deliver queued event before SUBACK')
        self.assertEqual(inbox.get('sim-r1', 'queued-event')['state'], 'pending')
        self.assertEqual(calls, [])
        self.assertEqual(errors, [])

    def test_two_segments_duplicates_conflict_and_restart_recovery(self):
        map_data = two_segment_map()
        path = ['N1', 'N2', 'N3', 'N4', 'N5']
        old_segment = plan_segment(map_data, path)
        next_segment = plan_segment(map_data, path[2:])
        old_resources, next_resources = set(old_segment.resources), set(next_segment.resources)
        store = AllocationStore(self.path)
        store.initialize()
        store.create_task('T1')

        # This simulator persists the inbox and never moves a real device.
        inbox_path = self.path.parent / 'sim-inbox.sqlite3'
        inbox = SimOrderInbox(inbox_path, 'sim-r1', 'integration@v1')
        inbox.initialize()
        inbox_ref = [inbox]
        order_results = queue.Queue()
        order_subscribed = threading.Event()
        robot = self.new_client()

        def order_suback(client, userdata, mid, reasons, properties):
            if len(reasons) == 1 and not reasons[0].is_failure and reasons[0].value == 1:
                order_subscribed.set()

        def receive_order(client, userdata, message):
            try:
                order_results.put((message.qos, message.retain, inbox_ref[0].accept(message.payload)))
            except Exception as exc:
                order_results.put(exc)

        robot.on_subscribe = order_suback
        robot.on_message = receive_order
        self.connect(robot, lambda client: client.subscribe('agv/sim-r1/order', qos=1))
        self.assertTrue(order_subscribed.wait(5))

        sender1 = self.new_client()
        self.connect(sender1, None)
        coordinator = Coordinator(store, map_data, OrderPublisher(sender1, enabled=True), ack_timeout=3)
        allocation = coordinator.prepare('T1', 'N5', [RobotCandidate('sim-r1', 'N1', True)], {'type': 'load'})
        self.assertEqual(allocation['released_until'], 'N3')
        self.assertEqual(coordinator.publish('T1')['state'], 'acked')
        qos, retained, receipt1 = order_results.get(timeout=5)
        self.assertEqual((qos, retained, receipt1.new, receipt1.seq), (1, False, True, 1))
        inbox.mark_completed('T1', 1, receipt1.digest, 'N3', rear_clear_confirmed=True)

        # Proposed events are handled only by a simulation-only receiver.
        gate = RecoveryGate(self.path)
        gate.initialize()
        progress = SegmentProgress(store, map_data, simulation_enabled=True, recovery_gate=gate)
        progress.initialize()
        event_results = queue.Queue()
        event_inbox = SimEventInbox(self.path, robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True)
        event_inbox.initialize()

        def start_receiver(progress_instance, gate_instance):
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='sim-observer', clean_session=False,
                                 protocol=mqtt.MQTTv311, reconnect_on_failure=False)
            self.addCleanup(fleet_fixture.stop_client, client)
            receiver = SimEventReceiver(client, progress_instance, inbox=event_inbox, recovery_gate=gate_instance,
                robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True,
                on_result=event_results.put, on_error=event_results.put)
            client.connect('127.0.0.1', self.port, keepalive=15)
            client.loop_start()
            deadline = time.monotonic() + 5
            while not receiver.ready and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(receiver.ready, 'event subscriptions did not become ready')
            return client, receiver

        observer, receiver = start_receiver(progress, gate)

        def publish_event(event):
            info = robot.publish('agv/sim-r1/event', json.dumps(event).encode(), qos=1, retain=False)
            info.wait_for_publish(timeout=5)
            self.assertTrue(info.is_published())
            return event_results.get(timeout=5)

        event1 = {'type': 'rear_clear', 'event_id': 'event-1', 'robot_session': 'boot-1',
                  'order_id': 'T1', 'seq': 1, 'map_id': 'integration@v1', 'stopped_at': 'N3',
                  'cleared_resources': [list(x) for x in sorted(old_resources - next_resources)]}
        self.assertIsInstance(publish_event(event1), RecoveryError)
        self.assertEqual(event_inbox.get('sim-r1', 'event-1')['state'], 'pending')
        self.assertEqual(set(store.list_occupancy()), old_resources)
        token = gate.begin('T1', 'boot-1')
        gate.complete('T1', 'boot-1', token, robot_id='sim-r1', order_id='T1', seq=1,
                      map_id='integration@v1', stopped_at='N3', order_digest=receipt1.digest, stopped=True)
        # A simulated robot disconnect invalidates its prior proof; online
        # alone is deliberately insufficient to grant another one.
        offline = robot.publish('agv/sim-r1/conn', b'offline', qos=0, retain=False)
        offline.wait_for_publish(timeout=5)
        self.assertTrue(offline.is_published())
        self.assertEqual(publish_event(event1)['status'], 'duplicate')
        with self.assertRaises(RecoveryError):
            event_inbox.retry('sim-r1', 'event-1', progress)
        self.assertEqual(event_inbox.get('sim-r1', 'event-1')['state'], 'pending')
        self.assertEqual(set(store.list_occupancy()), old_resources)
        token = gate.begin('T1', 'boot-1')
        gate.complete('T1', 'boot-1', token, robot_id='sim-r1', order_id='T1', seq=1,
                      map_id='integration@v1', stopped_at='N3', order_digest=receipt1.digest, stopped=True)
        advanced = event_inbox.retry('sim-r1', 'event-1', progress)
        self.assertEqual(event_inbox.get('sim-r1', 'event-1')['state'], 'applied')
        self.assertIsInstance(advanced, dict)
        self.assertEqual((advanced['status'], advanced['next_seq']), ('advanced', 2))
        self.assertEqual(set(store.list_occupancy()), next_resources)
        self.assertEqual(publish_event(event1)['status'], 'duplicate')
        self.assertEqual(set(store.list_occupancy()), next_resources)

        # An overlapping second robot cannot obtain any part of the segment.
        store.create_task('T2')
        sender_other = self.new_client()
        self.connect(sender_other, None)
        other = Coordinator(store, map_data, OrderPublisher(sender_other, enabled=True))
        with self.assertRaises(AllocationConflict):
            other.prepare('T2', 'N5', [RobotCandidate('sim-r2', 'N4', True)], {'type': 'load'})
        self.assertEqual(set(store.list_occupancy()), next_resources)

        # Explicit simulation wrapper; the production server does not yet
        # invoke this method or consume the proposed events automatically.
        sender2 = self.new_client()
        self.connect(sender2, None)
        payload2 = progress.get_order('T1', 2)['payload']
        publisher2 = ProgressPublisher(progress, OrderPublisher(sender2, enabled=True), ack_timeout=3)
        self.assertEqual(publisher2.publish('T1', 2, 'boot-1')['state'], 'acked')
        _, _, receipt2 = order_results.get(timeout=5)
        self.assertTrue(receipt2.new)
        self.assertEqual(receipt2.seq, 2)

        # A duplicate broker delivery cannot cause a second simulated execution.
        duplicate = robot.publish('agv/sim-r1/order', payload2, qos=1, retain=False)
        duplicate.wait_for_publish(timeout=5)
        self.assertTrue(duplicate.is_published())
        _, _, duplicate_receipt = order_results.get(timeout=5)
        self.assertFalse(duplicate_receipt.new)
        self.assertEqual(duplicate_receipt.digest, receipt2.digest)

        # Restart both persistence users: no automatic execution or release.
        inbox_ref[0] = SimOrderInbox(inbox_path, 'sim-r1', 'integration@v1')
        inbox_ref[0].initialize()
        self.assertFalse(inbox_ref[0].accept(payload2.encode()).new)
        with self.assertRaises(ValueError):
            inbox_ref[0].mark_completed('T1', 2, receipt2.digest, 'N5', rear_clear_confirmed=True)
        observer.disconnect()
        observer.loop_stop()
        self.assertIsInstance(event_results.get(timeout=5), RuntimeError)
        reopened = AllocationStore(self.path)
        reopened.initialize()
        gate2 = RecoveryGate(self.path)
        gate2.initialize()
        progress2 = SegmentProgress(reopened, map_data, simulation_enabled=True, recovery_gate=gate2)
        progress2.initialize()
        observer2, receiver2 = start_receiver(progress2, gate2)
        final_event = {'type': 'rear_clear', 'event_id': 'event-2', 'robot_session': 'boot-2',
                       'order_id': 'T1', 'seq': 2, 'map_id': 'integration@v1', 'stopped_at': 'N5',
                       'cleared_resources': [list(x) for x in sorted(next_resources - {('node', 'N5')})]}
        self.assertIsInstance(publish_event(final_event), RecoveryError)
        self.assertEqual(set(reopened.list_occupancy()), next_resources)
        inbox_ref[0].reconcile_completed('T1', 2, receipt2.digest, 'N5', rear_clear_confirmed=True)
        token2 = gate2.begin('T1', 'boot-2')
        gate2.complete('T1', 'boot-2', token2, robot_id='sim-r1', order_id='T1', seq=2,
                       map_id='integration@v1', stopped_at='N5', order_digest=receipt2.digest, stopped=True)
        self.assertEqual(event_inbox.retry('sim-r1', 'event-2', progress2)['status'], 'finished')
        self.assertEqual(reopened.list_occupancy(), {('node', 'N5'): 'sim-r1'})
        self.assertEqual(publish_event(final_event)['status'], 'duplicate')


if __name__ == '__main__':
    unittest.main()
