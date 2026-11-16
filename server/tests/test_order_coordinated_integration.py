"""Opt-in web -> reserved MQTT orders -> fake Unreal acknowledgement test.

All processes use private loopback ports and a temporary broker.  Unreal is a
WebSocket test client here; this test never controls a real robot or editor.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import secrets
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
from server.tests.test_twin_broker_integration import (
    BROKER, connect_publisher, free_port, stop, stop_publisher,
    wait_for_owned_broker, wait_for_owned_server,
)
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]


def simulation_map():
    # The pickup-to-delivery route revisits N01. Separate task routes must be
    # scheduled, not a single ambiguous order with a duplicate node.
    nodes = [
        {'id': 'J01', 'x': 0, 'y': 0}, {'id': 'N01', 'x': .2, 'y': 0},
        {'id': 'D01', 'x': .2, 'y': .2}, {'id': 'D04', 'x': .4, 'y': 0},
        {'id': 'D06', 'x': .2, 'y': -.2}, {'id': 'D05', 'x': .4, 'y': -.2},
    ]
    pairs = [('J01', 'N01'), ('N01', 'D01'), ('N01', 'D04'),
             ('N01', 'D06'), ('D06', 'D05')]
    return {'map_id': 'tiny', 'coordinate_unit': 'meter', 'nodes': nodes,
            'edges': [{'from': a, 'to': b, 'points': []} for a, b in pairs]}


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class CoordinatedProductOrderIntegrationTest(unittest.TestCase):
    def test_web_order_uses_reserved_mqtt_pickup_and_delivery_before_acknowledging(self):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        token = secrets.token_urlsafe(32)
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='agv-coordinated-e2e-') as folder, \
             open(os.devnull, 'w', encoding='utf-8') as broker_log, \
             open(os.path.join(folder, 'server.log'), 'w', encoding='utf-8') as server_log, \
             ExitStack() as cleanup:
            map_path = Path(folder) / 'warehouse_map.json'
            map_path.write_text(json.dumps(simulation_map()), encoding='utf-8')
            ready_file = str(Path(folder) / 'broker.ready')
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), ready_file],
                                      cwd=ROOT, env=broker_env, stdout=broker_log,
                                      stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_file)

            orders, subscribed = queue.Queue(), threading.Event()
            errors = []
            observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id='test-order-observer-' + secrets.token_hex(5),
                                   protocol=mqtt.MQTTv311, reconnect_on_failure=False)
            def on_connect(client, userdata, flags, reason, properties):
                if reason.is_failure:
                    errors.append('broker connection refused')
                    subscribed.set()
                else:
                    client.subscribe('agv/sim-r1/order', qos=1)
            def on_subscribe(client, userdata, mid, reasons, properties):
                if len(reasons) != 1 or reasons[0].is_failure or reasons[0].value != 1:
                    errors.append('order subscription was rejected')
                subscribed.set()
            def on_message(client, userdata, message):
                orders.put((message.topic, message.qos, message.retain, message.payload))
            observer.on_connect, observer.on_subscribe, observer.on_message = on_connect, on_subscribe, on_message
            connect_publisher(observer, broker_port)
            observer.loop_start()
            cleanup.callback(stop_publisher, observer)
            self.assertTrue(subscribed.wait(10), 'order observation did not subscribe')
            self.assertEqual(errors, [])

            env = isolated_environment(os.environ)
            for key in list(env):
                if key.upper().startswith('AGV_'):
                    del env[key]
            env.update({
                'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                'AGV_ORDER_DEMO_ENABLED': '1', 'AGV_ORDER_COORDINATED': '1',
                'AGV_ORDER_DEMO_MAP': str(map_path), 'MQTT_HOST': '127.0.0.1',
                'MQTT_PORT': str(broker_port), 'AGV_TWIN_WEBSOCKET': '1',
                'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}),
                'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1', 'AGV_TWIN_UNREAL_TOKEN': token,
            })
            server = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app',
                                       '--host', '127.0.0.1', '--port', str(server_port),
                                       '--workers', '1', '--no-proxy-headers'], cwd=ROOT,
                                      env=env, stdout=server_log, stderr=server_log, creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, str(Path(folder) / 'server.log'), server_port)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.assertIsNone(server.poll(), 'isolated coordinated server exited')
                if request(server_port, '/order-demo/api/status').get('connected'):
                    break
                time.sleep(.05)
            else:
                self.fail('isolated coordinated server was not ready')

            uri = 'ws://127.0.0.1:{}/ws/digital-twin'.format(server_port)
            with connect(uri, proxy=None, origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': token}) as fake_unreal:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if request(server_port, '/order-demo/api/status').get('unreal_connected'):
                        break
                    time.sleep(.05)
                else:
                    self.fail('fake Unreal did not attach')

                initial = request(server_port, '/order-demo/api/status')
                self.assertEqual(initial['status'], 'idle')
                time.sleep(.3)
                self.assertEqual(request(server_port, '/order-demo/api/status')['pose'], initial['pose'])
                self.assertTrue(orders.empty(), 'no robot order may be sent before a web request')

                accepted = request(server_port, '/order-demo/api/product-orders', {'product_id': 'red'})
                self.assertNotEqual(accepted['status'], 'completed')
                actions = []
                received = []
                deadline = time.monotonic() + 55
                final = accepted
                while time.monotonic() < deadline:
                    while True:
                        try:
                            topic, qos, retained, raw = orders.get_nowait()
                        except queue.Empty:
                            break
                        self.assertEqual((topic, qos, retained), ('agv/sim-r1/order', 1, False))
                        received.append(json.loads(raw))
                    try:
                        message = json.loads(fake_unreal.recv(timeout=.3))
                    except TimeoutError:
                        message = None
                    if message and message.get('type') == 'robot.action':
                        action = message['data']
                        actions.append(action['action'])
                        if action['action'] == 'grab':
                            waiting = request(server_port, '/order-demo/api/status')
                            self.assertEqual((waiting['status'], waiting['phase'], waiting['current_node']),
                                             ('waiting', 'waiting_grab', 'D01'))
                            self.assertTrue(any(item['order_id'] == 'web-pickup' for item in received),
                                            'pickup must be an MQTT order before grab')
                            # A mismatched acknowledgement cannot transfer the
                            # reserved pickup node or start the delivery leg.
                            fake_unreal.send(action_reply(action, action_id='incorrect-action-id'))
                            time.sleep(.4)
                            still_waiting = request(server_port, '/order-demo/api/status')
                            self.assertEqual((still_waiting['status'], still_waiting['phase']),
                                             ('waiting', 'waiting_grab'))
                            self.assertEqual(still_waiting['pose'], waiting['pose'])
                            while not orders.empty():
                                topic, qos, retained, raw = orders.get_nowait()
                                self.assertEqual((topic, qos, retained), ('agv/sim-r1/order', 1, False))
                                received.append(json.loads(raw))
                            self.assertFalse(any(item['order_id'] == 'web-delivery' for item in received),
                                             'delivery must not start before confirmed grab')
                        elif action['action'] == 'place':
                            self.assertEqual(request(server_port, '/order-demo/api/status')['current_node'], 'D04')
                            self.assertTrue(any(item['order_id'] == 'web-delivery' for item in received),
                                            'delivery must be an MQTT order before place')
                        else:
                            self.fail('unexpected Unreal action')
                        fake_unreal.send(action_reply(action))
                    final = request(server_port, '/order-demo/api/status')
                    if final['status'] == 'completed':
                        break
                    if final['status'] in ('unknown', 'failed'):
                        self.fail('coordinated order entered an unsuccessful state')

                self.assertEqual((final['status'], final['phase'], final['current_node']),
                                 ('completed', 'done', 'D04'))
                self.assertEqual(actions, ['grab', 'place'])
                while not orders.empty():
                    topic, qos, retained, raw = orders.get_nowait()
                    self.assertEqual((topic, qos, retained), ('agv/sim-r1/order', 1, False))
                    received.append(json.loads(raw))
                pickup = [item for item in received if item['order_id'] == 'web-pickup']
                delivery = [item for item in received if item['order_id'] == 'web-delivery']
                self.assertEqual([(x['seq'], x['released_until']) for x in pickup], [(1, 'N01'), (2, 'D01')])
                self.assertEqual([(x['seq'], x['released_until']) for x in delivery], [(1, 'N01'), (2, 'D04')])
                self.assertTrue(all(x['nodes'] == ['J01', 'N01', 'D01'] and
                                    x['action_at_end'] == {'type': 'load'} for x in pickup))
                self.assertTrue(all(x['nodes'] == ['D01', 'N01', 'D04'] and
                                    x['action_at_end'] == {'type': 'unload'} for x in delivery))
                self.assertTrue(all(x['map_id'] == 'tiny@v1' for x in received))

                # A second product order reuses the same broker, app, robot and
                # Unreal session. Its task IDs must be fresh and the previous
                # final-node reservation must be handed over transactionally.
                second = request(server_port, '/order-demo/api/product-orders', {'product_id': 'blue'})
                self.assertNotEqual(second['status'], 'completed')
                second_actions = []
                deadline = time.monotonic() + 55
                while time.monotonic() < deadline:
                    while True:
                        try:
                            topic, qos, retained, raw = orders.get_nowait()
                        except queue.Empty:
                            break
                        self.assertEqual((topic, qos, retained), ('agv/sim-r1/order', 1, False))
                        received.append(json.loads(raw))
                    try:
                        message = json.loads(fake_unreal.recv(timeout=.3))
                    except TimeoutError:
                        message = None
                    if message and message.get('type') == 'robot.action':
                        action = message['data']
                        second_actions.append(action['action'])
                        current = request(server_port, '/order-demo/api/status')
                        if action['action'] == 'grab':
                            self.assertEqual((action['product_type'], current['current_node']), ('Blue', 'D06'))
                            self.assertTrue(any(item['order_id'] == 'web-2-pickup' for item in received))
                            self.assertFalse(any(item['order_id'] == 'web-2-delivery' for item in received))
                        elif action['action'] == 'place':
                            self.assertEqual((action['shelf_id'], action['slot_index'], current['current_node']),
                                             ('SHELF_BLUE', 0, 'D05'))
                            self.assertTrue(any(item['order_id'] == 'web-2-delivery' for item in received))
                        else:
                            self.fail('unexpected second-order Unreal action')
                        fake_unreal.send(action_reply(action))
                    second = request(server_port, '/order-demo/api/status')
                    if second['status'] == 'completed':
                        break
                    if second['status'] in ('unknown', 'failed'):
                        self.fail('second coordinated order entered an unsuccessful state')

                self.assertEqual((second['status'], second['phase'], second['current_node'], second['product_type']),
                                 ('completed', 'done', 'D05', 'Blue'))
                self.assertEqual(second_actions, ['grab', 'place'])
                while not orders.empty():
                    topic, qos, retained, raw = orders.get_nowait()
                    self.assertEqual((topic, qos, retained), ('agv/sim-r1/order', 1, False))
                    received.append(json.loads(raw))
                pickup2 = [item for item in received if item['order_id'] == 'web-2-pickup']
                delivery2 = [item for item in received if item['order_id'] == 'web-2-delivery']
                self.assertEqual([(x['seq'], x['released_until']) for x in pickup2], [(1, 'N01'), (2, 'D06')])
                self.assertEqual([(x['seq'], x['released_until']) for x in delivery2], [(1, 'D05')])
                self.assertTrue(all(x['nodes'] == ['D04', 'N01', 'D06'] and
                                    x['action_at_end'] == {'type': 'load'} for x in pickup2))
                self.assertTrue(all(x['nodes'] == ['D06', 'D05'] and
                                    x['action_at_end'] == {'type': 'unload'} for x in delivery2))

        self.assertIsNotNone(server.poll(), 'coordinated server was not stopped')
        self.assertIsNotNone(broker.poll(), 'isolated broker was not stopped')


if __name__ == '__main__':
    unittest.main()
