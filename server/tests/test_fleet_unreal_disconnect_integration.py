"""Disconnect the test Unreal socket while two isolated virtual robots move."""
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

from server.tests.test_fleet_disconnect_integration import _occupancy, _wait
from server.tests.test_order_fleet_integration import scenario_map
from server.tests.test_order_product_integration import request
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port, stop,
                                                       stop_publisher, wait_for_owned_broker,
                                                       wait_for_owned_server)
from server.tools.twin_multi_test import isolated_environment

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class UnrealDisconnectIntegrationTest(unittest.TestCase):
    def test_unreal_disconnect_freezes_both_orders_and_preserves_reservations(self):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        token = secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory(prefix='agv-unreal-cut-') as folder, \
                open(os.devnull, 'w', encoding='utf-8') as broker_log, \
                open(os.path.join(folder, 'server.log'), 'w', encoding='utf-8') as server_log, \
                ExitStack() as cleanup:
            raw = scenario_map(False)
            for node in raw['nodes']:
                node['x'] *= 3
                node['y'] *= 3
            map_path = Path(folder) / 'map.json'
            map_path.write_text(json.dumps(raw), encoding='utf-8')
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            ready = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), ready], cwd=ROOT,
                                      env=broker_env, stdout=broker_log, stderr=broker_log,
                                      creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready)
            received, subscribed = queue.Queue(), threading.Event()
            observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='observe-' + secrets.token_hex(5),
                                   reconnect_on_failure=False)
            observer.on_connect = lambda c, u, f, reason, p: c.subscribe('agv/+/state', qos=0)
            observer.on_subscribe = lambda c, u, m, reasons, p: subscribed.set()
            observer.on_message = lambda c, u, msg: received.put(msg.topic)
            connect_publisher(observer, broker_port)
            observer.loop_start()
            cleanup.callback(stop_publisher, observer)
            self.assertTrue(subscribed.wait(8))
            env = isolated_environment(os.environ)
            for key in list(env):
                if key.upper().startswith('AGV_'):
                    del env[key]
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                        'TEMP': folder, 'TMP': folder, 'AGV_ORDER_DEMO_ENABLED': '1',
                        'AGV_ORDER_COORDINATED': '1', 'AGV_ORDER_FLEET': '1',
                        'AGV_ORDER_DEMO_MAP': str(map_path), 'MQTT_HOST': '127.0.0.1',
                        'MQTT_PORT': str(broker_port), 'AGV_TWIN_WEBSOCKET': '1',
                        'AGV_TWIN_UNREAL_TOKEN': token,
                        'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}),
                        'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1'})
            server = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app',
                                       '--host', '127.0.0.1', '--port', str(server_port),
                                       '--workers', '1', '--no-proxy-headers'], cwd=ROOT,
                                      env=env, stdout=server_log, stderr=server_log, creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, str(Path(folder) / 'server.log'), server_port)
            status = lambda: request(server_port, '/order-demo/api/status')
            self.assertTrue(_wait(lambda: status().get('connected'), 15))
            uri = 'ws://127.0.0.1:{}/ws/digital-twin'.format(server_port)
            with connect(uri, proxy=None, origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': token}) as unreal:
                self.assertTrue(_wait(lambda: status().get('unreal_connected'), 5))
                red = request(server_port, '/order-demo/api/product-orders', {'product_id': 'red'})
                blue = request(server_port, '/order-demo/api/product-orders', {'product_id': 'blue'})
                self.assertEqual((red['assigned_robot'], blue['assigned_robot']), ('sim-r1', 'sim-r2'))
                paths = list(Path(folder).glob('agv-web-workflow-*/server.sqlite3'))
                self.assertEqual(len(paths), 1)
                db_path = paths[0]
                def moving():
                    states = {item['robot_id']: item for item in status()['robots']}
                    return (all(item['status'] == 'moving' for item in states.values())
                            and states['sim-r1']['pose']['x'] != -9000.0
                            and states['sim-r2']['pose']['x'] != 9000.0)
                self.assertTrue(_wait(moving, 8))
                before = _occupancy(db_path)
                self.assertEqual({row[2] for row in before}, {'web-pickup', 'r2-web-pickup'})
                unreal.close()
                self.assertTrue(_wait(lambda: not status().get('unreal_connected'), 5))
                # A tick already in flight at disconnect may finish. Drain its
                # observations; later publications would indicate continued work.
                time.sleep(.5)
                while not received.empty():
                    received.get_nowait()
                with self.assertRaises(queue.Empty):
                    received.get(timeout=1.0)
                self.assertEqual(_occupancy(db_path), before)
            # A fresh authenticated socket cannot silently revive unknown work.
            with connect(uri, proxy=None, origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': token}):
                time.sleep(.5)
                while not received.empty():
                    received.get_nowait()
                with self.assertRaises(queue.Empty):
                    received.get(timeout=1.0)
                self.assertEqual(_occupancy(db_path), before)
                self.assertFalse(status()['accepting_orders'])


if __name__ == '__main__':
    unittest.main()
