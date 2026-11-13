"""Loopback-only MQTT TLS and password integration checks; no real broker."""

import json
import gc
import os
from pathlib import Path
import queue
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest.mock import patch

import paho.mqtt.client as mqtt

from server.mqtt_client import MqttService, start_mqtt
from server.mqtt_capture import CaptureError, CaptureSession
from server.recovery import _digest_json
from server.segments import plan_segment
from server.sim_runtime import SimRuntime
from server.tests.test_segment_recovery_mqtt import two_segment_map


BROKER_DEPS = os.environ.get('AMQTT_TEST_PATH')
OPENSSL = shutil.which('openssl')
TEST_USER = 'local-test-user'
TEST_PASSWORD = 'local-test-password'

BROKER_SCRIPT = r'''
import asyncio
import sys
from amqtt.broker import Broker

async def main():
    broker = Broker({
        'listeners': {'default': {'type': 'tcp', 'bind': '127.0.0.1:' + sys.argv[1],
                                  'ssl': True, 'certfile': sys.argv[2], 'keyfile': sys.argv[3]}},
        'plugins': {'amqtt.plugins.authentication.FileAuthPlugin': {'password_file': sys.argv[4]}},
    })
    await broker.start()
    await asyncio.Event().wait()

asyncio.run(main())
'''


def _free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _stop_process(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@unittest.skipUnless(BROKER_DEPS and OPENSSL, 'set AMQTT_TEST_PATH and install OpenSSL')
class MqttSecurityIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.workspace = tempfile.TemporaryDirectory(prefix='agv-mqtt-tls-')
        cls.addClassCleanup(cls.workspace.cleanup)
        path = Path(cls.workspace.name)
        cls.cert = path / 'broker.crt'
        key = path / 'broker.key'
        cls.other_ca = path / 'other.crt'
        other_key = path / 'other.key'
        broker_env = {key: value for key, value in os.environ.items()
                      if not key.upper().startswith('MQTT_')}
        broker_env['PYTHON_DOTENV_DISABLED'] = '1'
        for cert_path, key_path, name in ((cls.cert, key, 'test-broker'),
                                          (cls.other_ca, other_key, 'other-test-ca')):
            subprocess.run(
                [OPENSSL, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                 '-subj', '/CN=' + name, '-addext', 'subjectAltName=IP:127.0.0.1',
                 '-keyout', str(key_path), '-out', str(cert_path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, env=broker_env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        broker_env['PYTHONPATH'] = str(Path(BROKER_DEPS).resolve())
        python = getattr(sys, '_base_executable', sys.executable)
        hashed = subprocess.run(
            [python, '-c', 'import sys; from passlib.apps import custom_app_context; '
                           'print(custom_app_context.hash(sys.stdin.read()))'],
            input=TEST_PASSWORD, text=True, capture_output=True, check=True, timeout=15,
            env=broker_env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.strip()
        password_file = path / 'users.txt'
        password_file.write_text(TEST_USER + ':' + hashed + '\n', encoding='utf-8')
        cls.port = _free_port()
        cls.broker_log = (path / 'broker.log').open('w', encoding='utf-8')
        cls.addClassCleanup(cls.broker_log.close)
        cls.broker = subprocess.Popen(
            [python, '-c', BROKER_SCRIPT, str(cls.port), str(cls.cert), str(key), str(password_file)],
            env=broker_env, stdout=cls.broker_log, stderr=cls.broker_log,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        cls.addClassCleanup(_stop_process, cls.broker)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.broker.poll() is not None:
                raise RuntimeError('isolated TLS broker exited before starting')
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise TimeoutError('isolated TLS broker did not start')

    def _start(self, host='127.0.0.1', password=TEST_PASSWORD, ca=None):
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith('MQTT_')}
        env.update({'MQTT_HOST': host, 'MQTT_PORT': str(self.port), 'MQTT_USERNAME': TEST_USER,
                    'MQTT_PASSWORD': password, 'MQTT_TLS_CA': str(ca or self.cert),
                    'PYTHON_DOTENV_DISABLED': '1'})
        with patch.dict(os.environ, env, clear=True), patch('server.mqtt_client.load_dotenv'):
            return start_mqtt()

    def test_authenticated_tls_receives_state(self):
        service = self._start()
        self.addCleanup(service.stop)
        deadline = time.monotonic() + 8
        while not service.snapshot()['ready'] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(service.snapshot()['ready'], 'authenticated subscription was not ready')
        publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        publisher.username_pw_set(TEST_USER, TEST_PASSWORD)
        publisher.tls_set(ca_certs=str(self.cert))
        connected = threading.Event()
        publisher.on_connect = lambda client, userdata, flags, code, properties: (
            connected.set() if not code.is_failure else None)
        publisher.connect('127.0.0.1', self.port, keepalive=15)
        publisher.loop_start()
        self.addCleanup(publisher.loop_stop)
        self.addCleanup(publisher.disconnect)
        self.assertTrue(connected.wait(8), 'publisher failed to authenticate')
        payload = {'last_node': 'N1', 'status': 'IDLE', 'battery': 0.8}
        result = publisher.publish('agv/sim-test/state', json.dumps(payload), qos=1)
        result.wait_for_publish(timeout=5)
        self.assertTrue(result.is_published())
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            observed = service.snapshot()['robots'].get('sim-test', {}).get('state')
            if observed == payload:
                break
            time.sleep(0.05)
        self.assertEqual(observed, payload)

    def test_wrong_password_is_rejected(self):
        rejected = threading.Event()
        original = MqttService._on_disconnect

        def observe(instance, client, userdata, flags, reason_code, properties):
            original(instance, client, userdata, flags, reason_code, properties)
            rejected.set()

        # aMQTT closes the session on an authentication failure without CONNACK.
        with patch.object(MqttService, '_on_disconnect', observe):
            service = self._start(password='wrong-test-password')
            self.addCleanup(service.stop)
            self.assertTrue(rejected.wait(8), 'broker did not reject incorrect password')
            self.assertFalse(service.snapshot()['connected'])
            self.assertFalse(service.snapshot()['ready'])

    def test_wrong_ca_is_rejected(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            with self.assertRaises(ssl.SSLCertVerificationError):
                self._start(ca=self.other_ca)
            gc.collect()
        unclosed = [str(item.message) for item in caught
                    if issubclass(item.category, ResourceWarning)
                    and 'unclosed' in str(item.message).lower()
                    and 'socket' in str(item.message).lower()]
        self.assertFalse(unclosed, 'TLS failure left an unclosed socket: {}'.format(unclosed))

    def test_hostname_mismatch_is_rejected(self):
        # Certificate contains only IP:127.0.0.1, never DNS:localhost.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            with self.assertRaises(ssl.SSLCertVerificationError):
                self._start(host='localhost')
            gc.collect()
        unclosed = [str(item.message) for item in caught
                    if issubclass(item.category, ResourceWarning)
                    and 'unclosed' in str(item.message).lower()
                    and 'socket' in str(item.message).lower()]
        self.assertFalse(unclosed, 'TLS failure left an unclosed socket: {}'.format(unclosed))

    def test_capture_wrong_ca_closes_failed_tls_socket(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        session = CaptureSession(client, object())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            with self.assertRaises(CaptureError) as failure:
                session.run('127.0.0.1', self.port, username=TEST_USER,
                            password=TEST_PASSWORD, tls_ca=str(self.other_ca))
            self.assertIsInstance(failure.exception.__cause__, ssl.SSLCertVerificationError)
            self.assertNotIn(TEST_PASSWORD, str(failure.exception))
            gc.collect()
        unclosed = [str(item.message) for item in caught
                    if issubclass(item.category, ResourceWarning)
                    and 'unclosed' in str(item.message).lower()
                    and 'socket' in str(item.message).lower()]
        self.assertFalse(unclosed, 'capture TLS failure left an unclosed socket: {}'.format(unclosed))

    def _runtime(self):
        directory = tempfile.TemporaryDirectory(prefix='agv-sim-tls-')
        self.addCleanup(directory.cleanup)
        return SimRuntime(Path(directory.name) / 'sim.sqlite3', two_segment_map(),
                          {'sim-r1': {'T1': 'boot-1'}}, port=self.port,
                          client_id='sim-runtime-tls-test', connect_timeout=3)

    def test_authenticated_simulation_runtime_publishes_first_and_next_orders_over_tls(self):
        received = queue.Queue()
        subscribed = threading.Event()
        subscriber = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        subscriber.username_pw_set(TEST_USER, TEST_PASSWORD)
        subscriber.tls_set(ca_certs=str(self.cert))
        subscriber.on_connect = lambda client, userdata, flags, code, properties: (
            client.subscribe('agv/sim-r1/order', qos=1) if not code.is_failure else None)
        subscriber.on_subscribe = lambda client, userdata, mid, codes, properties: (
            subscribed.set() if codes and all(not code.is_failure for code in codes) else None)
        subscriber.on_message = lambda client, userdata, message: received.put(message)
        subscriber.connect('127.0.0.1', self.port, keepalive=15)
        subscriber.loop_start()
        self.addCleanup(subscriber.loop_stop)
        self.addCleanup(subscriber.disconnect)
        self.assertTrue(subscribed.wait(8), 'test subscriber was not ready')

        env = {'MQTT_USERNAME': TEST_USER, 'MQTT_PASSWORD': TEST_PASSWORD,
               'MQTT_TLS_CA': str(self.cert), 'PYTHON_DOTENV_DISABLED': '1'}
        with patch.dict(os.environ, env):
            runtime = self._runtime()
            runtime.start()
            self.addCleanup(runtime.stop)
            self.assertTrue(runtime.status()['ready'])
            runtime.create_task('T1')
            prepared = runtime.prepare('T1', 'N5',
                [{'robot_id': 'sim-r1', 'node': 'N1', 'available': True}], {'type': 'load'})
            self.assertEqual(prepared['released_until'], 'N3')
            self.assertEqual(runtime.publish('T1')['state'], 'acked')
            message = received.get(timeout=5)
            order = json.loads(message.payload)
            self.assertEqual(message.topic, 'agv/sim-r1/order')
            self.assertEqual(message.qos, 1)
            self.assertFalse(message.retain)
            self.assertEqual((order['order_id'], order['seq'], order['map_id'],
                              order['released_until']), ('T1', 1, 'integration@v1', 'N3'))
            self.assertEqual(order['nodes'], ['N1', 'N2', 'N3', 'N4', 'N5'])

            challenge = runtime.begin('T1', 'boot-1')['challenge']
            result = runtime.complete('T1', 'boot-1', challenge, 'sim-r1', 'T1', 1,
                                      'integration@v1', 'N3', _digest_json(order), True)
            self.assertTrue(result['reconciled'])
            map_data = two_segment_map()
            old = set(plan_segment(map_data, ['N1', 'N2', 'N3', 'N4', 'N5']).resources)
            new = set(plan_segment(map_data, ['N3', 'N4', 'N5']).resources)
            event = {'type': 'rear_clear', 'event_id': 'tls-clear-1',
                     'robot_session': 'boot-1', 'order_id': 'T1', 'seq': 1,
                     'map_id': 'integration@v1', 'stopped_at': 'N3',
                     'cleared_resources': [list(item) for item in sorted(old - new)]}
            published = subscriber.publish('agv/sim-r1/event', json.dumps(event), qos=1)
            published.wait_for_publish(timeout=5)
            self.assertTrue(published.is_published())
            message = received.get(timeout=8)
            next_order = json.loads(message.payload)
            self.assertEqual(message.topic, 'agv/sim-r1/order')
            self.assertEqual(message.qos, 1)
            self.assertFalse(message.retain)
            self.assertEqual((next_order['order_id'], next_order['seq'],
                              next_order['released_until']), ('T1', 2, 'N5'))
            self.assertEqual(set(runtime.store.list_occupancy()), new)

    def test_simulation_runtime_rejects_wrong_credentials(self):
        env = {'MQTT_USERNAME': TEST_USER, 'MQTT_PASSWORD': 'wrong-test-password',
               'MQTT_TLS_CA': str(self.cert), 'PYTHON_DOTENV_DISABLED': '1'}
        with patch.dict(os.environ, env):
            runtime = self._runtime()
            with self.assertRaises((RuntimeError, OSError, ssl.SSLError)):
                runtime.start()
            self.assertFalse(runtime.status()['ready'])

    def test_one_shot_publisher_rejects_wrong_credentials(self):
        env = {'MQTT_USERNAME': TEST_USER, 'MQTT_PASSWORD': 'wrong-test-password',
               'MQTT_TLS_CA': str(self.cert), 'PYTHON_DOTENV_DISABLED': '1'}
        with patch.dict(os.environ, env):
            runtime = self._runtime()
            with self.assertRaises((RuntimeError, OSError, ssl.SSLError)):
                client, _ = runtime._connect_publisher()
                runtime._close_client(client)


if __name__ == '__main__':
    unittest.main()
