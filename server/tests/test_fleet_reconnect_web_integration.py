"""Isolated web orders resume after a broker outage, with a fake Unreal client.

This test owns its loopback broker, server and database. It exercises no
physical robot and does not establish real-world collision safety.
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
import time
import unittest

from websockets.sync.client import connect
import paho.mqtt.client as mqtt

from server.tests.test_fleet_disconnect_integration import _occupancy, _wait
from server.tests.test_order_fleet_integration import scenario_map
from server.tests.test_order_product_integration import action_reply, request
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port, stop,
                                                       stop_publisher, wait_for_owned_broker,
                                                       wait_for_owned_server)
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class FleetReconnectWebIntegrationTest(unittest.TestCase):
    def test_moving_web_orders_keep_occupancy_then_both_finish_after_broker_returns(self):
        self._exercise_recovery(single_robot=False)

    def test_single_robot_disconnect_keeps_both_orders_and_finishes(self):
        self._exercise_recovery(single_robot=True)

    def _exercise_recovery(self, single_robot):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        token = secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory(prefix='agv-reconnect-web-') as folder, \
                open(os.devnull, 'w', encoding='utf-8') as broker_log, \
                open(os.path.join(folder, 'server.log'), 'w', encoding='utf-8') as server_log, \
                ExitStack() as cleanup:
            raw = scenario_map(False)
            for node in raw['nodes']:
                node['x'] *= 2
                node['y'] *= 2
            map_path = Path(folder) / 'warehouse_map.json'
            map_path.write_text(json.dumps(raw), encoding='utf-8')
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']

            def start_broker(name):
                ready = str(Path(folder) / (name + '.ready'))
                process = subprocess.Popen([python, '-c', BROKER, str(broker_port), ready],
                    cwd=ROOT, env=broker_env, stdout=broker_log, stderr=broker_log, creationflags=flags)
                cleanup.callback(stop, process)
                wait_for_owned_broker(self, process, ready)
                return process

            broker = start_broker('first')
            env = isolated_environment(os.environ)
            for key in list(env):
                if key.upper().startswith('AGV_'):
                    del env[key]
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                        'TEMP': folder, 'TMP': folder,
                        'AGV_ORDER_DEMO_ENABLED': '1', 'AGV_ORDER_COORDINATED': '1',
                        'AGV_ORDER_FLEET': '1', 'AGV_ORDER_DEMO_MAP': str(map_path),
                        'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port),
                        'AGV_TWIN_WEBSOCKET': '1', 'AGV_TWIN_UNREAL_TOKEN': token,
                        'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}),
                        'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1'})
            server = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app',
                '--host', '127.0.0.1', '--port', str(server_port), '--workers', '1', '--no-proxy-headers',
                '--log-level', 'info', '--no-access-log'],
                cwd=ROOT, env=env, stdout=server_log, stderr=server_log, creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, str(Path(folder) / 'server.log'), server_port)
            status = lambda: request(server_port, '/order-demo/api/status')
            self.assertTrue(_wait(lambda: status().get('connected'), 15), 'server not ready')
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
                def robots():
                    return {item['robot_id']: item for item in status()['robots']}
                def assert_no_overlapping_reservations():
                    with closing(sqlite3.connect(db_path, timeout=5)) as db:
                        duplicate = db.execute(
                            'SELECT kind,resource_id FROM allocation_occupancy GROUP BY kind,resource_id '
                            'HAVING COUNT(DISTINCT task_id)>1').fetchall()
                    self.assertEqual(duplicate, [], 'simulated reservations must not overlap')
                def both_mid_edge():
                    states = robots()
                    assert_no_overlapping_reservations()
                    return (all(item['status'] == 'moving' for item in states.values())
                            and states['sim-r1']['pose']['x'] != -6000.0
                            and states['sim-r2']['pose']['x'] != 6000.0)
                self.assertTrue(_wait(both_mid_edge, 8), 'both virtual robots must leave their start nodes')
                before = _occupancy(db_path)
                self.assertEqual({row[2] for row in before}, {'web-pickup', 'r2-web-pickup'})
                self.assertTrue(any(row[0] == 'edge' for row in before))
                if single_robot:
                    # The duplicate client ID makes this test-owned broker
                    # disconnect only r2; the observer and r1 stay connected.
                    replacement = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                              client_id='sim-r2-order-executor',
                                              clean_session=False, protocol=mqtt.MQTTv311,
                                              reconnect_on_failure=False)
                    connect_publisher(replacement, broker_port)
                    replacement.loop_start()
                    cleanup.callback(stop_publisher, replacement)
                else:
                    stop(broker)
                self.assertTrue(_wait(lambda: not status()['connected'], 8))
                if single_robot:
                    self.assertIsNone(broker.poll(), 'test broker must remain alive during one-client fault')
                self.assertEqual(_occupancy(db_path), before)
                time.sleep(1)
                self.assertEqual(_occupancy(db_path), before)
                self.assertIsNone(server.poll(), 'web server must stay alive while broker is down')
                if single_robot:
                    stop_publisher(replacement)
                else:
                    start_broker('restored')
                reconnected = _wait(lambda: status().get('connected'), 25)
                if not reconnected:
                    server_log.flush()
                    tail = (Path(folder) / 'server.log').read_text(encoding='utf-8', errors='replace')[-10000:]
                    self.fail('web workflow did not reconnect: {!r}\n{}'.format(status(), tail))

                actions = []
                deadline = time.monotonic() + 100
                while time.monotonic() < deadline:
                    assert_no_overlapping_reservations()
                    try:
                        message = json.loads(unreal.recv(timeout=.1))
                    except TimeoutError:
                        message = None
                    if message and message.get('type') == 'robot.action':
                        action = message['data']
                        actions.append((action['agv_id'], action['action']))
                        unreal.send(action_reply(action))
                    states = robots()
                    if any(item['status'] in ('failed', 'unknown') for item in states.values()):
                        server_log.flush()
                        tail = (Path(folder) / 'server.log').read_text(encoding='utf-8', errors='replace')[-10000:]
                        self.fail('fleet entered unknown state: {!r}\n{}'.format(states, tail))
                    if all(item['status'] == 'completed' for item in states.values()):
                        break
                else:
                    self.fail('both web orders did not finish after reconnect: {!r}'.format(robots()))
                self.assertEqual(sorted(actions), [('AGV_01', 'grab'), ('AGV_01', 'place'),
                                                   ('AGV_02', 'grab'), ('AGV_02', 'place')])
                with closing(sqlite3.connect(db_path, timeout=5)) as db:
                    finished = db.execute("SELECT task_id FROM progress_events WHERE result='finished'").fetchall()
                self.assertEqual({row[0] for row in finished},
                                 {'web-pickup', 'web-delivery', 'r2-web-pickup', 'r2-web-delivery'})


if __name__ == '__main__':
    unittest.main()
