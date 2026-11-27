"""Six-shelf reroute/restart against a persistent synthetic WebSocket world.

The position hold is an explicitly artificial contention injector. It does not
alter the shared-pickup admission gate or claim anything about real hardware.
"""
from concurrent.futures import ThreadPoolExecutor
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
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from websockets.sync.client import connect
from server.team_shelf_web_mapping import load_team_shelf_mapping
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker, wait_for_owned_server
from server.tools.twin_multi_test import isolated_environment

ROOT = Path(__file__).resolve().parents[2]
HTTP = build_opener(ProxyHandler({}))

# Delay the first simulated delivery at the N04 crossing for up to 60 seconds.
# A second, naturally dispatched robot is then free to encounter a real
# reserved segment. This does not modify reservations or the pickup gate.
HOLD_SERVER = '''\
import os, sqlite3, time, uvicorn
from server.order_product_control import ProductControlEngine
original = ProductControlEngine.tick_recoverable
held = None
second_at_n05 = False
def contention(self, dt):
    global held, second_at_n05
    snap = self.snapshot()
    if self.ROBOT == 'sim-r2' and self._current_leg == 'pickup' and snap.get('current_node') == 'N05':
        second_at_n05 = True
    if self._current_leg == 'delivery' and snap.get('current_node') == 'N04':
        if held is None:
            held = time.monotonic()
            print('TEST_HOLD_START', snap.get('current_node'), flush=True)
        elapsed = time.monotonic() - held
        if elapsed < 60 and not second_at_n05:
            try:
                with sqlite3.connect(self.runtime.path) as db:
                    finish = db.execute('SELECT COUNT(*) FROM sim_reroute_finishes').fetchone()[0]
                if finish:
                    print('TEST_HOLD_RELEASE_REROUTE', flush=True)
                    held = -1e10
                    return original(self, dt)
            except sqlite3.OperationalError:
                pass
            return snap
    return original(self, dt)
ProductControlEngine.tick_recoverable = contention
uvicorn.run('server.order_demo_app:app', host='127.0.0.1', port=int(os.environ['TEST_HTTP_PORT']),
            workers=1, proxy_headers=False, access_log=False)
'''


def api(port, path, *, product=None, key=None):
    headers = {'Host': '127.0.0.1:8000'}
    data = None
    if product is not None:
        headers.update(Origin='http://127.0.0.1:8000', **{'Content-Type': 'application/json',
                       'Idempotency-Key': key})
        data = json.dumps({'product_id': product}).encode()
    elif key is not None:
        headers['Idempotency-Key'] = key
    req = Request('http://127.0.0.1:{}{}'.format(port, path), headers=headers, data=data)
    with HTTP.open(req, timeout=5) as response:
        return json.load(response)


def wait(predicate, timeout, description, child=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child is not None and child.poll() is not None:
            raise RuntimeError('server exited: ' + str(child.returncode))
        try:
            value = predicate()
            if value:
                return value
        except (OSError, HTTPError):
            pass
        time.sleep(.15)
    raise TimeoutError(description)


class World:
    def __init__(self, robots):
        self.robots = robots
        self.slots = [{'slot_id': n, 'state': 'empty', 'product_type': None} for n in range(1, 25)]
        for index, product in enumerate(('milk', 'tissue', 'egg', 'juice')):
            self.slots[index].update(state='occupied', product_type=product)
        self.actions = []
        self.errors = []
        self.stop = threading.Event()
        self.thread = None

    def start(self, port, token):
        self.stop.clear()
        def serve():
            try:
                with connect('ws://127.0.0.1:{}/ws/digital-twin'.format(port), proxy=None,
                             origin='http://127.0.0.1', open_timeout=5,
                             additional_headers={'X-AGV-Twin-Token': token}) as twin:
                    while not self.stop.is_set():
                        try:
                            message = json.loads(twin.recv(timeout=.2))
                        except TimeoutError:
                            continue
                        kind, data = message.get('type'), message.get('data', {})
                        if kind == 'robot.state':
                            self.robots[data['agv_id']].update(x=data['x'], y=data['y'], yaw=data['yaw'])
                        elif kind == 'team.shelf.snapshot.request.v1':
                            twin.send(json.dumps({'type': 'team.shelf.snapshot.v1', 'data': {
                                'request_id': data['request_id'], 'world_id': 'a' * 32,
                                'robots': list(self.robots.values()), 'slots': self.slots}}))
                        elif kind == 'team.shelf.action.v1':
                            slot = self.slots[data['slot_id'] - 1]
                            if data['action'] == 'pick':
                                assert (slot['state'], slot['product_type']) == ('occupied', data['product_type'])
                                slot.update(state='empty', product_type=None)
                                self.robots[data['agv_id']]['carrying'] = True
                            else:
                                assert slot['state'] == 'empty'
                                slot.update(state='occupied', product_type=data['product_type'])
                                self.robots[data['agv_id']]['carrying'] = False
                            self.actions.append((data['product_type'], data['action'], data['slot_id'], data['action_id']))
                            twin.send(json.dumps({'type': 'team.shelf.action.result.v1',
                                                  'data': dict(data, applied=True)}))
            except Exception as exc:
                if not self.stop.is_set():
                    self.errors.append(repr(exc))
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError('test world thread did not stop')


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class TeamShelfRerouteRestartTest(unittest.TestCase):
    def test_completed_reroute_world_survives_server_restart(self):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        examples = ROOT / 'server/examples'
        world_path, config = examples / 'mat_ssafy_v3_unreal_sim.json', examples / 'field_unreal_v3_sim.json'
        field, mapping = load_team_shelf_mapping(config, json.loads(world_path.read_text(encoding='utf-8')))
        positions = {node['id']: node['pos'] for node in field['nodes']}
        robots = {}
        for rid, twinid in (('sim-r1', 'AGV_01'), ('sim-r2', 'AGV_02')):
            x, y = positions[mapping.robots[rid]['node']]
            robots[twinid] = {'agv_id': twinid, **mapping.pose({'x': x, 'y': y, 'th': 0}),
                              'carrying': False, 'interpolating': False}
        world = World(robots)
        python, token = getattr(sys, '_base_executable', sys.executable), secrets.token_urlsafe(32)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='team-reroute-restart-') as tmp, ExitStack() as cleanup:
            folder = Path(tmp); state = folder / 'state'
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            blog = cleanup.enter_context((folder / 'broker.log').open('w', encoding='utf-8'))
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), str(folder / 'ready')],
                cwd=ROOT, env=broker_env, stdout=blog, stderr=blog, creationflags=flags)
            cleanup.callback(stop, broker); wait_for_owned_broker(self, broker, str(folder / 'ready'))
            env = isolated_environment({k: v for k, v in os.environ.items() if not k.upper().startswith('AGV_')})
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                'TEMP': tmp, 'TMP': tmp, 'PYTHON_DOTENV_DISABLED': '1', 'AGV_ORDER_DEMO_ENABLED': '1',
                'AGV_ORDER_COORDINATED': '1', 'AGV_ORDER_FLEET': '1', 'AGV_ORDER_SHELF_SYNC': '1',
                'AGV_ORDER_TEAM_SHELVES': '1', 'AGV_ORDER_AUTO_REROUTE': '1',
                'AGV_ORDER_TEAM_WORLD_REGISTRY': str(folder / 'worlds.sqlite3'), 'AGV_ORDER_PERSIST_DIR': str(state),
                'AGV_ORDER_DEMO_MAP': str(world_path), 'AGV_ORDER_FIELD_CONFIG': str(config),
                'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port), 'AGV_TWIN_WEBSOCKET': '1',
                'AGV_TWIN_UNREAL_TOKEN': token, 'TEST_HTTP_PORT': str(server_port),
                'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'})})
            logs = []
            def start(index, inject):
                path = folder / ('server-{}.log'.format(index))
                log = cleanup.enter_context(path.open('w', encoding='utf-8')); logs.append(path)
                command = ([python, '-c', HOLD_SERVER] if inject else
                           [python, '-m', 'uvicorn', 'server.order_demo_app:app', '--host', '127.0.0.1',
                            '--port', str(server_port), '--workers', '1', '--no-proxy-headers'])
                child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=flags)
                cleanup.callback(stop, child)
                wait_for_owned_server(self, child, str(path), server_port)
                return child
            try:
                server = start(1, True)
                world.start(server_port, token); cleanup.callback(world.close)
                wait(lambda: api(server_port, '/order-demo/api/status').get('accepting_orders'), 40, 'initial world', server)
                keys = [uuid.uuid4().hex, uuid.uuid4().hex]
                first = api(server_port, '/order-demo/api/product-orders', product='juice', key=keys[0])['request_id']
                second = api(server_port, '/order-demo/api/product-orders', product='milk', key=keys[1])['request_id']
                ids = [first, second]
                def done():
                    rows = [api(server_port, '/order-demo/api/orders/' + rid) for rid in ids]
                    if any(row['status'] in ('unknown', 'failed') for row in rows):
                        raise AssertionError(repr(rows))
                    return rows if all(row['status'] == 'completed' for row in rows) else None
                before = wait(done, 180, 'initial two orders', server)
                self.assertEqual({row['assigned_robot'] for row in before}, {'sim-r1', 'sim-r2'})
                with closing(sqlite3.connect(state / 'simulation.sqlite3')) as db:
                    finished = db.execute('SELECT task_id FROM sim_reroute_finishes').fetchall()
                    self.assertTrue(any(task.endswith('pickup') for (task,) in finished),
                                    'no completed pickup reroute')
                    steps = db.execute('SELECT COUNT(*) FROM sim_reroute_multi_steps').fetchone()[0]
                    self.assertGreater(steps, 1, 'expected a multi-segment reroute')
                saved_slots = [dict(row) for row in world.slots]
                action_count = len(world.actions)
                self.assertEqual(action_count, 4)
                world.close(); stop(server)
                server = start(2, False)
                world.start(server_port, token)
                wait(lambda: api(server_port, '/order-demo/api/status').get('accepting_orders'), 45, 'restored world', server)
                restored = [api(server_port, '/order-demo/api/orders/' + rid) for rid in ids]
                self.assertTrue(all(row['status'] == 'completed' for row in restored))
                for rid, key in zip(ids, keys):
                    self.assertEqual(api(server_port, '/order-demo/api/submission-lookup', key=key)['request_id'], rid)
                self.assertEqual(saved_slots, world.slots)
                inventory = api(server_port, '/order-demo/api/inventory')
                self.assertTrue(inventory.get('fresh'), repr(inventory))
                self.assertIs(inventory['shelves']['N05']['slots'][0], True)
                self.assertIs(inventory['shelves']['N02']['slots'][0], True)
                self.assertEqual(action_count, len(world.actions))
                third = api(server_port, '/order-demo/api/product-orders', product='tissue', key=uuid.uuid4().hex)['request_id']
                wait(lambda: api(server_port, '/order-demo/api/orders/' + third).get('status') == 'completed',
                     100, 'follow-up order', server)
                self.assertEqual(len(world.actions), 6)
                self.assertFalse(world.errors, repr(world.errors))
            except BaseException as exc:
                detail = '\n'.join(path.read_text(encoding='utf-8', errors='replace')[-15000:] for path in logs)
                raise AssertionError(str(exc) + '\n' + detail) from exc
            finally:
                world.close()


if __name__ == '__main__':
    unittest.main()
