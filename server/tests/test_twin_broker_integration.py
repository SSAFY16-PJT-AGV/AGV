"""Opt-in loopback broker -> FastAPI -> real WebSocket integration test."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, build_opener

import paho.mqtt.client as mqtt
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus


LOCAL_HTTP = build_opener(ProxyHandler({}))


BROKER = """
import asyncio, os, sys
from pathlib import Path
from amqtt.broker import Broker
async def main():
    broker = Broker({'listeners': {'default': {'type': 'tcp', 'bind': '127.0.0.1:' + sys.argv[1]}},
                     'sys_interval': 0, 'auth': {'allow-anonymous': True}})
    await broker.start()
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding='ascii')
    await asyncio.Event().wait()
asyncio.run(main())
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def stop_publisher(client):
    client.disconnect()
    client.loop_stop()


def loopback_environment():
    """Keep isolated child processes away from inherited proxy settings."""
    env = {key: value for key, value in os.environ.items()
           if key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy',
                                  'mqtt_proxy', 'no_proxy')}
    env['NO_PROXY'] = '127.0.0.1,localhost'
    env['no_proxy'] = '127.0.0.1,localhost'
    return env


def connect_publisher(client, port):
    # Paho can read mqtt_proxy via PySocks even when HTTP proxies are disabled.
    # Force loopback bypass for this synchronous connection without altering
    # the environment for the remainder of the test.
    if mqtt.socks is not None:
        default_proxy = mqtt.socks.get_default_proxy()
        if default_proxy and default_proxy[0] is not None:
            raise RuntimeError('isolated test refuses a process-wide SOCKS proxy')
    with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost',
                                'no_proxy': '127.0.0.1,localhost'}):
        client.connect('127.0.0.1', port, keepalive=15)


def wait_for_owned_broker(test, process, ready_file):
    """Do not contact a listener until this exact broker has bound its port."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        test.assertIsNone(process.poll(), 'isolated broker exited before binding')
        try:
            if Path(ready_file).read_text(encoding='ascii') == str(process.pid):
                test.assertIsNone(process.poll(), 'isolated broker exited after binding')
                return
        except (OSError, UnicodeError):
            pass
        time.sleep(0.05)
    test.fail('isolated broker did not confirm that it bound the port')


def wait_for_owned_server(test, process, log_file, port):
    """Check Uvicorn's own startup record before contacting the selected port."""
    deadline = time.monotonic() + 15
    expected_pid = 'Started server process [{}]'.format(process.pid)
    expected_address = 'Uvicorn running on http://127.0.0.1:{}'.format(port)
    while time.monotonic() < deadline:
        test.assertIsNone(process.poll(), 'local FastAPI server exited before binding')
        try:
            text = Path(log_file).read_text(encoding='utf-8', errors='replace')
            if expected_pid in text and expected_address in text:
                test.assertIsNone(process.poll(), 'local FastAPI server exited after binding')
                return
        except OSError:
            pass
        time.sleep(0.05)
    test.fail('local FastAPI server did not confirm that it bound the port')


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'set AMQTT_TEST_PATH for isolated broker test')
class TwinBrokerIntegrationTest(unittest.TestCase):
    def test_process_wide_mqtt_proxy_is_rejected_before_connecting(self):
        default_proxy = SimpleNamespace(get_default_proxy=lambda: (2, '127.0.0.1', 9, True, None, None))
        publisher = Mock()
        with patch.object(mqtt, 'socks', default_proxy):
            with self.assertRaises(RuntimeError):
                connect_publisher(publisher, 12345)
        publisher.connect.assert_not_called()

    def test_other_listener_cannot_be_mistaken_for_test_broker(self):
        env = loopback_environment()
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other, \
             tempfile.TemporaryDirectory(prefix='twin-port-collision-') as temp, \
             open(os.devnull, 'w', encoding='utf-8') as broker_log:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                other.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            other.bind(('127.0.0.1', 0))
            other.listen()
            port = other.getsockname()[1]
            ready_file = os.path.join(temp, 'broker.ready')
            broker = subprocess.Popen([python, '-c', BROKER, str(port), ready_file],
                env=env, stdout=broker_log, stderr=broker_log, creationflags=flags)
            self.addCleanup(stop, broker)
            with self.assertRaises(AssertionError):
                wait_for_owned_broker(self, broker, ready_file)
            self.assertFalse(Path(ready_file).exists(), 'broker did not own the port')

    def test_two_mapped_robots_share_authenticated_websocket_over_isolated_mqtt(self):
        broker_port, server_port = free_port(), free_port()
        while server_port == broker_port:
            server_port = free_port()
        broker_env = loopback_environment()
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        server_env = loopback_environment()
        for key in ('MQTT_USERNAME', 'MQTT_PASSWORD', 'MQTT_TLS_CA', 'AGV_TWIN_ROBOT_MAP'):
            server_env.pop(key, None)
        server_env['PYTHON_DOTENV_DISABLED'] = '1'
        server_env['PYTHONPATH'] = os.pathsep.join([sysconfig.get_paths()['purelib'], os.getcwd()])
        server_env.update({'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port),
                           'AGV_SIM_CONFIG': '', 'AGV_TWIN_WEBSOCKET': '1',
                           'AGV_TWIN_ROBOT_ID': '',
                           'AGV_TWIN_ROBOT_MAP': '{"sim-r1":"AGV_01","sim-r2":"AGV_02"}',
                           'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1',
                           'AGV_TWIN_UNREAL_TOKEN': 'a' * 32})
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        broker = server = None
        with tempfile.TemporaryDirectory(prefix='twin-multi-broker-') as temp, \
             open(os.devnull, 'w', encoding='utf-8') as broker_log, \
             open(os.path.join(temp, 'server.log'), 'w', encoding='utf-8') as server_log, \
             ExitStack() as cleanup:
            ready_file = os.path.join(temp, 'broker.ready')
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), ready_file],
                env=broker_env, stdout=broker_log, stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_file)

            connected = threading.Event()
            publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                    client_id='isolated-multi-test-publisher',
                                    reconnect_on_failure=False)
            publisher.on_connect = lambda client, userdata, flags, code, properties: (
                connected.set() if not code.is_failure else None)
            connect_publisher(publisher, broker_port)
            publisher.loop_start()
            cleanup.callback(stop_publisher, publisher)
            self.assertTrue(connected.wait(5), 'test publisher did not connect')

            def state(x, y):
                return {'last_node': 'J01', 'status': 'MOVING', 'battery': 0.8,
                        'map_id': 'twin-validation-v1',
                        'pose': {'x': x, 'y': y, 'th': 0}}

            def send(robot, payload, retained=False):
                result = publisher.publish('agv/{}/state'.format(robot), json.dumps(payload),
                                           qos=1, retain=retained)
                result.wait_for_publish(timeout=5)
                self.assertTrue(result.is_published(), 'test publisher failed to send')

            # Ensure the server receives an old retained observation on subscribe.
            send('sim-r1', state(1000, 2000), retained=True)
            server = subprocess.Popen([python, '-m', 'uvicorn', 'server.main:app',
                '--host', '127.0.0.1', '--port', str(server_port), '--workers', '1',
                '--no-proxy-headers'], env=server_env, stdout=server_log, stderr=server_log,
                creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, os.path.join(temp, 'server.log'), server_port)
            deadline = time.monotonic() + 15
            while True:
                self.assertIsNone(server.poll(), 'local FastAPI server exited')
                try:
                    with LOCAL_HTTP.open('http://127.0.0.1:{}/robots'.format(server_port), timeout=1) as response:
                        snapshot = json.load(response)
                    robot = snapshot.get('robots', {}).get('sim-r1', {})
                    if snapshot.get('ready') and robot.get('state') == state(1000, 2000):
                        self.assertFalse(robot['state_received_live'])
                        break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    self.fail('server did not receive initial retained sample')
                time.sleep(0.1)

            with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port),
                         proxy=None,
                         origin='http://127.0.0.1',
                         additional_headers={'X-AGV-Twin-Token': 'a' * 32},
                         open_timeout=5) as twin:
                with self.assertRaises(TimeoutError):
                    twin.recv(timeout=0.4)
                send('sim-r3', state(9000, 2000))
                with self.assertRaises(TimeoutError):
                    twin.recv(timeout=0.4)
                send('sim-r1', state(1500, 2000))
                send('sim-r2', state(3500, 4000))
                received = [json.loads(twin.recv(timeout=5))['data'] for _ in range(2)]
                by_id = {item['agv_id']: item for item in received}
                self.assertEqual(set(by_id), {'AGV_01', 'AGV_02'})
                self.assertEqual((by_id['AGV_01']['x'], by_id['AGV_01']['y']), (1.5, 2.0))
                self.assertEqual((by_id['AGV_02']['x'], by_id['AGV_02']['y']), (3.5, 4.0))
                with self.assertRaises(TimeoutError):
                    twin.recv(timeout=0.4)
        self.assertIsNotNone(server.poll(), 'test server was not stopped')
        self.assertIsNotNone(broker.poll(), 'test broker was not stopped')

    def test_live_mqtt_observation_reaches_real_websocket(self):
        broker_port, server_port = free_port(), free_port()
        while server_port == broker_port:
            server_port = free_port()
        broker_env = loopback_environment()
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory() as temp, ExitStack() as cleanup:
            with open(os.devnull, 'w', encoding='utf-8') as broker_log, \
                 open(os.path.join(temp, 'server.log'), 'w', encoding='utf-8') as server_log:
                actual_python = getattr(sys, '_base_executable', sys.executable)
                ready_file = os.path.join(temp, 'broker.ready')
                broker = subprocess.Popen([actual_python, '-c', BROKER, str(broker_port), ready_file],
                    env=broker_env, stdout=broker_log, stderr=broker_log,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                cleanup.callback(stop, broker)
                wait_for_owned_broker(self, broker, ready_file)
                server_env = loopback_environment()
                for key in ('MQTT_USERNAME', 'MQTT_PASSWORD', 'MQTT_TLS_CA', 'AGV_TWIN_ROBOT_MAP'):
                    server_env.pop(key, None)
                server_env['PYTHON_DOTENV_DISABLED'] = '1'
                server_env['PYTHONPATH'] = os.pathsep.join(
                    [sysconfig.get_paths()['purelib'], os.getcwd()])
                server_env.update({'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port),
                                   'AGV_SIM_CONFIG': '', 'AGV_TWIN_WEBSOCKET': '1',
                                   'AGV_TWIN_ROBOT_ID': 'sim-r1',
                                   'AGV_TWIN_POSE_MAP_ID': '',
                                   'AGV_TWIN_UNREAL_TOKEN': 'a' * 32})
                server = subprocess.Popen([actual_python, '-m', 'uvicorn', 'server.main:app',
                    '--host', '127.0.0.1', '--port', str(server_port), '--workers', '1',
                    '--no-proxy-headers'], env=server_env, stdout=server_log, stderr=server_log,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                cleanup.callback(stop, server)
                wait_for_owned_server(self, server, os.path.join(temp, 'server.log'), server_port)
                deadline = time.monotonic() + 15
                while True:
                    self.assertIsNone(server.poll(), 'local FastAPI server exited')
                    try:
                        with LOCAL_HTTP.open('http://127.0.0.1:{}/robots'.format(server_port), timeout=1) as response:
                            if json.load(response)['ready']:
                                break
                    except OSError:
                        pass
                    if time.monotonic() >= deadline:
                        self.fail('server did not subscribe to isolated broker')
                    time.sleep(0.1)
                connected = threading.Event()
                publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                        client_id='isolated-test-publisher',
                                        reconnect_on_failure=False)
                publisher.on_connect = lambda client, userdata, flags, reason, properties: connected.set()
                connect_publisher(publisher, broker_port)
                publisher.loop_start()
                cleanup.callback(stop_publisher, publisher)
                self.assertTrue(connected.wait(5), 'test publisher did not connect')

                def send(topic, payload, retain=False):
                    result = publisher.publish(topic, json.dumps(payload), qos=0, retain=retain)
                    result.wait_for_publish(timeout=5)
                    self.assertTrue(result.is_published(), 'test publisher failed to send')

                state = {'last_node': 'N0', 'status': 'IDLE', 'battery': 0.8}
                send('agv/sim-r1/state', state, retain=True)
                deadline = time.monotonic() + 5
                while True:
                    with LOCAL_HTTP.open('http://127.0.0.1:{}/robots'.format(server_port), timeout=1) as response:
                        robots = json.load(response)['robots']
                    if robots.get('sim-r1', {}).get('state', {}).get('last_node') == 'N0':
                        break
                    if time.monotonic() >= deadline:
                        self.fail('server did not observe the pre-connection state')
                    time.sleep(0.1)
                with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port),
                             proxy=None,
                             open_timeout=5) as twin:
                    def receive_node(connection, expected):
                        try:
                            message = connection.recv(timeout=5)
                        except TimeoutError:
                            with LOCAL_HTTP.open('http://127.0.0.1:{}/robots'.format(server_port), timeout=1) as response:
                                observed = json.load(response)
                            self.fail('WebSocket timed out; MQTT observation: {}'.format(observed))
                        self.assertEqual(json.loads(message)['data']['current_node'], expected)
                    with self.assertRaises(TimeoutError):
                        twin.recv(timeout=0.4)
                    send('agv/sim-r2/state', state)
                    with self.assertRaises(TimeoutError):
                        twin.recv(timeout=0.4)
                    state['last_node'] = 'N1'
                    send('agv/sim-r1/state', state)
                    received = json.loads(twin.recv(timeout=5))
                    self.assertEqual(received, {'type': 'robot.state', 'data': {
                        'agv_id': 'sim-r1', 'current_node': 'N1', 'status': 'IDLE', 'battery': 0.8}})
                    with self.assertRaises(TimeoutError):
                        twin.recv(timeout=0.4)
                    state['last_node'] = 'N2'
                    send('agv/sim-r1/state', state)
                    receive_node(twin, 'N2')
                with self.assertRaises(InvalidStatus):
                    with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port),
                                 proxy=None,
                                 origin='http://127.0.0.1', open_timeout=5):
                        pass
                with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port),
                             proxy=None,
                             origin='http://127.0.0.1',
                             additional_headers={'X-AGV-Twin-Token': 'a' * 32},
                             open_timeout=5) as reconnected:
                    with self.assertRaises(TimeoutError):
                        reconnected.recv(timeout=0.4)
                    state['last_node'] = 'N3'
                    send('agv/sim-r1/state', state)
                    receive_node(reconnected, 'N3')

    def test_live_pose_is_converted_and_invalid_samples_are_not_forwarded(self):
        broker_port, server_port = free_port(), free_port()
        while server_port == broker_port:
            server_port = free_port()
        broker_env = loopback_environment()
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        server_env = loopback_environment()
        for key in ('MQTT_USERNAME', 'MQTT_PASSWORD', 'MQTT_TLS_CA', 'AGV_TWIN_ROBOT_MAP'):
            server_env.pop(key, None)
        server_env['PYTHON_DOTENV_DISABLED'] = '1'
        server_env['PYTHONPATH'] = os.pathsep.join(
            [sysconfig.get_paths()['purelib'], os.getcwd()])
        server_env.update({'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port),
                           'AGV_SIM_CONFIG': '', 'AGV_TWIN_WEBSOCKET': '1',
                           'AGV_TWIN_ROBOT_ID': 'sim-r1',
                           'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1',
                           'AGV_TWIN_UNREAL_TOKEN': 'a' * 32})
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='twin-pose-broker-') as temp, \
             open(os.devnull, 'w', encoding='utf-8') as broker_log, \
             open(os.path.join(temp, 'server.log'), 'w', encoding='utf-8') as server_log, \
             ExitStack() as cleanup:
            ready_file = os.path.join(temp, 'broker.ready')
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), ready_file],
                env=broker_env, stdout=broker_log, stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_file)
            server = subprocess.Popen([python, '-m', 'uvicorn', 'server.main:app',
                '--host', '127.0.0.1', '--port', str(server_port), '--workers', '1',
                '--no-proxy-headers'], env=server_env, stdout=server_log, stderr=server_log,
                creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, os.path.join(temp, 'server.log'), server_port)
            deadline = time.monotonic() + 15
            while True:
                self.assertIsNone(server.poll(), 'local FastAPI server exited')
                try:
                    with LOCAL_HTTP.open('http://127.0.0.1:{}/robots'.format(server_port), timeout=1) as response:
                        if json.load(response)['ready']:
                            break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    self.fail('server did not subscribe to isolated broker')
                time.sleep(0.1)
            connected = threading.Event()
            publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                    client_id='isolated-pose-test-publisher',
                                    reconnect_on_failure=False)
            publisher.on_connect = lambda client, userdata, flags, reason, properties: connected.set()
            connect_publisher(publisher, broker_port)
            publisher.loop_start()
            cleanup.callback(stop_publisher, publisher)
            self.assertTrue(connected.wait(5), 'test publisher did not connect')

            def send(state):
                result = publisher.publish('agv/sim-r1/state', json.dumps(state), qos=0, retain=False)
                result.wait_for_publish(timeout=5)
                self.assertTrue(result.is_published(), 'test publisher failed to send')

            state = {'last_node': 'J01', 'status': 'MOVING', 'battery': 0.8,
                     'map_id': 'twin-validation-v1', 'pose': {'x': 1500, 'y': 2000, 'th': 90}}
            with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port),
                         proxy=None,
                         origin='http://127.0.0.1',
                         additional_headers={'X-AGV-Twin-Token': 'a' * 32},
                         open_timeout=5) as twin:
                for pose, expected in [({'x': 1500, 'y': 2000, 'th': 90}, (1.5, 2.0, 90)),
                                       ({'x': 1550, 'y': 2050, 'th': -45}, (1.55, 2.05, -45))]:
                    state['pose'] = pose
                    send(state)
                    data = json.loads(twin.recv(timeout=5))['data']
                    self.assertEqual((data['x'], data['y'], data['yaw']), expected)
                    self.assertEqual(data['agv_id'], 'sim-r1')
                for invalid in (dict(state, map_id='wrong-map'),
                                dict(state, pose={'x': 1550, 'y': 2050})):
                    send(invalid)
                    with self.assertRaises(TimeoutError):
                        twin.recv(timeout=0.4)


if __name__ == '__main__':
    unittest.main()
