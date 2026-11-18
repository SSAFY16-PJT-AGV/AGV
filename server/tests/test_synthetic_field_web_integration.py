"""Team-map copy + internal synthetic world; never a physical robot test."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import queue
import secrets
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest
from urllib.request import Request

import paho.mqtt.client as mqtt

from server.tests.test_field_web_unreal_integration import fixtures
from server.tests.test_order_persistent_restart_integration import wait_for
from server.tests.test_order_product_integration import HTTP, request
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port,
    stop, stop_publisher, wait_for_owned_broker, wait_for_owned_server)
from server.tools.twin_multi_test import isolated_environment

ROOT = Path(__file__).resolve().parents[2]


def renamed_fixture(folder, *, repeat=False):
    """Use different field and world IDs so identity mapping cannot hide defects."""
    unreal_path, config_path, _products, _positions = fixtures(folder, None, repeat=repeat)
    unreal = json.loads(unreal_path.read_text(encoding='utf-8'))
    config = json.loads(config_path.read_text(encoding='utf-8'))
    for node in unreal['nodes']:
        node['id'] = 'U_' + node['id']
    for edge in unreal['edges']:
        edge['from'] = 'U_' + edge['from']
        edge['to'] = 'U_' + edge['to']
    for anchor in config['anchors']:
        anchor['unreal_node'] = 'U_' + anchor['unreal_node']
    for product in config['products']:
        product['unreal_pickup_node'] = 'U_' + product['unreal_pickup_node']
        product['unreal_destination_node'] = 'U_' + product['unreal_destination_node']
    unreal_path.write_text(json.dumps(unreal, ensure_ascii=False), encoding='utf-8')
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    return unreal_path, config_path, config


def submit(port, product):
    req = Request('http://127.0.0.1:{}/order-demo/api/product-orders'.format(port),
                  headers={'Host': '127.0.0.1:8000', 'Origin': 'http://127.0.0.1:8000',
                           'Content-Type': 'application/json', 'Idempotency-Key': 'field-synthetic-' + product},
                  data=json.dumps({'product_id': product}).encode('utf-8'))
    with HTTP.open(req, timeout=3) as response:
        result = json.load(response)
        if response.status not in (200, 202) or not isinstance(result.get('request_id'), str):
            raise AssertionError('unexpected order admission')
        return result['request_id']


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class SyntheticFieldWebIntegration(unittest.TestCase):
    def test_two_products_use_distinct_robots_and_translated_sources(self):
        self._exercise(repeat=False, crash=False)

    def test_repeated_visit_occurrence_executes_through_real_mqtt(self):
        self._exercise(repeat=True, crash=False)

    def test_lost_grab_ack_blocks_restart_and_retains_allocation(self):
        self._exercise(repeat=False, crash='grab')

    def test_lost_place_ack_blocks_restart_and_retains_allocation(self):
        self._exercise(repeat=False, crash='place')

    def _exercise(self, *, repeat, crash):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='agv-synthetic-field-') as temp, ExitStack() as cleanup:
            folder = Path(temp)
            world, config, mapping = renamed_fixture(folder, repeat=repeat)
            for product in mapping['products']:
                self.assertNotEqual(product['pickup_node'], product['unreal_pickup_node'])
                self.assertNotEqual(product['destination_node'], product['unreal_destination_node'])
            durable, marker = folder / 'new-state', folder / 'world-effect'
            broker_log = cleanup.enter_context((folder / 'broker.log').open('w', encoding='utf-8'))
            server_log = cleanup.enter_context((folder / 'server.log').open('w', encoding='utf-8'))
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), str(folder / 'ready')], cwd=ROOT,
                                     env=broker_env, stdout=broker_log, stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(folder / 'ready'))
            captured, subscribed = queue.Queue(), threading.Event()
            observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id='synthetic-field-test-' + secrets.token_hex(6),
                                   protocol=mqtt.MQTTv311, reconnect_on_failure=False)
            observer.on_connect = lambda c, _u, _f, _r, _p: c.subscribe('agv/+/order', qos=1)
            observer.on_subscribe = lambda _c, _u, _m, _r, _p: subscribed.set()
            observer.on_message = lambda _c, _u, msg: captured.put((msg.topic, json.loads(msg.payload)))
            connect_publisher(observer, broker_port)
            observer.loop_start()
            cleanup.callback(stop_publisher, observer)
            self.assertTrue(subscribed.wait(10))
            env = isolated_environment({k: v for k, v in os.environ.items() if not k.upper().startswith('AGV_')})
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                        'TEMP': temp, 'TMP': temp, 'AGV_ORDER_PERSIST_DIR': str(durable),
                        'AGV_ORDER_ACTION_SOURCE': 'synthetic', 'AGV_ORDER_DEMO_ENABLED': '1',
                        'AGV_ORDER_COORDINATED': '1', 'AGV_ORDER_FLEET': '1', 'AGV_ORDER_SHELF_SYNC': '1',
                        'AGV_ORDER_DEMO_MAP': str(world), 'AGV_ORDER_FIELD_CONFIG': str(config),
                        'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port),
                        'TEST_EFFECT_MARKER': str(marker), 'TEST_EFFECT_ACTION': crash if crash else '',
                        'TEST_HTTP_PORT': str(server_port)})
            if crash:
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
                command = [python, '-c', script]
            else:
                # Diagnostics only in this test-owned child; never change an
                # action or create a product endpoint for test controls.
                script = '''\
import os, time, uvicorn
from server.synthetic_peer import SyntheticPeer
original = SyntheticPeer._apply
def observed(self, action):
    try:
        return original(self, action)
    except Exception:
        twin = action.get('agv_id')
        robot = 'sim-r1' if twin == 'AGV_01' else 'sim-r2'
        latest = self.observer.twin_observation(robot)
        print('TEST_ACTION_DIAGNOSTIC', repr(action), repr(self._robots.get(twin)),
              repr(time.monotonic() - self._pose_at.get(twin, 0)), repr(latest), flush=True)
        raise
SyntheticPeer._apply = observed
uvicorn.run('server.order_demo_app:app', host='127.0.0.1', port=int(os.environ['TEST_HTTP_PORT']),
            workers=1, proxy_headers=False, access_log=False)
'''
                command = [python, '-c', script]
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=server_log, stderr=server_log,
                                      creationflags=flags)
            cleanup.callback(stop, server)
            try:
                wait_for_owned_server(self, server, str(folder / 'server.log'), server_port)
            except AssertionError as exc:
                server_log.flush()
                raise AssertionError(str(exc) + '\n' + (folder / 'server.log').read_text(
                    encoding='utf-8', errors='replace')) from exc
            def diagnostic():
                server_log.flush()
                return (folder / 'server.log').read_text(encoding='utf-8', errors='replace')[-12000:]
            self.assertTrue(wait_for(lambda: request(server_port, '/order-demo/api/status').get('accepting_orders')),
                            diagnostic())
            product_ids = ['red'] if repeat or crash else ['red', 'blue']
            with ThreadPoolExecutor(max_workers=len(product_ids)) as pool:
                requests = list(pool.map(lambda product: submit(server_port, product), product_ids))
            if crash:
                self.assertTrue(wait_for(marker.exists, timeout=120), diagnostic())
                server.kill()
                server.wait(timeout=10)
                with closing(sqlite3.connect(durable / 'pickup-inventory.sqlite3')) as db:
                    self.assertEqual(db.execute("SELECT available FROM counts WHERE source_id='P_orange'").fetchone(),
                                     (3 if crash == 'grab' else 2,))
                    self.assertEqual(db.execute('SELECT state FROM reservations').fetchall(),
                                     [('inflight' if crash == 'grab' else 'consumed',)])
                with closing(sqlite3.connect(durable / 'orders.sqlite3')) as db:
                    self.assertEqual(db.execute('SELECT state FROM orders').fetchall(), [('active',)])
                    self.assertEqual(db.execute('SELECT state FROM slot_reservations').fetchall(), [('reserved',)])
                with closing(sqlite3.connect(durable / 'simulation.sqlite3')) as db:
                    self.assertGreater(db.execute('SELECT COUNT(*) FROM allocation_occupancy').fetchone()[0], 0)
                again = subprocess.Popen(command, cwd=ROOT, env=env, stdout=server_log, stderr=server_log,
                                         creationflags=flags)
                cleanup.callback(stop, again)
                self.assertNotEqual(again.wait(timeout=15), 0)
                self.assertIn('FileExistsError', diagnostic())
                return
            def terminal(request_id):
                value = request(server_port, '/order-demo/api/orders/' + request_id)
                return value if value.get('status') in ('completed', 'failed', 'unknown') else None
            results = [wait_for(lambda: terminal(rid), timeout=150) for rid in requests]
            self.assertTrue(all(row is not None and row['status'] == 'completed' for row in results),
                            'orders={!r}\n{}'.format(results, diagnostic()))
            self.assertEqual({row['assigned_robot'] for row in results}, {'sim-r1'} if repeat else {'sim-r1', 'sim-r2'})
            orders = []
            while not captured.empty():
                orders.append(captured.get_nowait())
            self.assertTrue(orders)
            self.assertTrue(all(data['map_id'] == 'mat_ssafy_v2@v0' for _topic, data in orders))
            if repeat:
                pickup = [data for _topic, data in orders if data['order_id'].endswith('pickup')]
                self.assertTrue(any(len(data['nodes']) != len(set(data['nodes'])) for data in pickup))
                self.assertGreaterEqual(sum(data['released_until'] == 'M3' for data in pickup), 2)
            with closing(sqlite3.connect(durable / 'pickup-inventory.sqlite3')) as db:
                for product in mapping['products']:
                    self.assertEqual(db.execute('SELECT available FROM counts WHERE source_id=?',
                                                (product['pickup_node'],)).fetchone(), (2,))
                self.assertEqual(db.execute('SELECT state FROM reservations').fetchall(), [('consumed',)] * len(results))
            with closing(sqlite3.connect(durable / 'orders.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT state FROM slot_reservations').fetchall(),
                                 [('confirmed',)] * len(results))

