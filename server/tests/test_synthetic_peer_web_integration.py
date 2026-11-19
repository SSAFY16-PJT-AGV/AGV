"""Isolated HTTP/MQTT synthetic world integration; never uses physical hardware."""
from contextlib import ExitStack, closing
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from urllib.request import Request

from websockets.sync.client import connect

from server.tests.test_order_fleet_integration import scenario_map
from server.tests.test_order_multi_slot_integration import post_order
from server.tests.test_order_persistent_restart_integration import wait_for
from server.tests.test_order_product_integration import request
from server.tests.test_order_product_integration import HTTP
from server.tests.test_twin_broker_integration import (BROKER, free_port, stop,
    wait_for_owned_broker, wait_for_owned_server)
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]


def post_blue(port):
    req = Request('http://127.0.0.1:{}/order-demo/api/product-orders'.format(port),
                  headers={'Host': '127.0.0.1:8000', 'Origin': 'http://127.0.0.1:8000',
                           'Content-Type': 'application/json', 'Idempotency-Key': 'synthetic-blue'},
                  data=json.dumps({'product_id': 'blue'}).encode('utf-8'))
    with HTTP.open(req, timeout=3) as response:
        return response.status, json.load(response)


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class SyntheticPeerWebIntegration(unittest.TestCase):
    def test_http_order_real_mqtt_synthetic_actions_and_restart_rejection(self):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        token = secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory(prefix='agv-synthetic-web-') as temp, ExitStack() as cleanup:
            folder = Path(temp)
            broker_log = cleanup.enter_context((folder / 'broker.log').open('w', encoding='utf-8'))
            server_log = cleanup.enter_context((folder / 'server.log').open('w', encoding='utf-8'))
            map_path, durable = folder / 'map.json', folder / 'new-world'
            map_path.write_text(json.dumps(scenario_map(False)), encoding='utf-8')
            benv = isolated_environment(os.environ)
            benv['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), str(folder / 'ready')],
                                      cwd=ROOT, env=benv, stdout=broker_log, stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(folder / 'ready'))
            env = isolated_environment(os.environ)
            for key in list(env):
                if key.upper().startswith('AGV_'):
                    del env[key]
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                        'TEMP': str(folder), 'TMP': str(folder), 'AGV_ORDER_PERSIST_DIR': str(durable),
                        'AGV_ORDER_ACTION_SOURCE': 'synthetic', 'AGV_ORDER_DEMO_ENABLED': '1',
                        'AGV_ORDER_COORDINATED': '1', 'AGV_ORDER_FLEET': '1', 'AGV_ORDER_SHELF_SYNC': '1',
                        'AGV_ORDER_DEMO_MAP': str(map_path), 'MQTT_HOST': '127.0.0.1',
                        'MQTT_PORT': str(broker_port), 'AGV_TWIN_WEBSOCKET': '1',
                        'AGV_TWIN_UNREAL_TOKEN': token,
                        'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}),
                        'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1'})
            command = [python, '-m', 'uvicorn', 'server.order_demo_app:app', '--host', '127.0.0.1',
                       '--port', str(server_port), '--workers', '1', '--no-proxy-headers', '--no-access-log']
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=server_log, stderr=server_log,
                                      creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, str(folder / 'server.log'), server_port)
            def diagnostic():
                server_log.flush()
                return (folder / 'server.log').read_text(encoding='utf-8', errors='replace')[-10000:]
            self.assertTrue(wait_for(lambda: request(server_port, '/order-demo/api/status').get('accepting_orders')),
                            diagnostic())
            self.assertEqual(request(server_port, '/order-demo/api/status')['action_source'], 'synthetic')
            map_view = request(server_port, '/order-demo/api/map')
            self.assertEqual(map_view['coordinate_unit'], 'mm')
            self.assertEqual({item['robot_id'] for item in map_view['occupancy']}, {'sim-r1', 'sim-r2'})
            # An authenticated UE-style socket can observe but cannot attach
            # as the synthetic action owner or inject a fabricated result.
            with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port), proxy=None,
                         origin='http://127.0.0.1', additional_headers={'X-AGV-Twin-Token': token}) as ws:
                ws.send(json.dumps({'type': 'robot.action.result', 'data': {'order_id': 'wrong',
                                   'action_id': 'wrong', 'agv_id': 'AGV_01', 'action': 'grab', 'applied': True}}))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(post_order, server_port, 'synthetic-red'),
                               pool.submit(post_blue, server_port)]
                    submissions = [future.result() for future in futures]
                self.assertTrue(all(status in (200, 202) for status, _ in submissions))
                request_ids = [submitted['request_id'] for _, submitted in submissions]
                def active_map():
                    view = request(server_port, '/order-demo/api/map')
                    return view if any(item['kind'] == 'edge' for item in view['occupancy']) else None
                self.assertIsNotNone(wait_for(active_map, timeout=15), diagnostic())
                def terminal(request_id):
                    row = request(server_port, '/order-demo/api/orders/' + request_id)
                    return row if row.get('status') in ('completed', 'unknown', 'failed') else None
                results = [wait_for(lambda: terminal(request_id), timeout=120) for request_id in request_ids]
                self.assertTrue(all(result is not None and result['status'] == 'completed' for result in results),
                                diagnostic())
                self.assertEqual({result['assigned_robot'] for result in results}, {'sim-r1', 'sim-r2'})
                map_view = request(server_port, '/order-demo/api/map')
                self.assertEqual({item['robot_id'] for item in map_view['occupancy']}, {'sim-r1', 'sim-r2'})
            with closing(sqlite3.connect(durable / 'pickup-inventory.sqlite3')) as db:
                self.assertEqual(db.execute("SELECT available FROM counts WHERE source_id='D01'").fetchone(), (2,))
                self.assertEqual(db.execute("SELECT available FROM counts WHERE source_id='D06'").fetchone(), (2,))
                self.assertEqual(db.execute('SELECT state FROM reservations').fetchall(), [('consumed',)] * 2)
            with closing(sqlite3.connect(durable / 'orders.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT state FROM slot_reservations').fetchall(), [('confirmed',)] * 2)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone(), (3,))
            stop(server)
            again = subprocess.Popen(command, cwd=ROOT, env=env, stdout=server_log, stderr=server_log,
                                     creationflags=flags)
            cleanup.callback(stop, again)
            self.assertNotEqual(again.wait(timeout=15), 0)
            self.assertIn('FileExistsError', diagnostic())

    def test_lost_grab_and_place_result_preserve_unknown_state(self):
        for action in ('grab', 'place'):
            with self.subTest(action=action):
                self._kill_after_synthetic_effect(action)

    def _kill_after_synthetic_effect(self, target):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='agv-synthetic-crash-') as temp, ExitStack() as cleanup:
            folder = Path(temp)
            broker_log = cleanup.enter_context((folder / 'broker.log').open('w', encoding='utf-8'))
            server_log = cleanup.enter_context((folder / 'server.log').open('w', encoding='utf-8'))
            map_path, durable = folder / 'map.json', folder / 'world'
            marker = folder / 'effect-observed'
            map_path.write_text(json.dumps(scenario_map(False)), encoding='utf-8')
            benv = isolated_environment(os.environ)
            benv['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), str(folder / 'ready')],
                                      cwd=ROOT, env=benv, stdout=broker_log, stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(folder / 'ready'))
            env = isolated_environment(os.environ)
            for key in list(env):
                if key.upper().startswith('AGV_'):
                    del env[key]
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                        'TEMP': str(folder), 'TMP': str(folder), 'AGV_ORDER_PERSIST_DIR': str(durable),
                        'AGV_ORDER_ACTION_SOURCE': 'synthetic', 'AGV_ORDER_DEMO_ENABLED': '1',
                        'AGV_ORDER_COORDINATED': '1', 'AGV_ORDER_FLEET': '1', 'AGV_ORDER_SHELF_SYNC': '1',
                        'AGV_ORDER_DEMO_MAP': str(map_path), 'MQTT_HOST': '127.0.0.1',
                        'MQTT_PORT': str(broker_port), 'TEST_EFFECT_MARKER': str(marker),
                        'TEST_EFFECT_ACTION': target, 'TEST_HTTP_PORT': str(server_port)})
            # The barrier is test-owned child code, not an app endpoint. It
            # freezes after the in-memory world changed and before receive().
            script = '''\
import os, pathlib, threading, uvicorn
from server.synthetic_peer import SyntheticPeer
original = SyntheticPeer._apply
def intercept(self, action):
    result = original(self, action)
    if action['action'] == os.environ['TEST_EFFECT_ACTION']:
        with pathlib.Path(os.environ['TEST_EFFECT_MARKER']).open('x') as marker:
            marker.write(action['action_id'])
            marker.flush()
            os.fsync(marker.fileno())
        threading.Event().wait()
    return result
SyntheticPeer._apply = intercept
uvicorn.run('server.order_demo_app:app', host='127.0.0.1', port=int(os.environ['TEST_HTTP_PORT']),
            workers=1, proxy_headers=False, access_log=False)
'''
            server = subprocess.Popen([python, '-c', script], cwd=ROOT, env=env, stdout=server_log,
                                      stderr=server_log, creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, str(folder / 'server.log'), server_port)
            def diagnostic():
                server_log.flush()
                return (folder / 'server.log').read_text(encoding='utf-8', errors='replace')[-10000:]
            self.assertTrue(wait_for(lambda: request(server_port, '/order-demo/api/status').get('accepting_orders')),
                            diagnostic())
            code, value = post_order(server_port, 'crash-' + target)
            self.assertIn(code, (200, 202))
            self.assertTrue(wait_for(marker.exists, timeout=120), diagnostic())
            server.kill()
            server.wait(timeout=10)
            with closing(sqlite3.connect(durable / 'pickup-inventory.sqlite3')) as db:
                self.assertEqual(db.execute("SELECT available FROM counts WHERE source_id='D01'").fetchone(),
                                 (3 if target == 'grab' else 2,))
                self.assertEqual(db.execute('SELECT state FROM reservations').fetchall(),
                                 [('inflight' if target == 'grab' else 'consumed',)])
            with closing(sqlite3.connect(durable / 'orders.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT state FROM orders').fetchall(), [('active',)])
                self.assertEqual(db.execute('SELECT state FROM slot_reservations').fetchall(), [('reserved',)])
            with closing(sqlite3.connect(durable / 'simulation.sqlite3')) as db:
                self.assertGreater(db.execute('SELECT COUNT(*) FROM allocation_occupancy').fetchone()[0], 0)
            again = subprocess.Popen([python, '-c', script], cwd=ROOT, env=env, stdout=server_log,
                                     stderr=server_log, creationflags=flags)
            cleanup.callback(stop, again)
            self.assertNotEqual(again.wait(timeout=15), 0)
            self.assertIn('FileExistsError', diagnostic())

