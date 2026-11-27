"""Loopback HTTP, MQTT and synthetic-world auto reroute integration."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from urllib.request import Request

from server.tests.test_sim_reroute_mqtt import cycle_map
from server.tests.test_synthetic_field_web_integration import submit
from server.tests.test_order_persistent_restart_integration import wait_for
from server.tests.test_order_product_integration import request, HTTP
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker, wait_for_owned_server
from server.tools.twin_multi_test import isolated_environment

ROOT = Path(__file__).resolve().parents[2]


def cycle_fixture(folder):
    data = cycle_map(); data['schema'] = 'agv-map-v1'
    for node in data['nodes']:
        if node['id'] in ('D1', 'D2', 'S1', 'S2'):
            node['uturn'] = True  # Explicit simulated pickup/dropoff turnarounds.
    data['zones'].extend({'id': 'wait-' + node, 'type': 'waiting', 'node': node} for node in ('S1', 'S2'))
    for edge in (data['edges'][3], data['edges'][4]):
        edge['from'], edge['to'] = edge['to'], edge['from']; edge['shape'].reverse()
    data['nodes'].extend([{'id': 'P1', 'pos': [-100, 0]}, {'id': 'P2', 'pos': [200, 200]}])
    data['edges'].extend([
        {'id': 'IN1', 'from': 'P1', 'to': 'S1', 'shape': [[-100, 0], [0, 0]], 'bidirectional': True, 'lanes': 1},
        {'id': 'IN2', 'from': 'S2', 'to': 'P2', 'shape': [[200, 100], [200, 200]], 'bidirectional': True, 'lanes': 1}])
    field = folder / 'field.json'; field.write_text(json.dumps(data), encoding='utf-8')
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True, allow_nan=False,
                                       separators=(',', ':')).encode('ascii')).hexdigest()
    world = {'map_id': 'reroute-world', 'coordinate_unit': 'meter',
             'nodes': [{'id': n['id'], 'x': n['pos'][0] / 1000 + 3,
                        'y': n['pos'][1] / 1000 + 4} for n in data['nodes']],
             'edges': [{'from': e['from'], 'to': e['to'], 'points': []} for e in data['edges']]}
    world_path = folder / 'world.json'; world_path.write_text(json.dumps(world), encoding='utf-8')
    products = [
        {'id': 'red', 'label': 'red', 'product_type': 'Red', 'pickup_node': 'D1', 'destination_node': 'S1',
         'unreal_pickup_node': 'D1', 'unreal_destination_node': 'S1', 'shelf_id': 'SHELF_RED', 'slot_index': 0, 'slot_count': 6},
        {'id': 'blue', 'label': 'blue', 'product_type': 'Blue', 'pickup_node': 'D2', 'destination_node': 'S2',
         'unreal_pickup_node': 'D2', 'unreal_destination_node': 'S2', 'shelf_id': 'SHELF_BLUE', 'slot_index': 0, 'slot_count': 6}]
    config = {'field_map_file': field.name, 'field_map_sha256': digest, 'unreal_map_id': world['map_id'],
              'anchors': [{'field_node': 'W1', 'unreal_node': 'W1'}, {'field_node': 'W2', 'unreal_node': 'W2'}],
              'reflect_y': False, 'tolerance_m': .001,
              'robots': {'sim-r1': {'node': 'S1', 'incoming_edge': 'IN1'},
                         'sim-r2': {'node': 'S2', 'incoming_edge': 'IN2~r'}}, 'products': products}
    config_path = folder / 'field-config.json'; config_path.write_text(json.dumps(config), encoding='utf-8')
    return world_path, config_path


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class AutomaticRerouteWebTest(unittest.TestCase):
    def test_simulated_web_orders_reroute_and_complete_product_actions(self):
        broker_port, server_port = free_port(), free_port()
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='agv-auto-web-') as temp, ExitStack() as cleanup:
            folder = Path(temp); world, config = cycle_fixture(folder); durable = folder / 'state'
            broker_log = cleanup.enter_context((folder / 'broker.log').open('w', encoding='utf-8'))
            server_log = cleanup.enter_context((folder / 'server.log').open('w', encoding='utf-8'))
            broker_env = isolated_environment(os.environ); broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), str(folder / 'ready')],
                cwd=ROOT, env=broker_env, stdout=broker_log, stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker); wait_for_owned_broker(self, broker, str(folder / 'ready'))
            env = isolated_environment({k: v for k, v in os.environ.items() if not k.upper().startswith('AGV_')})
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                'TEMP': temp, 'TMP': temp, 'AGV_ORDER_PERSIST_DIR': str(durable),
                'AGV_ORDER_ACTION_SOURCE': 'synthetic', 'AGV_ORDER_DEMO_ENABLED': '1',
                'AGV_ORDER_COORDINATED': '1', 'AGV_ORDER_FLEET': '1', 'AGV_ORDER_SHELF_SYNC': '1',
                'AGV_ORDER_AUTO_REROUTE': '1', 'AGV_ORDER_DEMO_MAP': str(world),
                'AGV_ORDER_FIELD_CONFIG': str(config), 'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port)})
            env['TEST_HTTP_PORT'] = str(server_port)
            script = '''
import os, sqlite3, traceback, uvicorn
from server.order_pickup_workflow import PickupWorkflow
from server.order_product_control import ProductControlEngine
from server.order_fleet import FleetProductControlEngine
original = PickupWorkflow._unknown
original_mark = ProductControlEngine._mark_unknown
def diagnostic(self):
    print('TEST_WORKFLOW_UNKNOWN', traceback.format_exc(), flush=True)
    return original(self)
PickupWorkflow._unknown = diagnostic
def mark(self):
    print('TEST_ENGINE_UNKNOWN', traceback.format_exc(), flush=True)
    return original_mark(self)
ProductControlEngine._mark_unknown = mark
original_tick = ProductControlEngine.tick_recoverable
def simultaneous_start(self, dt):
    # Test-only scheduling barrier: let both real web orders reserve their
    # first segments before advancing either robot, to exercise contention.
    with sqlite3.connect(self.runtime.path) as db:
        count = db.execute("SELECT COUNT(*) FROM tasks WHERE status='ASSIGNED'").fetchone()[0]
    return self.snapshot() if count < 2 else original_tick(self, dt)
ProductControlEngine.tick_recoverable = simultaneous_start
original_fleet = FleetProductControlEngine.create_product_order
def fleet_diagnostic(self, *args, **kwargs):
    try:
        result = original_fleet(self, *args, **kwargs)
        print('TEST_FLEET_RESULT', repr(result), flush=True)
        return result
    except Exception:
        print('TEST_FLEET_EXCEPTION', traceback.format_exc(), flush=True)
        raise
FleetProductControlEngine.create_product_order = fleet_diagnostic
uvicorn.run('server.order_demo_app:app', host='127.0.0.1', port=int(os.environ['TEST_HTTP_PORT']),
            workers=1, proxy_headers=False, access_log=False)
'''
            server = subprocess.Popen([python, '-c', script],
                                      cwd=ROOT, env=env, stdout=server_log, stderr=server_log, creationflags=flags)
            cleanup.callback(stop, server)
            def diagnostic():
                server_log.flush(); return (folder / 'server.log').read_text(encoding='utf-8', errors='replace')[-18000:]
            try:
                wait_for_owned_server(self, server, str(folder / 'server.log'), server_port)
            except AssertionError as exc:
                self.fail(str(exc) + '\n' + diagnostic())
            self.assertTrue(wait_for(lambda: request(server_port, '/order-demo/api/status').get('accepting_orders')), diagnostic())
            self.assertEqual(request(server_port, '/order-demo/api/status').get('automatic_reroute'), 'isolated_simulation')
            with ThreadPoolExecutor(max_workers=2) as pool:
                requests = list(pool.map(lambda product: submit(server_port, product), ['red', 'blue']))
            def terminal_orders():
                rows = [request(server_port, '/order-demo/api/orders/' + rid) for rid in requests]
                return rows if (all(row.get('status') == 'completed' for row in rows)
                                or any(row.get('status') in ('failed', 'unknown') for row in rows)) else None
            results = wait_for(terminal_orders, timeout=90)
            if results is None:
                results = [request(server_port, '/order-demo/api/orders/' + rid) for rid in requests]
            self.assertTrue(all(row is not None and row['status'] == 'completed' for row in results),
                            'orders={!r}\n{}'.format(results, diagnostic()))
            with closing(sqlite3.connect(durable / 'simulation.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_finishes').fetchone(), (1,))
                self.assertEqual(db.execute('SELECT status,COUNT(*) FROM tasks GROUP BY status ORDER BY status').fetchall(),
                                 [('ASSIGNED', 2), ('DONE', 2)])
            with closing(sqlite3.connect(durable / 'orders.sqlite3')) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM orders WHERE state='completed'").fetchone(), (2,))
            third_request = Request('http://127.0.0.1:{}/order-demo/api/product-orders'.format(server_port),
                headers={'Host': '127.0.0.1:8000', 'Origin': 'http://127.0.0.1:8000',
                         'Content-Type': 'application/json', 'Idempotency-Key': 'auto-reroute-next-red'},
                data=json.dumps({'product_id': 'red'}).encode('utf-8'))
            with HTTP.open(third_request, timeout=3) as response:
                third_id = json.load(response)['request_id']
            def third_terminal():
                row = request(server_port, '/order-demo/api/orders/' + third_id)
                return row if row.get('status') in ('completed', 'failed', 'unknown') else None
            third = wait_for(third_terminal, timeout=90)
            self.assertEqual((third or {}).get('status'), 'completed', '{}\n{}'.format(third, diagnostic()))
            with closing(sqlite3.connect(durable / 'orders.sqlite3')) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM orders WHERE state='completed'").fetchone(), (3,))


if __name__ == '__main__':
    unittest.main()
