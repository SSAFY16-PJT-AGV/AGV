"""PR #62 topology through Coordinator, SQLite, MQTT and an Unreal double.

This is not a real Unreal map or physical stop test. Waiting points, products,
coordinate alignment, simulated robots and action ACKs are synthetic.

The map's extra waiting nodes, products, world coordinates and ACKs are test
fixtures. They are not physical locations or safety evidence.
"""
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

import paho.mqtt.client as mqtt
from websockets.sync.client import connect

from server.tests.test_order_product_integration import request, action_reply
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port,
    stop, stop_publisher, wait_for_owned_broker, wait_for_owned_server)
from server.tools.twin_multi_test import isolated_environment
from server.field_twin_mapping import FieldTwinMapping

ROOT = Path(__file__).resolve().parents[2]


def fixtures():
    # Repository examples are a virtual projection, not the team's UE level.
    # Additional waiting points, object locations and ACK/stop are synthetic.
    examples = ROOT / 'server/examples'
    unreal_path = examples / 'mat_ssafy_v3_unreal_sim.json'
    config_path = examples / 'field_unreal_v3_sim.json'
    unreal = json.loads(unreal_path.read_text(encoding='utf-8'))
    config = json.loads(config_path.read_text(encoding='utf-8'))
    positions = {node['id']: (node['x'], node['y']) for node in unreal['nodes']}
    return unreal_path, config_path, config['products'], positions


class V3RealUnrealSnapshotContract(unittest.TestCase):
    def test_original_team_unreal_snapshot_cannot_be_bound_to_v3(self):
        # Even if a caller deliberately selects that snapshot and supplies
        # plausible anchor IDs, the differing map semantics must fail closed.
        _, config_path, _, _ = fixtures()
        config = json.loads(config_path.read_text(encoding='utf-8'))
        field = json.loads((config_path.parent / config.pop('field_map_file')).read_text(encoding='utf-8'))
        original = json.loads((ROOT / 'server/tests/fixtures/unreal_warehouse_ce23ec8.json')
                              .read_text(encoding='utf-8'))
        config['unreal_map_id'] = original['map_id']
        with self.assertRaisesRegex(ValueError, 'v3 field and Unreal map snapshots do not match'):
            FieldTwinMapping(field, original, config)


@unittest.skipUnless(os.getenv('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class V3WebUnrealIntegration(unittest.TestCase):
    def test_two_web_orders_use_team_map_and_transformed_unreal_actions(self):
        self._exercise()

    def _exercise(self):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        token, python = secrets.token_urlsafe(32), getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='field-web-unreal-') as temp, ExitStack() as cleanup:
            folder = Path(temp)
            unreal_map, config, products, positions = fixtures()
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
            observer.on_message = lambda _c, _u, msg: messages.put((msg.topic, json.loads(msg.payload)))
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
                blue = request(server_port, '/order-demo/api/product-orders', {'product_id': 'blue'})
                assignments[blue['assigned_robot']] = products[1]
                self.assertEqual(set(assignments), {'sim-r1', 'sim-r2'})
                by_twin = {('AGV_01' if robot == 'sim-r1' else 'AGV_02'): item
                           for robot, item in assignments.items()}
                actions, states, orders = [], [], []
                occupied_samples = 0
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
                    dbs = list(folder.glob('agv-web-workflow-*/server.sqlite3'))
                    self.assertEqual(len(dbs), 1)
                    with closing(sqlite3.connect(dbs[0])) as db:
                        count = db.execute('SELECT COUNT(*) FROM allocation_occupancy').fetchone()[0]
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM (SELECT kind,resource_id,COUNT(*) n '
                            'FROM allocation_occupancy GROUP BY kind,resource_id HAVING n>1)').fetchone(), (0,))
                        occupied_samples += count > 0
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
                while not messages.empty():
                    orders.append(messages.get_nowait())
                self.assertTrue(orders)
                self.assertEqual({topic for topic, _ in orders}, {'agv/sim-r1/order', 'agv/sim-r2/order'})
                self.assertTrue(all(order['map_id'] == 'mat_ssafy_v3@v0' for _, order in orders))
                self.assertGreater(occupied_samples, 0, 'never observed a reserved/occupied resource')
                dbs = list(folder.glob('agv-web-workflow-*/server.sqlite3'))
                self.assertEqual(len(dbs), 1)
                with closing(sqlite3.connect(dbs[0])) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM (SELECT kind,resource_id,COUNT(*) n '
                        'FROM allocation_occupancy GROUP BY kind,resource_id HAVING n>1)').fetchone(), (0,))


if __name__ == '__main__':
    unittest.main()
