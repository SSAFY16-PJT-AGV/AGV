"""Real isolated MQTT transport and allocation DB, using synthetic peers only."""
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
from queue import Queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import paho.mqtt.client as mqtt

from server.allocation_store import AllocationStore
from server.coordinator import Coordinator
from server.field_dispatch import FieldCandidate
from server.order_publisher import OrderPublisher
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress
from server.sim_command_observations import SimCommandObservations
from server.sim_command_observation_receiver import SimCommandObservationReceiver
from server.tests.test_occurrence_allocation_lab import laboratory_map
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port,
                                                       stop, stop_publisher, wait_for_owned_broker)
from server.tools.twin_multi_test import isolated_environment


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class SimObservationReceiverMqttTests(unittest.TestCase):
    def test_sent_order_and_response_use_real_broker_and_allocation_database(self):
        port = free_port()
        broker_env = isolated_environment(os.environ)
        broker_env['PYTHONPATH'] = str(Path(os.environ['AMQTT_TEST_PATH']).resolve())
        with tempfile.TemporaryDirectory(prefix='sim-observation-mqtt-') as folder, ExitStack() as cleanup, \
                open(os.devnull, 'w') as log, patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            ready_file = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', BROKER,
                                       str(port), ready_file], env=broker_env, stdout=log, stderr=log,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_file)
            peer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='sim-response-test-peer',
                               clean_session=True, reconnect_on_failure=False)
            received, subscribed = Queue(), threading.Event()
            def on_connect(client, _userdata, _flags, reason, _properties):
                if not reason.is_failure:
                    client.subscribe('agv/sim-r1/order', qos=1)
            def on_subscribe(_client, _userdata, _mid, reasons, _properties):
                if len(reasons) == 1 and not reasons[0].is_failure and reasons[0].value == 1:
                    subscribed.set()
            peer.on_connect, peer.on_subscribe = on_connect, on_subscribe
            peer.on_message = lambda _client, _user, msg: received.put(bytes(msg.payload))
            connect_publisher(peer, port)
            peer.loop_start()
            cleanup.callback(stop_publisher, peer)
            self.assertTrue(subscribed.wait(10))
            publisher_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='sim-response-test-publisher',
                                           clean_session=True, reconnect_on_failure=False)
            connected = threading.Event()
            publisher_client.on_connect = lambda _client, _user, _flags, reason, _props: (
                connected.set() if not reason.is_failure else None)
            connect_publisher(publisher_client, port)
            publisher_client.loop_start()
            cleanup.callback(stop_publisher, publisher_client)
            self.assertTrue(connected.wait(10))

            path = Path(folder) / 'allocation.sqlite3'
            data = laboratory_map()
            store = AllocationStore(path, field_required=True)
            store.initialize()
            store.create_task('T1')
            coordinator = Coordinator(store, data, OrderPublisher(publisher_client, enabled=True),
                                      field_simulation=True)
            gate = RecoveryGate(path, field_required=True)
            gate.initialize()
            progress = SegmentProgress(store, data, simulation_enabled=True, field_simulation=True, recovery_gate=gate)
            progress.initialize()
            observations = SimCommandObservations(progress, {'sim-r1': {'T1': 'boot-1'}}, simulation=True)
            observations.initialize()
            coordinator.prepare_field('T1', 'D', [FieldCandidate('sim-r1', 'A', 'E0', True)], {'type': 'none'})
            receiver = SimCommandObservationReceiver(observations, ['sim-r1'], port, simulation=True)
            receiver.start()
            cleanup.callback(receiver.stop)
            before = store.list_occupancy()
            coordinator.publish('T1')
            wire = received.get(timeout=10)
            order = json.loads(wire)
            response = {'event_id': 'response-1', 'robot_session': 'boot-1', 'order_id': order['order_id'],
                        'seq': order['seq'], 'map_id': order['map_id'], 'outcome': 'accepted',
                        'order_digest': hashlib.sha256(wire).hexdigest()}
            body = json.dumps(response, separators=(',', ':'))
            def publish_and_wait(expected):
                info = peer.publish('sim-observation/sim-r1/order-response', body, qos=1, retain=False)
                info.wait_for_publish(timeout=5)
                self.assertTrue(info.is_published())
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and receiver.last_result != expected:
                    time.sleep(.02)
                self.assertEqual(receiver.last_result, expected)
            publish_and_wait({'status': 'recorded', 'outcome': 'accepted'})
            publish_and_wait({'status': 'duplicate', 'outcome': 'accepted'})
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT robot_id,event_id,task_id,seq,outcome '
                                            'FROM sim_command_observations').fetchall(),
                                 [('sim-r1', 'response-1', 'T1', 1, 'accepted')])
            self.assertEqual(store.list_occupancy(), before)
            self.assertEqual(store.get_order('T1')['state'], 'acked')
            self.assertIsNone(progress.get_order('T1', 2))
            receiver.stop()
            self.assertFalse(receiver.ready)
            lost = peer.publish('sim-observation/sim-r1/order-response', body, qos=1, retain=False)
            lost.wait_for_publish(timeout=5)
            self.assertTrue(lost.is_published())
            reopened = SimCommandObservationReceiver(observations, ['sim-r1'], port, simulation=True)
            reopened.start()
            cleanup.callback(reopened.stop)
            # A new clean session is not a replay mechanism. Observe its state
            # briefly before explicitly sending the same response once again.
            time.sleep(.3)
            self.assertIsNone(reopened.last_result)
            info = peer.publish('sim-observation/sim-r1/order-response', body, qos=1, retain=False)
            info.wait_for_publish(timeout=5)
            self.assertTrue(info.is_published())
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and reopened.last_result is None:
                time.sleep(.02)
            self.assertEqual(reopened.last_result, {'status': 'duplicate', 'outcome': 'accepted'})
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM sim_command_observations').fetchone(), (1,))
            self.assertEqual(store.list_occupancy(), before)


if __name__ == '__main__':
    unittest.main()
