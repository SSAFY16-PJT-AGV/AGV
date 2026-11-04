"""Optional end-to-end test using a dedicated loopback broker and fake robot."""

import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import paho.mqtt.client as mqtt

from server.allocation_store import AllocationConflict, AllocationStore
from server.coordinator import Coordinator
from server.dispatch import RobotCandidate
from server.order_publisher import OrderPublisher
from server.task_store import SqliteTaskStore


def fixture_map():
    return {
        'map_id': 'integration', 'version': 1, 'unit': 'mm',
        'nodes': [{'id': 'N0', 'pos': [-10, 0]}, {'id': 'N1', 'pos': [0, 0]}, {'id': 'N2', 'pos': [10, 0]},
                  {'id': 'N3', 'pos': [20, 0]}, {'id': 'N4', 'pos': [30, 0]}],
        'edges': [
            {'from': 'N0', 'to': 'N1', 'shape': [[-10, 0], [0, 0]], 'bidirectional': True, 'lanes': 1},
            {'from': 'N1', 'to': 'N2', 'shape': [[0, 0], [10, 0]], 'bidirectional': True, 'lanes': 1},
            {'from': 'N2', 'to': 'N3', 'shape': [[10, 0], [20, 0]], 'bidirectional': True, 'lanes': 1},
            {'from': 'N3', 'to': 'N4', 'shape': [[20, 0], [30, 0]], 'bidirectional': True, 'lanes': 1},
        ],
        'zones': [{'id': 'cross', 'type': 'shared', 'nodes': ['N2']},
                  {'id': 'wait3', 'type': 'waiting', 'node': 'N3'},
                  {'id': 'wait4', 'type': 'waiting', 'node': 'N4'}],
    }


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def stop_client(client):
    try:
        client.disconnect()
    finally:
        client.loop_stop()


@unittest.skipUnless(os.environ.get('MOSQUITTO_EXE'), 'set MOSQUITTO_EXE to run isolated broker integration')
class FleetMqttIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.path = folder / 'fleet.sqlite3'
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserve:
            reserve.bind(('127.0.0.1', 0))
            self.port = reserve.getsockname()[1]
        config = folder / 'mosquitto.conf'
        config.write_text('listener {} 127.0.0.1\nallow_anonymous true\nlog_type all\nlog_dest stderr\n'.format(self.port),
                          encoding='utf-8')
        log_path = folder / 'broker.log'
        log = log_path.open('w+', encoding='utf-8')
        self.addCleanup(log.close)
        self.broker = subprocess.Popen(
            [os.environ['MOSQUITTO_EXE'], '-c', str(config)],
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        self.addCleanup(stop_process, self.broker)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.broker.poll() is not None:
                self.fail('test broker exited before becoming ready')
            if re.search(r'mosquitto version .* running', log_path.read_text(encoding='utf-8')):
                break
            time.sleep(0.05)
        else:
            self.fail('test broker did not report running')

    def new_client(self):
        if self.broker.poll() is not None:
            self.fail('test broker unexpectedly exited')
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311,
                             reconnect_on_failure=False)
        self.addCleanup(stop_client, client)
        return client

    def connect(self, client, on_connect):
        connected = threading.Event()
        errors = []

        def callback(client, userdata, flags, reason_code, properties):
            try:
                if reason_code.is_failure:
                    errors.append('connection rejected')
                elif on_connect is not None:
                    on_connect(client)
            finally:
                connected.set()

        client.on_connect = callback
        client.connect('127.0.0.1', self.port, keepalive=15)
        client.loop_start()
        self.assertTrue(connected.wait(5), 'MQTT connection timed out')
        self.assertFalse(errors, 'MQTT connection rejected')

    def test_fake_robot_order_and_reservation_survive_restart(self):
        received, subscribed = threading.Event(), threading.Event()
        delivered = []
        robot = self.new_client()

        def on_subscribe(client, userdata, mid, reason_codes, properties):
            if len(reason_codes) == 1 and not reason_codes[0].is_failure and reason_codes[0].value == 1:
                subscribed.set()

        def on_message(client, userdata, message):
            delivered.append((message.topic, message.payload, message.qos, message.retain))
            received.set()

        robot.on_subscribe = on_subscribe
        robot.on_message = on_message
        self.connect(robot, lambda client: client.subscribe('agv/sim-r1/order', qos=1))
        self.assertTrue(subscribed.wait(5), 'fake robot subscription was not acknowledged')

        sender = self.new_client()
        self.connect(sender, None)
        publisher = OrderPublisher(sender, enabled=True)
        store = AllocationStore(self.path)
        store.initialize()
        store.create_task('T1')
        store.create_task('T2')
        coordinator = Coordinator(store, fixture_map(), publisher, ack_timeout=3)
        allocation = coordinator.prepare(
            'T1', 'N4', [RobotCandidate('sim-r1', 'N1', True),
                         RobotCandidate('sim-r2', 'N0', True)], {'type': 'load'})
        self.assertEqual((allocation['robot_id'], allocation['released_until']), ('sim-r1', 'N3'))
        self.assertEqual(coordinator.publish('T1')['state'], 'acked')
        self.assertTrue(received.wait(5), 'fake robot did not receive the order')
        self.assertEqual(len(delivered), 1)
        topic, payload, qos, retain = delivered[0]
        self.assertEqual((topic, qos, retain), ('agv/sim-r1/order', 1, False))
        self.assertEqual(json.loads(payload), json.loads(store.get_order('T1')['payload']))

        # Reopen a new object to verify that the DB, not process memory, retains occupancy.
        reopened = AllocationStore(self.path)
        expected = {('node', 'N1'): 'sim-r1', ('node', 'N2'): 'sim-r1',
                    ('node', 'N3'): 'sim-r1', ('edge', '["N1","N2"]'): 'sim-r1',
                    ('edge', '["N2","N3"]'): 'sim-r1', ('zone', 'cross'): 'sim-r1'}
        self.assertEqual(reopened.list_occupancy(), expected)
        self.assertEqual(reopened.get_order('T1')['state'], 'acked')

        second_sender = self.new_client()
        self.connect(second_sender, None)
        other = Coordinator(reopened, fixture_map(), OrderPublisher(second_sender, enabled=True))
        with self.assertRaises(AllocationConflict):
            other.prepare('T2', 'N4', [RobotCandidate('sim-r2', 'N1', True)], {'type': 'load'})
        self.assertIsNone(reopened.get_order('T2'))
        self.assertEqual(SqliteTaskStore(self.path).get('T2').status, 'PENDING')
        self.assertEqual(reopened.list_occupancy(), expected)


if __name__ == '__main__':
    unittest.main()
