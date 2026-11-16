"""An isolated forged MQTT order must not enter the robot's durable inbox.

This uses a private, newly started loopback broker, temporary files and a fake
Unreal socket. It does not contact the user's broker, port 8000 or real robot.
"""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
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

from server.tests.test_order_coordinated_integration import simulation_map
from server.tests.test_order_product_integration import request
from server.tests.test_twin_broker_integration import (
    BROKER, connect_publisher, free_port, stop, stop_publisher,
    wait_for_owned_broker, wait_for_owned_server,
)
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class ForgedOrderIntegrationTest(unittest.TestCase):
    def test_foreign_queued_order_is_not_persisted_or_moved_when_web_order_starts(self):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        token = secrets.token_urlsafe(32)
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='agv-forged-e2e-') as folder, \
             open(os.devnull, 'w', encoding='utf-8') as broker_log, \
             open(os.path.join(folder, 'server.log'), 'w', encoding='utf-8') as server_log, \
             ExitStack() as cleanup:
            base = Path(folder)
            temp_root = base / 'app-temp'
            temp_root.mkdir()
            map_path = base / 'warehouse_map.json'
            map_path.write_text(json.dumps(simulation_map()), encoding='utf-8')
            ready_file = str(base / 'broker.ready')
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
            env.update({
                'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                'TEMP': str(temp_root), 'TMP': str(temp_root), 'TMPDIR': str(temp_root),
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
            wait_for_owned_server(self, server, str(base / 'server.log'), server_port)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.assertIsNone(server.poll(), 'isolated server exited')
                if request(server_port, '/order-demo/api/status').get('connected'):
                    break
                time.sleep(.05)
            else:
                self.fail('isolated server MQTT was not ready')

            # Observer and publisher share one ordered MQTT connection. Seeing
            # the forged payload confirms the private broker received it before
            # the later HTTP order. The idle robot does not tick its queue yet.
            received, subscribed = threading.Event(), threading.Event()
            publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                    client_id='forged-observer-' + secrets.token_hex(5),
                                    protocol=mqtt.MQTTv311, reconnect_on_failure=False)
            def on_connect(client, userdata, flags, reason, properties):
                if reason.is_failure:
                    subscribed.set()
                else:
                    client.subscribe('agv/sim-r1/order', qos=1)
            def on_subscribe(client, userdata, mid, reasons, properties):
                if len(reasons) == 1 and not reasons[0].is_failure and reasons[0].value == 1:
                    subscribed.set()
            def on_message(client, userdata, message):
                if message.payload == forged:
                    received.set()
            publisher.on_connect, publisher.on_subscribe, publisher.on_message = on_connect, on_subscribe, on_message
            connect_publisher(publisher, broker_port)
            publisher.loop_start()
            cleanup.callback(stop_publisher, publisher)
            self.assertTrue(subscribed.wait(10), 'test observer did not subscribe')

            uri = 'ws://127.0.0.1:{}/ws/digital-twin'.format(server_port)
            with connect(uri, proxy=None, origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': token}):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    initial = request(server_port, '/order-demo/api/status')
                    if initial.get('unreal_connected'):
                        break
                    time.sleep(.05)
                else:
                    self.fail('fake Unreal was not attached')
                self.assertEqual(initial['status'], 'idle')
                forged = json.dumps({'order_id': 'forged-simulation-order', 'seq': 1,
                    'map_id': 'tiny@v1', 'nodes': ['J01', 'N01', 'D04'],
                    'released_until': 'N01', 'action_at_end': {'type': 'load'}},
                    separators=(',', ':')).encode('utf-8')
                info = publisher.publish('agv/sim-r1/order', forged, qos=1, retain=False)
                info.wait_for_publish(timeout=5)
                self.assertTrue(info.is_published())
                self.assertTrue(received.wait(5), 'test broker did not deliver forged order')
                time.sleep(1.0)  # Allow the connected robot subscriber to enqueue it.
                before = request(server_port, '/order-demo/api/status')
                self.assertEqual((before['status'], before['pose']), ('idle', initial['pose']))
                attempt = request(server_port, '/order-demo/api/product-orders', {'product_id': 'red'})
                self.assertEqual(attempt['status'], 'unknown')
                after = request(server_port, '/order-demo/api/status')
                self.assertEqual((after['status'], after['pose']), ('unknown', initial['pose']))
                self.assertFalse(after['connected'], 'forged order must fail the simulated robot closed')

                robot_dbs = list(temp_root.glob('agv-web-workflow-*/robot.sqlite3'))
                self.assertEqual(len(robot_dbs), 1, 'expected a private simulated robot inbox')
                uri = robot_dbs[0].resolve().as_uri() + '?mode=ro'
                with closing(sqlite3.connect(uri, uri=True)) as db:
                    rows = db.execute('SELECT order_id FROM sim_inbox_orders').fetchall()
                self.assertEqual(rows, [], 'foreign order must not be durably accepted')

        self.assertIsNotNone(server.poll(), 'isolated test server was not stopped')
        self.assertIsNotNone(broker.poll(), 'isolated test broker was not stopped')


if __name__ == '__main__':
    unittest.main()
