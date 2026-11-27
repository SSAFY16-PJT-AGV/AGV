"""A consumed simulated world cannot be reordered after an API restart."""
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
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import paho.mqtt.client as mqtt
from websockets.sync.client import connect

from server.team_shelf_web_mapping import load_team_shelf_mapping
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port, stop,
    stop_publisher, wait_for_owned_broker, wait_for_owned_server)
from server.tools.twin_multi_test import isolated_environment

ROOT = Path(__file__).resolve().parents[2]
HTTP = build_opener(ProxyHandler({}))


def api(port, method, path, key=None):
    headers = {'Host': '127.0.0.1:8000'}
    body = None
    if key is not None:
        headers.update({'Origin': 'http://127.0.0.1:8000', 'Content-Type': 'application/json',
                        'Idempotency-Key': key})
        body = json.dumps({'product_id': 'milk'}).encode()
    req = Request('http://127.0.0.1:{}{}'.format(port, path), method=method, headers=headers, data=body)
    try:
        with HTTP.open(req, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class TeamShelfWorldRestartHttpTest(unittest.TestCase):
    def test_same_world_and_request_key_after_server_restart_do_not_publish(self):
        ports = set()
        while len(ports) < 2:
            port = free_port()
            if port != 8000:
                ports.add(port)
        broker_port, server_port = ports
        token = secrets.token_urlsafe(32)
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        examples = ROOT / 'server/examples'
        unreal_file = examples / 'mat_ssafy_v3_unreal_sim.json'
        config_file = examples / 'field_unreal_v3_sim.json'
        field, mapping = load_team_shelf_mapping(config_file, json.loads(unreal_file.read_text(encoding='utf-8')))
        positions = {node['id']: node['pos'] for node in field['nodes']}
        robots = []
        for robot, twin in (('sim-r1', 'AGV_01'), ('sim-r2', 'AGV_02')):
            x, y = positions[mapping.robots[robot]['node']]
            pose = mapping.pose({'x': x, 'y': y, 'th': 0})
            robots.append({'agv_id': twin, **pose, 'carrying': False, 'interpolating': False})
        slots = [{'slot_id': i, 'state': 'empty', 'product_type': None} for i in range(1, 25)]
        slots[0].update(state='occupied', product_type='milk')
        with tempfile.TemporaryDirectory(prefix='team-world-restart-') as temp, ExitStack() as cleanup:
            folder = Path(temp)
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            null = cleanup.enter_context(open(os.devnull, 'w'))
            ready = folder / 'broker.ready'
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), str(ready)],
                env=broker_env, cwd=ROOT, stdout=null, stderr=null, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(ready))
            messages, subscribed = queue.Queue(), threading.Event()
            observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='restart-' + secrets.token_hex(5),
                                   protocol=mqtt.MQTTv311, reconnect_on_failure=False)
            observer.on_connect = lambda client, _u, _f, _r, _p: client.subscribe('agv/+/order', qos=1)
            observer.on_subscribe = lambda _c, _u, _m, _r, _p: subscribed.set()
            observer.on_message = lambda _c, _u, msg: messages.put(msg.payload)
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
            for run in (1, 2):
                log_path = folder / ('server-%s.log' % run)
                with log_path.open('w', encoding='utf-8') as log:
                    server = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app', '--host',
                        '127.0.0.1', '--port', str(server_port), '--workers', '1', '--no-proxy-headers'],
                        cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=flags)
                    try:
                        wait_for_owned_server(self, server, str(log_path), server_port)
                        with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(server_port), proxy=None,
                                     origin='http://127.0.0.1', open_timeout=5,
                                     additional_headers={'X-AGV-Twin-Token': token}) as twin:
                            deadline = time.monotonic() + 15
                            sent = False
                            while time.monotonic() < deadline and not sent:
                                try:
                                    msg = json.loads(twin.recv(timeout=.2))
                                except TimeoutError:
                                    continue
                                if msg.get('type') == 'team.shelf.snapshot.request.v1':
                                    twin.send(json.dumps({'type': 'team.shelf.snapshot.v1', 'data': {
                                        'request_id': msg['data']['request_id'], 'world_id': 'a' * 32,
                                        'robots': robots, 'slots': slots}}))
                                    sent = True
                            self.assertTrue(sent, 'server did not ask for snapshot')
                            if run == 1:
                                deadline = time.monotonic() + 10
                                while time.monotonic() < deadline:
                                    _, state = api(server_port, 'GET', '/order-demo/api/status')
                                    if state.get('accepting_orders'):
                                        break
                                    time.sleep(.05)
                                self.assertTrue(state.get('accepting_orders'), repr(state))
                                code, _ = api(server_port, 'POST', '/order-demo/api/product-orders', 'same-key')
                                self.assertIn(code, (200, 202))
                                messages.get(timeout=10)
                            else:
                                time.sleep(.5)
                                _, state = api(server_port, 'GET', '/order-demo/api/status')
                                self.assertFalse(state.get('accepting_orders'), repr(state))
                                retry_code, retry = api(server_port, 'POST', '/order-demo/api/product-orders', 'same-key')
                                self.assertNotEqual(retry_code, 200, repr(retry))
                                with self.assertRaises(queue.Empty):
                                    messages.get(timeout=3)
                                # A later world must not cause the pre-world retry to
                                # start. The old key may belong to an already-issued
                                # operation, so a failed world cannot queue it for
                                # execution under a different world identity.
                                deadline = time.monotonic() + 6
                                resent = False
                                while time.monotonic() < deadline and not resent:
                                    try:
                                        msg = json.loads(twin.recv(timeout=.2))
                                    except TimeoutError:
                                        continue
                                    if msg.get('type') == 'team.shelf.snapshot.request.v1':
                                        twin.send(json.dumps({'type': 'team.shelf.snapshot.v1', 'data': {
                                            'request_id': msg['data']['request_id'], 'world_id': 'b' * 32,
                                            'robots': robots, 'slots': slots}}))
                                        resent = True
                                self.assertTrue(resent, 'server did not retry snapshot')
                                with self.assertRaises(queue.Empty):
                                    messages.get(timeout=3)
                    finally:
                        stop(server)
                while not messages.empty():
                    messages.get_nowait()


if __name__ == '__main__':
    unittest.main()
