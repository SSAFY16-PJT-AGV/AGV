"""Isolated MQTT + server + simulated Unreal WebSocket product-order test.

The WebSocket client below is a test double; this is not an Unreal or physical
robot test.  It never publishes a robot order and does not use port 8000.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from urllib.request import ProxyHandler, Request, build_opener

from websockets.sync.client import connect

from server.tests.test_twin_broker_integration import (
    BROKER, free_port, stop, wait_for_owned_broker, wait_for_owned_server,
)
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]
HTTP = build_opener(ProxyHandler({}))
ORIGIN = 'http://127.0.0.1:8000'


def sample_map():
    names = ('J01', 'D01', 'D04', 'D06', 'D05')
    return {'map_id': 'test-product-map', 'coordinate_unit': 'meter',
            'nodes': [{'id': name, 'x': index * 0.4, 'y': 0,
                       'type': 'destination' if name.startswith('D') else 'junction'}
                      for index, name in enumerate(names)],
            'edges': [{'from': first, 'to': second, 'points': []}
                      for first, second in zip(names, names[1:])]}


def request(port, path, payload=None):
    headers = {'Host': '127.0.0.1:8000'}
    data = None
    if payload is not None:
        headers.update({'Origin': ORIGIN, 'Content-Type': 'application/json'})
        data = json.dumps(payload).encode('utf-8')
    req = Request('http://127.0.0.1:{}{}'.format(port, path), headers=headers, data=data)
    with HTTP.open(req, timeout=3) as response:
        return json.load(response)


def receive_action(ws, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = json.loads(ws.recv(timeout=min(1, max(0.01, deadline - time.monotonic()))))
        except TimeoutError:
            continue
        if message.get('type') == 'robot.action':
            return message['data']
    raise AssertionError('simulated Unreal did not receive an action')


def action_reply(action, *, applied=True, action_id=None):
    data = {key: action[key] for key in ('order_id', 'action_id', 'agv_id', 'action')}
    if action_id is not None:
        data['action_id'] = action_id
    data['applied'] = applied
    return json.dumps({'type': 'robot.action.result', 'data': data})


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class ProductOrderIntegrationTest(unittest.TestCase):
    def test_product_order_awaits_both_authenticated_unreal_results(self):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        token = secrets.token_urlsafe(32)
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='agv-product-e2e-') as folder, \
             open(os.devnull, 'w', encoding='utf-8') as broker_log, \
             open(os.path.join(folder, 'server.log'), 'w', encoding='utf-8') as server_log, \
             ExitStack() as cleanup:
            map_path = Path(folder) / 'warehouse_map.json'
            map_path.write_text(json.dumps(sample_map()), encoding='utf-8')
            ready_file = str(Path(folder) / 'broker.ready')
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), ready_file],
                                      cwd=ROOT, env=broker_env, stdout=broker_log,
                                      stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_file)

            env = isolated_environment(os.environ)
            for key in list(env):
                if key.upper().startswith('AGV_'):
                    del env[key]
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                        'AGV_ORDER_DEMO_ENABLED': '1', 'AGV_ORDER_DEMO_MAP': str(map_path),
                        'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port),
                        'AGV_TWIN_WEBSOCKET': '1',
                        'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}),
                        'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1',
                        'AGV_TWIN_UNREAL_TOKEN': token})
            server = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app',
                                       '--host', '127.0.0.1', '--port', str(server_port),
                                       '--workers', '1', '--no-proxy-headers'],
                                      cwd=ROOT, env=env, stdout=server_log,
                                      stderr=server_log, creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, str(Path(folder) / 'server.log'), server_port)

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.assertIsNone(server.poll(), 'demo server exited')
                if request(server_port, '/order-demo/api/status').get('connected'):
                    break
                time.sleep(0.1)
            else:
                self.fail('isolated MQTT observer is not ready')

            uri = 'ws://127.0.0.1:{}/ws/digital-twin'.format(server_port)
            with connect(uri, proxy=None, origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': token}) as simulated_unreal, \
                 connect(uri, proxy=None, open_timeout=5) as other_client:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if request(server_port, '/order-demo/api/status').get('unreal_connected'):
                        break
                    time.sleep(0.05)
                else:
                    self.fail('simulated Unreal session was not registered')

                products = request(server_port, '/order-demo/api/products')['products']
                self.assertIn('red', {p['id'] for p in products})
                accepted = request(server_port, '/order-demo/api/product-orders', {'product_id': 'red'})
                self.assertEqual(accepted['route'], ['J01', 'D01', 'D04'])
                self.assertEqual(accepted['product_type'], 'Red')
                grab = receive_action(simulated_unreal)
                self.assertEqual((grab['action'], grab['agv_id'], grab['product_type']),
                                 ('grab', 'AGV_01', 'Red'))
                waiting = request(server_port, '/order-demo/api/status')
                self.assertEqual((waiting['status'], waiting['phase'], waiting['current_node']),
                                 ('waiting', 'waiting_grab', 'D01'))
                other_client.send(action_reply(grab))
                simulated_unreal.send(action_reply(grab, action_id='invalid-action-id'))
                time.sleep(0.4)
                still_waiting = request(server_port, '/order-demo/api/status')
                self.assertEqual((still_waiting['status'], still_waiting['phase']),
                                 ('waiting', 'waiting_grab'))
                self.assertEqual(still_waiting['pose'], waiting['pose'])

                simulated_unreal.send(action_reply(grab))
                place = receive_action(simulated_unreal)
                self.assertEqual((place['action'], place['shelf_id'], place['slot_index']),
                                 ('place', 'SHELF_RED', 0))
                self.assertNotEqual(place['action_id'], grab['action_id'])
                waiting = request(server_port, '/order-demo/api/status')
                self.assertEqual((waiting['status'], waiting['phase'], waiting['current_node']),
                                 ('waiting', 'waiting_place', 'D04'))
                simulated_unreal.send(action_reply(place))
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    final = request(server_port, '/order-demo/api/status')
                    if final['status'] == 'completed':
                        break
                    time.sleep(0.05)
                self.assertEqual((final['status'], final['phase'], final['current_node']),
                                 ('completed', 'done', 'D04'))

        self.assertIsNotNone(server.poll(), 'demo test server was not stopped')
        self.assertIsNotNone(broker.poll(), 'demo test broker was not stopped')


if __name__ == '__main__':
    unittest.main()
