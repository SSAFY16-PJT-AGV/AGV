"""Opt-in end-to-end test against an isolated broker and real loopback sockets.

This uses a simulated WebSocket observer, not Unreal Editor or a physical AGV.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from websockets.sync.client import connect

from server.tests.test_twin_broker_integration import (
    BROKER, free_port, stop, wait_for_owned_broker, wait_for_owned_server,
)
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]
PORT = 8000
ORIGIN = 'http://127.0.0.1:8000'
HTTP = build_opener(ProxyHandler({}))


def request(path, payload=None, origin=None):
    headers = {}
    if origin is not None:
        headers['Origin'] = origin
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode('utf-8')
    req = Request(ORIGIN + path, data=data, headers=headers)
    with HTTP.open(req, timeout=3) as response:
        return json.load(response)


def sample_map():
    # Distance and topology are intentionally different from a direct shortcut;
    # all coordinates are Unreal-format metres, not the server's millimetres.
    nodes = [
        {'id': 'J01', 'type': 'junction', 'x': 0, 'y': 0},
        {'id': 'D01', 'type': 'destination', 'x': 0.6, 'y': 0},
        {'id': 'N01', 'type': 'access', 'x': 1.2, 'y': 0},
        {'id': 'D04', 'type': 'destination', 'x': 1.8, 'y': 0},
    ]
    edges = [
        {'from': 'J01', 'to': 'D01', 'type': 'main', 'points': []},
        {'from': 'D01', 'to': 'N01', 'type': 'main', 'points': []},
        {'from': 'N01', 'to': 'D04', 'type': 'main', 'points': []},
    ]
    return {'map_id': 'test', 'coordinate_unit': 'meter', 'nodes': nodes, 'edges': edges}


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class OrderDemoIntegrationTest(unittest.TestCase):
    def test_order_drives_computed_route_over_isolated_mqtt_and_websocket(self):
        # Do not contact a pre-existing server if its well-known Unreal port is in use.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', PORT))

        broker_port = free_port()
        while broker_port == PORT:
            broker_port = free_port()
        token = secrets.token_urlsafe(32)
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='agv-order-e2e-') as folder, \
             open(os.devnull, 'w', encoding='utf-8') as broker_log, \
             open(os.path.join(folder, 'server.log'), 'w', encoding='utf-8') as server_log, \
             ExitStack() as cleanup:
            map_path = Path(folder) / 'map.json'
            map_path.write_text(json.dumps(sample_map()), encoding='utf-8')
            ready_file = os.path.join(folder, 'broker.ready')
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), ready_file],
                                      cwd=ROOT, env=broker_env, stdout=broker_log,
                                      stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_file)

            env = isolated_environment(os.environ)
            # Prevent arbitrary settings inherited from a user's shell, including
            # settings added later to the separate demo app.
            for key in list(env):
                if key.upper().startswith('AGV_ORDER_DEMO_'):
                    del env[key]
            env.update({
                'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                'AGV_ORDER_DEMO_ENABLED': '1', 'AGV_ORDER_DEMO_MAP': str(map_path),
                'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port),
                'AGV_SIM_CONFIG': '', 'AGV_TWIN_WEBSOCKET': '1',
                'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}),
                'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1',
                'AGV_TWIN_UNREAL_TOKEN': token,
            })
            server = subprocess.Popen(
                [python, '-m', 'uvicorn', 'server.order_demo_app:app', '--host', '127.0.0.1',
                 '--port', str(PORT), '--workers', '1', '--no-proxy-headers'],
                cwd=ROOT, env=env, stdout=server_log, stderr=server_log, creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, os.path.join(folder, 'server.log'), PORT)

            deadline = time.monotonic() + 15
            while True:
                self.assertIsNone(server.poll(), 'isolated order server exited')
                if request('/order-demo/api/status').get('connected'):
                    break
                if time.monotonic() >= deadline:
                    self.fail('server MQTT subscription was not ready')
                time.sleep(0.1)

            nodes = request('/order-demo/api/nodes')['nodes']
            self.assertEqual({entry['id'] for entry in nodes}, {'J01', 'D01', 'N01', 'D04'})
            initial = request('/order-demo/api/status')
            self.assertEqual(initial['status'], 'idle')
            self.assertEqual(initial['current_node'], 'J01')
            self.assertEqual(initial['pose']['x'], 0)

            # Cross-site forms must not be able to start a demo order.
            with self.assertRaises(HTTPError) as blocked:
                request('/order-demo/api/orders', {'pickup_node': 'D01', 'destination_node': 'D04'},
                        origin='http://attacker.invalid')
            self.assertIn(blocked.exception.code, (401, 403))
            self.assertEqual(request('/order-demo/api/status')['status'], 'idle')

            with connect('ws://127.0.0.1:8000/ws/digital-twin', proxy=None,
                         origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': token}) as ws:
                before = json.loads(ws.recv(timeout=10))
                self.assertEqual(before['type'], 'robot.state')
                self.assertEqual(before['data']['agv_id'], 'AGV_01')
                self.assertAlmostEqual(before['data']['x'], 0, places=3)

                accepted = request('/order-demo/api/orders',
                                   {'pickup_node': 'D01', 'destination_node': 'D04'}, origin=ORIGIN)
                self.assertEqual(accepted['status'], 'moving')
                self.assertEqual(accepted['phase'], 'to_pickup')
                self.assertEqual(accepted['route'], ['J01', 'D01', 'N01', 'D04'])

                positions = []
                snapshots = [accepted]
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    self.assertIsNone(server.poll(), 'isolated order server exited')
                    try:
                        message = json.loads(ws.recv(timeout=0.5))
                    except TimeoutError:
                        message = None
                    if message and message.get('type') == 'robot.state':
                        state = message['data']
                        if state.get('agv_id') == 'AGV_01' and 'x' in state:
                            positions.append((state['x'], state['y']))
                    snapshot = request('/order-demo/api/status')
                    snapshots.append(snapshot)
                    if snapshot['status'] == 'completed' and positions and positions[-1][0] >= 1.79:
                        break

            self.assertEqual(snapshots[-1]['status'], 'completed')
            self.assertEqual(snapshots[-1]['phase'], 'done')
            self.assertEqual(snapshots[-1]['current_node'], 'D04')
            self.assertAlmostEqual(snapshots[-1]['pose']['x'], 1800, delta=1)
            self.assertTrue(any(s['phase'] == 'to_destination' for s in snapshots))
            self.assertGreaterEqual(len({round(x, 2) for x, _ in positions}), 3,
                                    'must deliver independently changing intermediate poses')
            self.assertTrue(any(0.05 < x < 1.75 for x, _ in positions))
            self.assertTrue(all(abs(y) < 0.01 for _, y in positions))


if __name__ == '__main__':
    unittest.main()
