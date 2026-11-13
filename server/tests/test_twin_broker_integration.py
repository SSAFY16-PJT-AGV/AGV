"""Opt-in loopback broker -> FastAPI -> real WebSocket integration test."""
import json
import os
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from urllib.request import urlopen

import paho.mqtt.client as mqtt
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus


BROKER = """
import asyncio, sys
from amqtt.broker import Broker
async def main():
    broker = Broker({'listeners': {'default': {'type': 'tcp', 'bind': '127.0.0.1:' + sys.argv[1]}},
                     'sys_interval': 0, 'auth': {'allow-anonymous': True}})
    await broker.start()
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


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'set AMQTT_TEST_PATH for isolated broker test')
class TwinBrokerIntegrationTest(unittest.TestCase):
    def test_live_mqtt_observation_reaches_real_websocket(self):
        broker_port, server_port = free_port(), free_port()
        while server_port == broker_port:
            server_port = free_port()
        broker_env = os.environ.copy()
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory() as temp, ExitStack() as cleanup:
            with open(os.devnull, 'w', encoding='utf-8') as broker_log, \
                 open(os.devnull, 'w', encoding='utf-8') as server_log:
                actual_python = getattr(sys, '_base_executable', sys.executable)
                broker = subprocess.Popen([actual_python, '-c', BROKER, str(broker_port)],
                    env=broker_env, stdout=broker_log, stderr=broker_log,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                cleanup.callback(stop, broker)
                deadline = time.monotonic() + 15
                while True:
                    self.assertIsNone(broker.poll(), 'isolated broker exited')
                    try:
                        with socket.create_connection(('127.0.0.1', broker_port), timeout=0.2):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail('isolated broker did not start')
                        time.sleep(0.1)
                server_env = os.environ.copy()
                for key in ('MQTT_USERNAME', 'MQTT_PASSWORD', 'MQTT_TLS_CA'):
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
                deadline = time.monotonic() + 15
                while True:
                    self.assertIsNone(server.poll(), 'local FastAPI server exited')
                    try:
                        with urlopen('http://127.0.0.1:{}/robots'.format(server_port), timeout=1) as response:
                            if json.load(response)['ready']:
                                break
                    except OSError:
                        pass
                    if time.monotonic() >= deadline:
                        self.fail('server did not subscribe to isolated broker')
                    time.sleep(0.1)
                connected = threading.Event()
                publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='isolated-test-publisher')
                publisher.on_connect = lambda client, userdata, flags, reason, properties: connected.set()
                publisher.connect('127.0.0.1', broker_port, keepalive=15)
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
                    with urlopen('http://127.0.0.1:{}/robots'.format(server_port), timeout=1) as response:
                        robots = json.load(response)['robots']
                    if robots.get('sim-r1', {}).get('state', {}).get('last_node') == 'N0':
                        break
                    if time.monotonic() >= deadline:
                        self.fail('server did not observe the pre-connection state')
                    time.sleep(0.1)
                with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port),
                             open_timeout=5) as twin:
                    def receive_node(connection, expected):
                        try:
                            message = connection.recv(timeout=5)
                        except TimeoutError:
                            with urlopen('http://127.0.0.1:{}/robots'.format(server_port), timeout=1) as response:
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
                                 origin='http://127.0.0.1', open_timeout=5):
                        pass
                with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port),
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
        broker_env = os.environ.copy()
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        server_env = os.environ.copy()
        for key in ('MQTT_USERNAME', 'MQTT_PASSWORD', 'MQTT_TLS_CA'):
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
        with open(os.devnull, 'w', encoding='utf-8') as broker_log, \
             open(os.devnull, 'w', encoding='utf-8') as server_log, ExitStack() as cleanup:
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port)],
                env=broker_env, stdout=broker_log, stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            deadline = time.monotonic() + 15
            while True:
                self.assertIsNone(broker.poll(), 'isolated broker exited')
                try:
                    with socket.create_connection(('127.0.0.1', broker_port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        self.fail('isolated broker did not start')
                    time.sleep(0.1)
            server = subprocess.Popen([python, '-m', 'uvicorn', 'server.main:app',
                '--host', '127.0.0.1', '--port', str(server_port), '--workers', '1',
                '--no-proxy-headers'], env=server_env, stdout=server_log, stderr=server_log,
                creationflags=flags)
            cleanup.callback(stop, server)
            deadline = time.monotonic() + 15
            while True:
                self.assertIsNone(server.poll(), 'local FastAPI server exited')
                try:
                    with urlopen('http://127.0.0.1:{}/robots'.format(server_port), timeout=1) as response:
                        if json.load(response)['ready']:
                            break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    self.fail('server did not subscribe to isolated broker')
                time.sleep(0.1)
            connected = threading.Event()
            publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='isolated-pose-test-publisher')
            publisher.on_connect = lambda client, userdata, flags, reason, properties: connected.set()
            publisher.connect('127.0.0.1', broker_port, keepalive=15)
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
