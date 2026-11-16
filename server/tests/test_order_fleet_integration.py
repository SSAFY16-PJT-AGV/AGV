"""Isolated two-robot web orders through MQTT and a fake Unreal WebSocket."""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
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
from server.tests.test_twin_broker_integration import (
    BROKER, connect_publisher, free_port, stop, stop_publisher,
    wait_for_owned_broker, wait_for_owned_server,
)
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]


def preserve_failure(folder, db_path, state, actions, orders):
    """Keep only this test's diagnostic evidence before its tempdir closes."""
    target = ROOT / '.venv' / ('fleet-e2e-failure-' + secrets.token_hex(5))
    target.mkdir()
    if db_path.is_file():
        with closing(sqlite3.connect(db_path, timeout=5)) as src, \
             closing(sqlite3.connect(target / 'server.sqlite3')) as dst:
            src.backup(dst)
    for name in ('server.log', 'warehouse_map.json'):
        source = Path(folder) / name
        if source.is_file():
            shutil.copyfile(source, target / name)
    (target / 'observed.json').write_text(json.dumps(
        {'state': state, 'actions': actions, 'orders': orders}, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(target)


def scenario_map(contended):
    # With contention both initial routes need N01; in the other case they
    # use independent resources. Neither case proves real-world safety.
    coords = {'J01': (-3, 0), 'J05': (3, 0), 'N01': (0, 0),
              'D01': (0, .5), 'D04': (-.5, .5),
              'D06': (0, -.5), 'D05': (-.5, -.5)}
    pairs = [('J01', 'N01'), ('N01', 'D01'), ('N01', 'D04'), ('D06', 'D05')]
    if contended:
        pairs += [('J05', 'N01'), ('N01', 'D06')]
    else:
        coords['N02'] = (2, -.5)
        pairs += [('J05', 'N02'), ('N02', 'D06')]
    return {'map_id': 'fleet-test', 'coordinate_unit': 'meter',
            'nodes': [{'id': name, 'x': x, 'y': y} for name, (x, y) in coords.items()],
            'edges': [{'from': first, 'to': second, 'points': []} for first, second in pairs]}


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class FleetOrderIntegrationTest(unittest.TestCase):
    def test_two_web_orders_on_distinct_routes_both_complete(self):
        self._exercise_fleet(False)

    def test_conflicting_crossing_waits_then_other_robot_proceeds(self):
        self._exercise_fleet(True)

    def _exercise_fleet(self, contended):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        token = secrets.token_urlsafe(32)
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='agv-fleet-e2e-') as folder, \
             open(os.devnull, 'w', encoding='utf-8') as broker_log, \
             open(os.path.join(folder, 'server.log'), 'w', encoding='utf-8') as server_log, \
             ExitStack() as cleanup:
            map_path = Path(folder) / 'warehouse_map.json'
            map_path.write_text(json.dumps(scenario_map(contended)), encoding='utf-8')
            ready_file = str(Path(folder) / 'broker.ready')
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), ready_file],
                                      cwd=ROOT, env=broker_env, stdout=broker_log,
                                      stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_file)

            received, subscribed, errors = queue.Queue(), threading.Event(), []
            observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id='fleet-observer-' + secrets.token_hex(5),
                                   protocol=mqtt.MQTTv311, reconnect_on_failure=False)
            def on_connect(client, _userdata, _flags, reason, _properties):
                if reason.is_failure:
                    errors.append('broker connection refused')
                    subscribed.set()
                else:
                    client.subscribe([('agv/sim-r1/order', 1), ('agv/sim-r2/order', 1)])
            def on_subscribe(_client, _userdata, _mid, reasons, _properties):
                if len(reasons) != 2 or any(reason.is_failure or reason.value != 1 for reason in reasons):
                    errors.append('order subscriptions rejected')
                subscribed.set()
            def on_message(_client, _userdata, message):
                received.put((message.topic, message.qos, message.retain, json.loads(message.payload)))
            observer.on_connect, observer.on_subscribe, observer.on_message = on_connect, on_subscribe, on_message
            connect_publisher(observer, broker_port)
            observer.loop_start()
            cleanup.callback(stop_publisher, observer)
            self.assertTrue(subscribed.wait(10))
            self.assertEqual(errors, [])

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
                                       '--host', '127.0.0.1', '--port', str(server_port),
                                       '--workers', '1', '--no-proxy-headers'], cwd=ROOT,
                                      env=env, stdout=server_log, stderr=server_log, creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, str(Path(folder) / 'server.log'), server_port)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.assertIsNone(server.poll(), 'isolated fleet server exited')
                if request(server_port, '/order-demo/api/status').get('connected'):
                    break
                time.sleep(.05)
            else:
                self.fail('isolated fleet server was not ready')

            uri = 'ws://127.0.0.1:{}/ws/digital-twin'.format(server_port)
            with connect(uri, proxy=None, origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': token}) as unreal:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if request(server_port, '/order-demo/api/status').get('unreal_connected'):
                        break
                    time.sleep(.05)
                else:
                    self.fail('fake Unreal did not attach')
                initial = request(server_port, '/order-demo/api/status')
                self.assertEqual(initial['mode'], 'fleet')
                self.assertEqual({x['robot_id']: x['current_node'] for x in initial['robots']},
                                 {'sim-r1': 'J01', 'sim-r2': 'J05'})
                self.assertTrue(received.empty(), 'orders must not precede a web request')
                red = request(server_port, '/order-demo/api/product-orders', {'product_id': 'red'})
                blue = request(server_port, '/order-demo/api/product-orders', {'product_id': 'blue'})
                self.assertEqual((red['assigned_robot'], blue['assigned_robot']), ('sim-r1', 'sim-r2'))

                db_files = list(Path(folder).glob('agv-web-workflow-*/server.sqlite3'))
                self.assertEqual(len(db_files), 1, 'fleet must own one isolated reservation DB')
                db_path = db_files[0]
                if contended:
                    # The first segment takes roughly three seconds to reach
                    # N01, ensuring the second request is admitted while N01
                    # belongs to r1. It must keep its idle start node and its
                    # task PENDING instead of receiving a conflicting order.
                    with closing(sqlite3.connect(db_path, timeout=5)) as db:
                        self.assertEqual(db.execute("SELECT task_id FROM allocation_occupancy "
                            "WHERE kind='node' AND resource_id='N01'").fetchone(), ('web-pickup',))
                        self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id='r2-web-pickup'").fetchone(), ('PENDING',))
                        self.assertEqual(db.execute("SELECT node FROM sim_idle_robots WHERE robot_id='sim-r2'").fetchone(), ('J05',))
                        self.assertIsNone(db.execute("SELECT task_id FROM allocation_jobs WHERE task_id='r2-web-pickup'").fetchone())

                actions, orders, seen_live = [], [], set()
                saw_active_pair = saw_release = saw_handoff_wait = False
                deadline = time.monotonic() + 75
                while time.monotonic() < deadline:
                    while not received.empty():
                        topic, qos, retained, order = received.get_nowait()
                        self.assertIn(topic, ('agv/sim-r1/order', 'agv/sim-r2/order'))
                        self.assertEqual((qos, retained), (1, False))
                        orders.append((topic, order))
                    try:
                        message = json.loads(unreal.recv(timeout=.2))
                    except TimeoutError:
                        message = None
                    if message and message.get('type') == 'robot.action':
                        action = message['data']
                        actions.append((action['agv_id'], action['action']))
                        unreal.send(action_reply(action))
                    state = request(server_port, '/order-demo/api/status')
                    robots = {item['robot_id']: item for item in state['robots']}
                    for robot, snapshot in robots.items():
                        if snapshot['status'] in ('unknown', 'failed'):
                            evidence = preserve_failure(folder, db_path, state, actions, orders)
                            self.fail('fleet entered %s for %s; evidence: %s' %
                                      (snapshot['status'], robot, evidence))
                        if snapshot['status'] in ('moving', 'waiting'):
                            seen_live.add(robot)
                    if db_path.is_file():
                        with closing(sqlite3.connect(db_path, timeout=5)) as db:
                            db.execute('BEGIN')
                            duplicated = db.execute('SELECT kind,resource_id,COUNT(DISTINCT task_id) '
                                                    'FROM allocation_occupancy GROUP BY kind,resource_id '
                                                    'HAVING COUNT(DISTINCT task_id)>1').fetchall()
                            self.assertEqual(duplicated, [])
                            overlap = db.execute("SELECT i.node FROM sim_idle_robots i JOIN allocation_occupancy o "
                                                 "ON o.kind='node' AND o.resource_id=i.node").fetchall()
                            self.assertEqual(overlap, [])
                            active = db.execute('SELECT robot_id,task_id FROM sim_active_robots').fetchall()
                            if len(active) == 2:
                                saw_active_pair = True
                            owner = db.execute("SELECT task_id FROM allocation_occupancy WHERE kind='node' AND resource_id='N01'").fetchone()
                            if contended and owner == ('r2-web-pickup',):
                                saw_release = True
                            if (contended and ('AGV_01', 'grab') in actions
                                    and robots['sim-r1']['phase'] == 'reservation_wait'
                                    and owner == ('r2-web-pickup',)):
                                self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id='web-delivery'").fetchone(), ('PENDING',))
                                self.assertEqual(db.execute("SELECT task_id FROM allocation_occupancy WHERE kind='node' AND resource_id='D01'").fetchone(), ('web-pickup',))
                                self.assertIsNone(db.execute("SELECT task_id FROM allocation_jobs WHERE task_id='web-delivery'").fetchone())
                                saw_handoff_wait = True
                    if all(robots[key]['status'] == 'completed' for key in ('sim-r1', 'sim-r2')):
                        break
                else:
                    self.fail('two fleet orders did not both finish')

                while not received.empty():
                    topic, qos, retained, order = received.get_nowait()
                    self.assertEqual((qos, retained), (1, False))
                    orders.append((topic, order))
                self.assertEqual(seen_live, {'sim-r1', 'sim-r2'})
                self.assertTrue(saw_active_pair, 'both tasks should be active during the same test')
                if contended:
                    self.assertTrue(saw_release, 'r2 must acquire N01 after r1 releases it')
                    self.assertTrue(saw_handoff_wait, 'r1 must retain D01 and wait to hand off while r2 owns N01')
                self.assertEqual(sorted(actions), [('AGV_01', 'grab'), ('AGV_01', 'place'),
                                                   ('AGV_02', 'grab'), ('AGV_02', 'place')])
                for robot, pickup, delivery in [('sim-r1', 'web-pickup', 'web-delivery'),
                                                ('sim-r2', 'r2-web-pickup', 'r2-web-delivery')]:
                    messages = [order for topic, order in orders if topic == 'agv/{}/order'.format(robot)]
                    self.assertEqual({item['order_id'] for item in messages}, {pickup, delivery})
                    self.assertTrue(all(item['map_id'] == 'fleet-test@v1' for item in messages))
                    for task in (pickup, delivery):
                        sequences = [item['seq'] for item in messages if item['order_id'] == task]
                        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))
                with closing(sqlite3.connect(db_path, timeout=5)) as db:
                    rows = db.execute("SELECT task_id,result FROM progress_events WHERE result='finished'").fetchall()
                    self.assertEqual({task for task, _ in rows},
                                     {'web-pickup', 'web-delivery', 'r2-web-pickup', 'r2-web-delivery'})


if __name__ == '__main__':
    unittest.main()
