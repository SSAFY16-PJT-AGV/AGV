"""Simulated simultaneous orders with live shelf state over a real WS socket.

The broker, web server, SQLite databases, and Unreal double belong exclusively
to this test. This does not exercise a real robot or Unreal Editor.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
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
from urllib.request import ProxyHandler, Request, build_opener

from websockets.sync.client import connect

from server.tests.test_order_fleet_integration import scenario_map
from server.tests.test_order_product_integration import action_reply, request
from server.tests.test_twin_broker_integration import (BROKER, free_port, stop,
    wait_for_owned_broker, wait_for_owned_server)
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]
HTTP = build_opener(ProxyHandler({}))


def preserve_failure(folder, unreal, exc):
    """Preserve only this test's synthetic diagnostics before temp cleanup."""
    target = ROOT / '.venv' / ('multi-slot-failure-' + secrets.token_hex(5))
    target.mkdir()
    for name in ('server.log', 'warehouse_map.json'):
        src = Path(folder) / name
        if src.is_file():
            shutil.copyfile(src, target / name)
    databases = list(Path(folder).glob('agv-web-workflow-*/server.sqlite3'))
    if len(databases) == 1 and databases[0].is_file():
        with closing(sqlite3.connect(databases[0])) as src, closing(sqlite3.connect(target / 'server.sqlite3')) as dst:
            src.backup(dst)
    with unreal.lock:
        evidence = {'error': repr(exc), 'actions': list(unreal.actions),
                    'snapshots': list(unreal.snapshots), 'stock': dict(unreal.stock),
                    'slots': dict(unreal.slots), 'errors': list(unreal.errors)}
    (target / 'observed.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(target)


def post_order(port, key):
    req = Request('http://127.0.0.1:{}/order-demo/api/product-orders'.format(port),
                  headers={'Host': '127.0.0.1:8000', 'Origin': 'http://127.0.0.1:8000',
                           'Content-Type': 'application/json', 'Idempotency-Key': key},
                  data=json.dumps({'product_id': 'red'}).encode('utf-8'))
    with HTTP.open(req, timeout=3) as response:
        return response.status, json.load(response)


class FakeUnreal:
    """Stateful shelf and stock double; only successful actions mutate it."""
    def __init__(self, port, token):
        self.port, self.token = port, token
        self.stop_event, self.connected = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.slots = {'SHELF_RED': [True, False, False, True, False, False],
                      'SHELF_BLUE': [False] * 6}
        self.stock = {'Red': 3, 'Blue': 3}
        self.actions, self.snapshots, self.errors = [], [], []
        self.thread = threading.Thread(target=self._run, daemon=True, name='fake-unreal-shelf')

    def start(self):
        self.thread.start()
        if not self.connected.wait(10):
            raise RuntimeError('fake Unreal failed to connect: {!r}'.format(self.errors))

    def stop(self):
        self.stop_event.set()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError('fake Unreal thread failed to stop')

    def _run(self):
        try:
            uri = 'ws://127.0.0.1:{}/ws/digital-twin'.format(self.port)
            with connect(uri, proxy=None, origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': self.token}) as ws:
                self.connected.set()
                while not self.stop_event.is_set():
                    try:
                        message = json.loads(ws.recv(timeout=.1))
                    except TimeoutError:
                        continue
                    if message.get('type') == 'shelf.snapshot.request':
                        data = message['data']
                        with self.lock:
                            shelves = [{'shelf_id': key,
                                        'product_type': {'SHELF_RED': 'Red', 'SHELF_BLUE': 'Blue'}[key],
                                        'slots': list(self.slots[key])} for key in data['shelf_ids']]
                            self.snapshots.append(shelves)
                        ws.send(json.dumps({'type': 'shelf.snapshot',
                                            'data': {'request_id': data['request_id'], 'shelves': shelves}}))
                    elif message.get('type') == 'robot.action':
                        action = message['data']
                        with self.lock:
                            if any(old['action_id'] == action['action_id'] for old in self.actions):
                                raise AssertionError('duplicate Unreal action')
                            if action['action'] == 'grab':
                                product = action['product_type']
                                applied = self.stock.get(product, 0) > 0
                                if applied:
                                    self.stock[product] -= 1
                            elif action['action'] == 'place':
                                shelf, slot = action['shelf_id'], action['slot_index']
                                applied = (shelf in self.slots and type(slot) is int
                                           and 0 <= slot < 6 and not self.slots[shelf][slot])
                                if applied:
                                    self.slots[shelf][slot] = True
                            else:
                                raise AssertionError('unexpected action')
                            self.actions.append(dict(action, applied=applied))
                        ws.send(action_reply(action, applied=applied))
                        if not applied:
                            raise AssertionError('fake Unreal rejected missing stock or occupied slot')
        except BaseException as exc:
            self.errors.append(repr(exc))
            self.connected.set()


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class MultiSlotIntegrationTest(unittest.TestCase):
    def test_three_simultaneous_same_product_orders_use_distinct_empty_slots(self):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        token = secrets.token_urlsafe(32)
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='agv-multi-slot-') as folder, \
                open(os.devnull, 'w', encoding='utf-8') as broker_log, \
                open(Path(folder) / 'server.log', 'w', encoding='utf-8') as server_log, \
                ExitStack() as cleanup:
            map_path = Path(folder) / 'warehouse_map.json'
            map_path.write_text(json.dumps(scenario_map(False)), encoding='utf-8')
            ready = str(Path(folder) / 'broker.ready')
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), ready], cwd=ROOT,
                                      env=broker_env, stdout=broker_log, stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready)
            env = isolated_environment(os.environ)
            for key in list(env):
                if key.upper().startswith('AGV_'):
                    del env[key]
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                        'TEMP': folder, 'TMP': folder,
                        'AGV_ORDER_DEMO_ENABLED': '1', 'AGV_ORDER_COORDINATED': '1',
                        'AGV_ORDER_FLEET': '1', 'AGV_ORDER_SHELF_SYNC': '1',
                        'AGV_ORDER_DEMO_MAP': str(map_path),
                        'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port),
                        'AGV_TWIN_WEBSOCKET': '1', 'AGV_TWIN_UNREAL_TOKEN': token,
                        'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}),
                        'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1'})
            server = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app',
                                       '--host', '127.0.0.1', '--port', str(server_port),
                                       '--workers', '1', '--no-proxy-headers', '--no-access-log'], cwd=ROOT,
                                      env=env, stdout=server_log, stderr=server_log, creationflags=flags)
            cleanup.callback(stop, server)
            wait_for_owned_server(self, server, str(Path(folder) / 'server.log'), server_port)
            unreal = FakeUnreal(server_port, token)
            unreal.start()
            cleanup.callback(unreal.stop)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    self.assertFalse(unreal.errors, unreal.errors)
                    if unreal.snapshots and request(server_port, '/order-demo/api/status').get('accepting_orders'):
                        break
                    time.sleep(.05)
                else:
                    self.fail('authenticated fake Unreal shelf snapshot did not make orders ready')

                barrier = threading.Barrier(3)
                def submit(index):
                    barrier.wait(timeout=5)
                    return post_order(server_port, 'multi-slot-' + str(index))
                with ThreadPoolExecutor(max_workers=3) as pool:
                    submissions = list(pool.map(submit, range(3)))
                self.assertTrue(all(code in (200, 202) for code, _body in submissions), submissions)
                results = []
                for code, body in submissions:
                    if code == 200:
                        results.append(body)
                    else:
                        self.assertEqual(body.get('status'), 'pending')
                pending = {body['request_id'] for code, body in submissions if code == 202}
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    self.assertFalse(unreal.errors, unreal.errors)
                    for request_id in list(pending):
                        data = request(server_port, '/order-demo/api/submissions/' + request_id)
                        self.assertNotIn(data['status'], ('error', 'unknown'), data)
                        if data['status'] == 'completed':
                            results.append(data['result'])
                            pending.remove(request_id)
                    with unreal.lock:
                        placements = [a for a in unreal.actions if a['action'] == 'place']
                    if not pending and len(placements) == 3:
                        state = request(server_port, '/order-demo/api/status')
                        if (not state.get('stale') and all(
                                item.get('status') in ('idle', 'completed') for item in state['robots'])):
                            break
                    time.sleep(.1)
                else:
                    self.fail('three orders did not complete: pending={!r}, actions={!r}, status={!r}'
                              .format(pending, unreal.actions, request(server_port, '/order-demo/api/status')))
                self.assertEqual(len(results), 3)
                self.assertEqual({r['slot_index'] for r in results}, {1, 2, 4})
                pickup_orders = {r['order_id'] for r in results}
                self.assertEqual(len(pickup_orders), 3)
                self.assertEqual({a['slot_index'] for a in placements}, {1, 2, 4})
                self.assertTrue(all(a['shelf_id'] == 'SHELF_RED' and a['applied'] for a in placements))
                self.assertEqual(len([a for a in unreal.actions if a['action'] == 'grab']), 3)
                self.assertEqual(unreal.stock['Red'], 0)
                self.assertEqual(unreal.slots['SHELF_RED'], [True, True, True, True, True, False])
                db_paths = list(Path(folder).glob('agv-web-workflow-*/server.sqlite3'))
                self.assertEqual(len(db_paths), 1)
                with closing(sqlite3.connect(db_paths[0])) as db:
                    finished = {r[0] for r in db.execute(
                        "SELECT task_id FROM progress_events WHERE result='finished'")}
                expected = pickup_orders | {item[:-len('pickup')] + 'delivery' for item in pickup_orders}
                self.assertTrue(expected.issubset(finished), (expected, finished))
            except BaseException as exc:
                server_log.flush()
                tail = (Path(folder) / 'server.log').read_text(encoding='utf-8', errors='replace')[-10000:]
                target = preserve_failure(folder, unreal, exc)
                raise AssertionError('multi-slot integration failed: {!r}; evidence: {}\nserver log:\n{}'
                                     .format(exc, target, tail)) from exc


if __name__ == '__main__':
    unittest.main()
