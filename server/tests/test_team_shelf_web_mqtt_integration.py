"""HTTP to slot claims, real isolated MQTT and a simulated Unreal peer.

This peer is a test double. It does not prove a physical position or an
actual rendered Unreal session.
"""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest

import paho.mqtt.client as mqtt
from websockets.sync.client import connect

from server.tests.test_order_product_integration import request
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port, stop,
    stop_publisher, wait_for_owned_broker, wait_for_owned_server)
from server.tools.twin_multi_test import isolated_environment
from server.team_shelf_web_mapping import load_team_shelf_mapping

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class TeamShelfWebMqttTest(unittest.TestCase):
    def test_web_order_runs_existing_mqtt_workflow_to_slot_actions(self):
        self._exercise(('milk',))

    def test_parallel_two_products_are_dispatched_to_two_robots(self):
        self._exercise(('milk', 'tissue'))

    def test_repeated_product_uses_next_source_and_destination_slot(self):
        self._exercise(('milk',), repeat=True)

    def _exercise(self, product_ids, *, repeat=False):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        token = secrets.token_urlsafe(32)
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        examples = ROOT / 'server/examples'
        unreal_file = examples / 'mat_ssafy_v3_unreal_sim.json'
        config_file = examples / 'field_unreal_v3_sim.json'
        field, mapping = load_team_shelf_mapping(config_file, json.loads(unreal_file.read_text(encoding='utf-8')))
        positions = {node['id']: node['pos'] for node in field['nodes']}
        robots = {}
        for robot, twin in (('sim-r1', 'AGV_01'), ('sim-r2', 'AGV_02')):
            x, y = positions[mapping.robots[robot]['node']]
            pose = mapping.pose({'x': x, 'y': y, 'th': 0})
            robots[twin] = {'agv_id': twin, **pose, 'carrying': False, 'interpolating': False}
        slots = [{'slot_id': i, 'state': 'empty', 'product_type': None} for i in range(1, 25)]
        for index, product in enumerate(('milk', 'tissue', 'egg', 'juice')):
            slots[index].update(state='occupied', product_type=product)
        if repeat:
            slots[1]['product_type'] = 'milk'
        with tempfile.TemporaryDirectory(prefix='team-web-mqtt-') as temp, ExitStack() as cleanup:
            folder = Path(temp)
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker_log = cleanup.enter_context(open(os.devnull, 'w'))
            ready = folder / 'broker.ready'
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), str(ready)],
                env=broker_env, cwd=ROOT, stdout=broker_log, stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(ready))
            messages, subscribed = queue.Queue(), __import__('threading').Event()
            observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='team-web-' + secrets.token_hex(5),
                                   protocol=mqtt.MQTTv311, reconnect_on_failure=False)
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
                'AGV_ORDER_FLEET': '1', 'AGV_ORDER_SHELF_SYNC': '1', 'AGV_ORDER_TEAM_SHELVES': '1',
                'AGV_ORDER_TEAM_WORLD_REGISTRY': str(folder / 'worlds.sqlite3'),
                'AGV_ORDER_DEMO_MAP': str(unreal_file), 'AGV_ORDER_FIELD_CONFIG': str(config_file),
                'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port), 'AGV_TWIN_WEBSOCKET': '1',
                'AGV_TWIN_UNREAL_TOKEN': token,
                'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'})})
            log = cleanup.enter_context((folder / 'server.log').open('w', encoding='utf-8'))
            server = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app', '--host',
                '127.0.0.1', '--port', str(server_port), '--workers', '1', '--no-proxy-headers'],
                cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=flags)
            cleanup.callback(stop, server)
            try:
                wait_for_owned_server(self, server, str(folder / 'server.log'), server_port)
            except AssertionError as exc:
                log.flush()
                raise AssertionError(str(exc) + '\n' + (folder / 'server.log').read_text(encoding='utf-8')) from exc
            with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port), proxy=None,
                         origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': token}) as twin:
                actions = []
                submitted = False
                futures = {}
                assignments = {}
                second_started = False
                pool = cleanup.enter_context(ThreadPoolExecutor(max_workers=len(product_ids)))
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    try:
                        message = json.loads(twin.recv(timeout=.2))
                    except TimeoutError:
                        message = None
                    if message and message.get('type') == 'robot.state':
                        data = message['data']
                        robots[data['agv_id']].update(x=data['x'], y=data['y'], yaw=data['yaw'])
                    elif message and message.get('type') == 'team.shelf.snapshot.request.v1':
                        twin.send(json.dumps({'type': 'team.shelf.snapshot.v1', 'data': {
                            'request_id': message['data']['request_id'], 'world_id': 'a' * 32,
                            'robots': list(robots.values()), 'slots': slots}}))
                    elif message and message.get('type') == 'team.shelf.action.v1':
                        data = message['data']
                        actions.append((data['product_type'], data['action'], data['slot_id']))
                        slot = slots[data['slot_id'] - 1]
                        if data['action'] == 'pick':
                            self.assertEqual((slot['state'], slot['product_type']), ('occupied', data['product_type']))
                            slot.update(state='empty', product_type=None)
                            robots[data['agv_id']]['carrying'] = True
                        else:
                            self.assertEqual(slot['state'], 'empty')
                            slot.update(state='occupied', product_type=data['product_type'])
                            robots[data['agv_id']]['carrying'] = False
                        twin.send(json.dumps({'type': 'team.shelf.action.result.v1',
                                              'data': dict(data, applied=True)}))
                    status = request(server_port, '/order-demo/api/status')
                    if not submitted and status.get('accepting_orders'):
                        futures = {product: pool.submit(request, server_port, '/order-demo/api/product-orders',
                                                       {'product_id': product}) for product in product_ids}
                        submitted = True
                    if submitted:
                        for product, future in futures.items():
                            if product not in assignments and future.done():
                                assignments[product] = future.result()['assigned_robot']
                        selected = [item for item in status['robots'] if item['robot_id'] in assignments.values()]
                        if any(item.get('status') in ('failed', 'unknown') for item in selected):
                            log.flush()
                            self.fail('slot order failed: ' + repr(status) + '\nactions=' + repr(actions) +
                                      '\n' + (folder / 'server.log').read_text(encoding='utf-8', errors='replace'))
                        if (len(assignments) == len(futures) and len(selected) == len(set(assignments.values()))
                                and all(item.get('status') == 'completed' for item in selected)):
                            if repeat and not second_started:
                                futures['milk-second'] = pool.submit(request, server_port,
                                    '/order-demo/api/product-orders', {'product_id': 'milk'})
                                second_started = True
                                continue
                            if repeat and (not futures['milk-second'].done()
                                           or len(actions) < 4):
                                continue
                            break
                else:
                    log.flush()
                    self.fail('team slot order timed out: ' + repr(status) + '\n' +
                              (folder / 'server.log').read_text(encoding='utf-8', errors='replace'))
                expected = {('milk', 'pick', 1), ('milk', 'place', 5),
                            ('tissue', 'pick', 2), ('tissue', 'place', 9)}
                if repeat:
                    expected = {('milk', 'pick', 1), ('milk', 'place', 5),
                                ('milk', 'pick', 2), ('milk', 'place', 6)}
                self.assertEqual(set(actions), {item for item in expected if item[0] in product_ids})
                self.assertEqual(len(actions), 4 if repeat else 2 * len(product_ids))
                if not repeat:
                    self.assertEqual(len(set(assignments.values())), len(product_ids))
                self.assertEqual((slots[0]['state'], slots[4]['product_type']), ('empty', 'milk'))
                orders = []
                while not messages.empty():
                    orders.append(messages.get_nowait())
                self.assertGreaterEqual(len(orders), 2)
                self.assertTrue(all(payload['map_id'] == 'mat_ssafy_v3@v0' for _, payload in orders))
