"""An acknowledged slot and interrupted web write remain blocked but readable."""
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
import unittest
from urllib.request import Request

import paho.mqtt.client as mqtt

from server.sim_handoff_store import SimHandoffStore
from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_persistent_orders import TeamShelfPersistentOrders
from server.team_shelf_web_mapping import load_team_shelf_mapping
from server.team_shelf_world_registry import TeamShelfWorldRegistry
from server.tests.test_team_shelf_order_slots import snapshot
from server.tests.test_twin_broker_integration import connect_publisher, free_port, stop_publisher
from server.tools.order_demo import clean_environment
from server.tools.twin_multi_test import BROKER_SCRIPT, HTTP, ROOT, stop_process, wait_for_ready_log


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class PartialCommitRestartTest(unittest.TestCase):
    def test_completed_slot_interrupted_order_is_readable_but_not_republished(self):
        with tempfile.TemporaryDirectory(prefix='team-slot-partial-') as tmp:
            folder = Path(tmp)
            state = folder / 'state'
            state.mkdir()
            world_path = ROOT / 'server/examples/mat_ssafy_v3_unreal_sim.json'
            config = ROOT / 'server/examples/field_unreal_v3_sim.json'
            field, mapping = load_team_shelf_mapping(config, json.loads(world_path.read_text(encoding='utf-8')))
            slots = TeamShelfOrderSlots.create_new(state / 'team-slots.sqlite3')
            session = slots.begin_session('a' * 32)
            slots.apply_snapshot(session, snapshot(), fence=slots.begin_snapshot(session))
            journal = TeamShelfPersistentOrders(state / 'orders.sqlite3', mapping.identity, mapping.products, slots)
            journal.bind_world_id('a' * 32)
            positions = {robot: row['node'] for robot, row in mapping.robots.items()}
            simulation = SimHandoffStore.create_new(state / 'simulation.sqlite3',
                                                     simulation_enabled=True, idle_positions=positions)
            journal.bind_simulation_id(simulation.creation_id)
            registry_path = folder / 'registry.sqlite3'
            TeamShelfWorldRegistry(registry_path).claim_persistent('a' * 32, slots.creation_id)
            order = journal.submit('milk', 'partial-key')
            request_id = order['request_id']
            journal.claim(request_id)
            slots.reserve(session, request_id, 'milk')
            journal.mark_active(request_id, {'assigned_robot': 'sim-r1', 'order_id': 'web-pickup',
                'shelf_id': 'N02', 'slot_index': 0, 'reservation_id': request_id})
            slots.mark_dispatched(session, request_id, 'pick', 'pick-action')
            slots.complete_action(session, request_id, 'pick-action', True)
            slots.mark_dispatched(session, request_id, 'place', 'place-action')
            slots.complete_action(session, request_id, 'place-action', True)
            # Simulate termination after the durable slot write but before
            # the separate order DB's mark_completed transaction.
            self.assertEqual(slots.get(request_id)['state'], 'completed')
            self.assertEqual(journal.get_order(request_id)['state'], 'active')
            broker_port, server_port = free_port(), free_port()
            while broker_port == server_port or 8000 in (broker_port, server_port):
                broker_port, server_port = free_port(), free_port()
            python, token = getattr(sys, '_base_executable', sys.executable), secrets.token_urlsafe(32)
            broker_env = clean_environment(os.environ)
            broker_env.update(PYTHONPATH=os.environ['AMQTT_TEST_PATH'], TEMP=tmp, TMP=tmp)
            server_env = clean_environment(os.environ)
            server_env.update(PYTHONPATH=os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                TEMP=tmp, TMP=tmp, PYTHON_DOTENV_DISABLED='1', AGV_ORDER_DEMO_ENABLED='1',
                AGV_ORDER_COORDINATED='1', AGV_ORDER_FLEET='1', AGV_ORDER_SHELF_SYNC='1',
                AGV_ORDER_TEAM_SHELVES='1', AGV_ORDER_TEAM_WORLD_REGISTRY=str(registry_path),
                AGV_ORDER_PERSIST_DIR=str(state), AGV_ORDER_DEMO_MAP=str(world_path),
                AGV_ORDER_FIELD_CONFIG=str(config), MQTT_HOST='127.0.0.1', MQTT_PORT=str(broker_port),
                AGV_TWIN_WEBSOCKET='1', AGV_TWIN_UNREAL_TOKEN=token,
                AGV_TWIN_ROBOT_MAP=json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}))
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            broker = server = observer = None
            try:
                with (folder / 'broker.log').open('w', encoding='utf-8') as blog, \
                     (folder / 'server.log').open('w', encoding='utf-8') as slog:
                    broker = subprocess.Popen([python, '-c', BROKER_SCRIPT, str(broker_port)], cwd=ROOT,
                        env=broker_env, stdout=blog, stderr=blog, creationflags=flags)
                    wait_for_ready_log(broker, folder / 'broker.log', '[AGV_ISOLATED_BROKER_READY]:' + str(broker.pid), 'broker')
                    received, subscribed = queue.Queue(), threading.Event()
                    observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='partial-' + secrets.token_hex(5),
                                           protocol=mqtt.MQTTv311, reconnect_on_failure=False)
                    observer.on_connect = lambda c, _u, _f, _r, _p: c.subscribe('agv/+/order', qos=1)
                    observer.on_subscribe = lambda _c, _u, _m, _r, _p: subscribed.set()
                    observer.on_message = lambda _c, _u, m: received.put(m.payload)
                    connect_publisher(observer, broker_port)
                    observer.loop_start()
                    self.assertTrue(subscribed.wait(10))
                    server = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app', '--host',
                        '127.0.0.1', '--port', str(server_port), '--workers', '1', '--no-proxy-headers'],
                        cwd=ROOT, env=server_env, stdout=slog, stderr=slog, creationflags=flags)
                    wait_for_ready_log(server, folder / 'server.log',
                        'INFO:     Started server process [{}]'.format(server.pid), 'server', timeout=30)
                    wait_for_ready_log(server, folder / 'server.log',
                        'INFO:     Uvicorn running on http://127.0.0.1:{} (Press CTRL+C to quit)'.format(server_port),
                        'server listener', timeout=30)
                    def get(path, headers=None):
                        request_headers = {'Host': '127.0.0.1:8000'}
                        request_headers.update(headers or {})
                        with HTTP.open(Request('http://127.0.0.1:{}{}'.format(server_port, path),
                                               headers=request_headers), timeout=3) as response:
                            return json.load(response)
                    saved = get('/order-demo/api/orders/' + request_id)
                    self.assertEqual(saved['status'], 'unknown')
                    lookup = get('/order-demo/api/submission-lookup', {'Idempotency-Key': 'partial-key'})
                    self.assertEqual((lookup['status'], lookup['request_id']), ('unknown', request_id))
                    inventory = get('/order-demo/api/inventory')
                    self.assertFalse(inventory['fresh'])
                    self.assertTrue(all(cell is None for shelf in inventory['shelves'].values()
                                        for cell in shelf['slots']))
                    with self.assertRaises(queue.Empty):
                        received.get(timeout=3)
            finally:
                stop_process(server)
                if observer is not None:
                    stop_publisher(observer)
                stop_process(broker)


if __name__ == '__main__':
    unittest.main()
