"""Web orders on the team-map copy through MQTT and an Unreal WebSocket double.

The map's extra waiting nodes, products, world coordinates and ACKs are test
fixtures. They are not physical locations or safety evidence.
"""
from contextlib import ExitStack, closing
import copy
import hashlib
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

import paho.mqtt.client as mqtt
from websockets.sync.client import connect

from server.tests.test_order_product_integration import request, action_reply
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port,
    stop, stop_publisher, wait_for_owned_broker, wait_for_owned_server)
from server.tools.twin_multi_test import isolated_environment

ROOT = Path(__file__).resolve().parents[2]


def fixtures(folder, source, *, repeat=False):
    data = json.loads((source / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
    data = copy.deepcopy(data)
    # Exclusively synthetic waiting points for this isolated test.
    for node in ('P_blue', 'P_orange', 'P_purple', 'P_green', 'P_yellow', 'P_red', 'M3', 'M1'):
        if repeat and node == 'P_red':
            continue  # Exercise M3 -> P_red -> M3 within one authorized segment.
        data['zones'].append({'id': 'test-wait-' + node, 'type': 'waiting', 'node': node})
    field_path = folder / 'field.json'
    field_path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
    # A made-up world with an exact offset from the field's mm coordinates.
    unreal = {'map_id': 'field-test-world', 'coordinate_unit': 'meter',
              'nodes': [{'id': n['id'], 'x': n['pos'][0] / 1000 + 3,
                         'y': n['pos'][1] / 1000 + 4} for n in data['nodes']],
              'edges': [{'from': e['from'], 'to': e['to'], 'points': []} for e in data['edges']]}
    unreal_path = folder / 'unreal.json'
    unreal_path.write_text(json.dumps(unreal, ensure_ascii=False), encoding='utf-8')
    products = [
        {'id': 'red', 'label': 'red', 'product_type': 'Red', 'pickup_node': 'P_orange',
         'destination_node': 'P_purple', 'unreal_pickup_node': 'P_orange',
         'unreal_destination_node': 'P_purple', 'shelf_id': 'SHELF_RED', 'slot_index': 0, 'slot_count': 6},
        {'id': 'blue', 'label': 'blue', 'product_type': 'Blue', 'pickup_node': 'P_yellow',
         'destination_node': 'P_red', 'unreal_pickup_node': 'P_yellow',
         'unreal_destination_node': 'P_red', 'shelf_id': 'SHELF_BLUE', 'slot_index': 0, 'slot_count': 6},
    ]
    config = {'field_map_file': field_path.name, 'field_map_sha256': digest,
              'unreal_map_id': unreal['map_id'],
              'anchors': [{'field_node': 'P_orange', 'unreal_node': 'P_orange'},
                          {'field_node': 'P_purple', 'unreal_node': 'P_purple'}],
              'reflect_y': False, 'tolerance_m': .001,
              'robots': {'sim-r1': {'node': 'P_blue', 'incoming_edge': 'E12'},
                         'sim-r2': {'node': 'P_green', 'incoming_edge': 'E6'}},
              'products': products}
    if repeat:
        config['robots'] = {'sim-r1': {'node': 'X1', 'incoming_edge': 'E9'},
                            'sim-r2': {'node': 'P_orange', 'incoming_edge': 'E13'}}
        config['products'] = [dict(products[0], pickup_node='P_yellow', destination_node='P_green',
                                   unreal_pickup_node='P_yellow', unreal_destination_node='P_green')]
        products = config['products']
    config_path = folder / 'field-config.json'
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    positions = {node['id']: (node['x'], node['y']) for node in unreal['nodes']}
    return unreal_path, config_path, products, positions


@unittest.skipUnless(os.getenv('AMQTT_TEST_PATH') and os.getenv('AGV_FIELD_REPO'),
                     'requires aMQTT and a copy of the team field-run source')
class FieldWebUnrealIntegration(unittest.TestCase):
    def test_two_web_orders_use_team_map_and_transformed_unreal_actions(self):
        self._exercise(False)

    def test_web_order_uses_revisited_node_within_one_team_map_segment(self):
        self._exercise(True)

    def _exercise(self, repeat):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        token, python = secrets.token_urlsafe(32), getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='field-web-unreal-') as temp, ExitStack() as cleanup:
            folder = Path(temp)
            unreal_map, config, products, positions = fixtures(folder, Path(os.environ['AGV_FIELD_REPO']), repeat=repeat)
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker_log = cleanup.enter_context(open(os.devnull, 'w'))
            server_log = cleanup.enter_context((folder / 'server.log').open('w', encoding='utf-8'))
            ready = folder / 'broker.ready'
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), str(ready)], env=broker_env,
                cwd=ROOT, stdout=broker_log, stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(ready))
            messages, subscribed = queue.Queue(), threading.Event()
            observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                client_id='field-web-observer-' + secrets.token_hex(5), protocol=mqtt.MQTTv311,
                reconnect_on_failure=False)
            observer.on_connect = lambda client, _u, _f, _r, _p: client.subscribe('agv/+/order', qos=1)
            observer.on_subscribe = lambda _c, _u, _m, _r, _p: subscribed.set()
            observer.on_message = lambda _c, _u, msg: messages.put(json.loads(msg.payload))
            connect_publisher(observer, broker_port)
            observer.loop_start()
            cleanup.callback(stop_publisher, observer)
            self.assertTrue(subscribed.wait(10))
            env = isolated_environment({k: v for k, v in os.environ.items() if not k.upper().startswith('AGV_')})
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                'TEMP': temp, 'TMP': temp, 'AGV_ORDER_DEMO_ENABLED': '1', 'AGV_ORDER_COORDINATED': '1',
                'AGV_ORDER_FLEET': '1', 'AGV_ORDER_DEMO_MAP': str(unreal_map),
                'AGV_ORDER_FIELD_CONFIG': str(config), 'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port),
                'AGV_TWIN_WEBSOCKET': '1', 'AGV_TWIN_UNREAL_TOKEN': token,
                'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}),
                'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1'})
            server = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app', '--host',
                '127.0.0.1', '--port', str(server_port), '--workers', '1', '--no-proxy-headers'],
                cwd=ROOT, env=env, stdout=server_log, stderr=server_log, creationflags=flags)
            cleanup.callback(stop, server)
            try:
                wait_for_owned_server(self, server, str(folder / 'server.log'), server_port)
            except AssertionError as exc:
                server_log.flush()
                raise AssertionError(str(exc) + '\n' + (folder / 'server.log').read_text(
                    encoding='utf-8', errors='replace')) from exc
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not request(server_port, '/order-demo/api/status').get('connected'):
                time.sleep(.05)
            with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port), proxy=None,
                         origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': token}) as twin:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not request(server_port, '/order-demo/api/status').get('unreal_connected'):
                    time.sleep(.05)
                red = request(server_port, '/order-demo/api/product-orders', {'product_id': 'red'})
                assignments = {red['assigned_robot']: products[0]}
                if not repeat:
                    blue = request(server_port, '/order-demo/api/product-orders', {'product_id': 'blue'})
                    assignments[blue['assigned_robot']] = products[1]
                    self.assertEqual(set(assignments), {'sim-r1', 'sim-r2'})
                else:
                    self.assertEqual(set(assignments), {'sim-r1'})
                by_twin = {('AGV_01' if robot == 'sim-r1' else 'AGV_02'): item
                           for robot, item in assignments.items()}
                actions, states, orders = [], [], []
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    while not messages.empty():
                        orders.append(messages.get_nowait())
                    try:
                        message = json.loads(twin.recv(timeout=.2))
                    except TimeoutError:
                        message = None
                    if message and message.get('type') == 'robot.state':
                        states.append(message['data'])
                    if message and message.get('type') == 'robot.action':
                        action = message['data']
                        product = by_twin[action['agv_id']]
                        target = product['unreal_pickup_node' if action['action'] == 'grab'
                                         else 'unreal_destination_node']
                        x, y = positions[target]
                        self.assertAlmostEqual(action['expected_x'], x, places=3)
                        self.assertAlmostEqual(action['expected_y'], y, places=3)
                        actions.append((action['agv_id'], action['action']))
                        twin.send(action_reply(action))
                    status = request(server_port, '/order-demo/api/status')
                    if any(item['status'] in ('failed', 'unknown') for item in status['robots']):
                        self.fail('field order failed: ' + repr(status) + '\n' +
                                  (folder / 'server.log').read_text(encoding='utf-8', errors='replace'))
                    if all(item['status'] == 'completed' for item in status['robots']
                           if item['robot_id'] in assignments):
                        break
                else:
                    self.fail('field orders did not finish: ' + repr(status) + '\n' +
                              (folder / 'server.log').read_text(encoding='utf-8', errors='replace'))
                expected_actions = sorted((twin, action) for twin in by_twin for action in ('grab', 'place'))
                self.assertEqual(sorted(actions), expected_actions)
                self.assertTrue(any('x' in state and 'y' in state for state in states))
                self.assertTrue(orders)
                self.assertTrue(all(order['map_id'] == 'mat_ssafy_v2@v0' for order in orders))
                if repeat:
                    pickup = [order for order in orders if order['order_id'].endswith('pickup')]
                    self.assertTrue(any(len(order['nodes']) != len(set(order['nodes'])) for order in pickup))
                    self.assertGreaterEqual(sum(order['released_until'] == 'M3' for order in pickup), 2)
                dbs = list(folder.glob('agv-web-workflow-*/server.sqlite3'))
                self.assertEqual(len(dbs), 1)
                with closing(sqlite3.connect(dbs[0])) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM (SELECT kind,resource_id,COUNT(*) n '
                        'FROM allocation_occupancy GROUP BY kind,resource_id HAVING n>1)').fetchone(), (0,))


if __name__ == '__main__':
    unittest.main()
