"""Run a v3 two-order test against a visible, independently built Unreal game.

All MQTT/HTTP traffic is loopback-only. The specified project must be a
separate v3 test copy, built with the local token and action/snapshot support.
The script does not edit the project and only stops processes it launched.
Successful logs and inventory do not replace visual or physical verification.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import sysconfig
import time
import uuid
from urllib.error import URLError
from urllib.request import Request

from server.field_twin_mapping import load_field_twin_mapping
from server.tools.order_demo import clean_environment, make_server_environment, wait_for_demo
from server.tools.twin_multi_test import BROKER_SCRIPT, HTTP, ROOT, stop_process, wait_for_ready_log


# Fixed source: the selected value is passed as a separate argument, never
# interpolated into Python code. This process only owns the synthetic fleet.
SLOW_SERVER_SOURCE = '''\
import math, sys
from sim import order_executor
import uvicorn
speed = float(sys.argv[1])
if not math.isfinite(speed) or not 1 <= speed <= 1000:
    raise ValueError('simulation speed must be between 1 and 1000 mm/s')
order_executor.SPEED_MM_PER_SECOND = speed
uvicorn.run('server.order_demo_app:app', host='127.0.0.1', port=8000,
            workers=1, proxy_headers=False)
'''


def parse_speed(value):
    speed = float(value)
    if not math.isfinite(speed) or not 1 <= speed <= 1000:
        raise ValueError('simulation speed must be between 1 and 1000 mm/s')
    return speed


def server_command(python, speed):
    if speed is None:
        return [python, '-m', 'uvicorn', 'server.order_demo_app:app', '--host', '127.0.0.1',
                '--port', '8000', '--workers', '1', '--no-proxy-headers']
    if type(speed) not in (float, int):
        raise ValueError('simulation speed must be numeric')
    return [python, '-c', SLOW_SERVER_SOURCE, str(parse_speed(speed))]


def read_unreal_messages(path):
    """Read messages received by the actual test-owned Unreal game."""
    marker = '[WebSocket] Received: '
    messages = []
    for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        if marker in line:
            try:
                value = json.loads(line.split(marker, 1)[1])
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                messages.append(value)
    return messages


def verify_outcome(before, after, products, orders, messages):
    """Check movement and actions received by Unreal plus confirmed inventory."""
    if len(orders) != 2 or {o.get('assigned_robot') for o in orders} != {'sim-r1', 'sim-r2'}:
        raise AssertionError('two orders must be assigned to distinct robots')
    if any(o.get('status') != 'completed' for o in orders):
        raise AssertionError('an order did not complete')
    if not (before.get('fresh') and after.get('fresh') and before.get('pickup', {}).get('fresh')
            and after.get('pickup', {}).get('fresh')):
        raise AssertionError('fresh Unreal inventories required')
    before_pickup, after_pickup = before['pickup']['sources'], after['pickup']['sources']
    observations = {}
    for order in orders:
        product = products[order['product_id']]
        source, shelf, slot = product['pickup_node'], order['shelf_id'], order['slot_index']
        if shelf != product['shelf_id'] or type(slot) is not int or slot < 0:
            raise AssertionError('order references an unexpected shelf or slot')
        if before_pickup[source]['available'] - after_pickup[source]['available'] != 1:
            raise AssertionError('pickup inventory did not decrease exactly once: ' + source)
        oldslots, newslots = before['shelves'][shelf]['slots'], after['shelves'][shelf]['slots']
        if slot >= len(oldslots) or slot >= len(newslots) or oldslots[slot] or not newslots[slot]:
            raise AssertionError('reserved shelf slot was not newly occupied: ' + shelf)
        robot = 'AGV_01' if order['assigned_robot'] == 'sim-r1' else 'AGV_02'
        states = [m['data'] for m in messages if m.get('type') == 'robot.state'
                  and isinstance(m.get('data'), dict) and m['data'].get('agv_id') == robot]
        coords = [(s.get('x'), s.get('y')) for s in states
                  if type(s.get('x')) in (int, float) and type(s.get('y')) in (int, float)
                  and math.isfinite(s['x']) and math.isfinite(s['y'])]
        if len(coords) < 2 or not any(math.dist(coords[0], p) > 0.0001 for p in coords[1:]):
            raise AssertionError('Unreal did not receive a change of position: ' + robot)
        actions = [m['data'] for m in messages if m.get('type') == 'robot.action'
                   and isinstance(m.get('data'), dict) and m['data'].get('agv_id') == robot]
        for action in ('grab', 'place'):
            if not any(a.get('action') == action and a.get('result') == 'success' for a in actions):
                raise AssertionError('Unreal did not receive a successful ' + action + ': ' + robot)
        observations[robot] = {'first_received_pose_m': coords[0], 'last_received_pose_m': coords[-1],
                               'state_messages': len(coords),
                               'actions': [{k: a.get(k) for k in ('action', 'action_id', 'result')}
                                           for a in actions]}
    return observations


def _get(path):
    req = Request('http://127.0.0.1:8000' + path, headers={'Host': '127.0.0.1:8000'})
    with HTTP.open(req, timeout=3) as response:
        return json.load(response)


def _post(product):
    req = Request('http://127.0.0.1:8000/order-demo/api/product-orders',
                  headers={'Host': '127.0.0.1:8000', 'Origin': 'http://127.0.0.1:8000',
                           'Content-Type': 'application/json', 'Idempotency-Key': uuid.uuid4().hex},
                  data=json.dumps({'product_id': product}).encode('utf-8'))
    # If the result is unclear, do not post again.
    with HTTP.open(req, timeout=5) as response:
        value = json.load(response)
    if not isinstance(value.get('request_id'), str):
        raise ValueError('server did not identify the order')
    return value['request_id']


def _wait(fn, timeout, description, children):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, process in children.items():
            if process is not None and process.poll() is not None:
                raise RuntimeError(name + ' exited with code ' + str(process.returncode))
        try:
            value = fn()
            if value:
                return value
        except (OSError, URLError):
            pass
        time.sleep(.15)
    raise TimeoutError(description)


def run(args):
    editor, project = args.unreal_editor.resolve(), args.uproject.resolve()
    field_config, deps = args.field_config.resolve(), args.broker_deps.resolve()
    warehouse = project.parent / 'Content/Data/warehouse_map.json'
    for item in (editor, project, field_config, warehouse):
        if not item.is_file():
            raise FileNotFoundError(item)
    if project.suffix.lower() != '.uproject' or not (deps / 'amqtt').is_dir():
        raise ValueError('valid test project and isolated aMQTT dependencies required')
    field, mapping = load_field_twin_mapping(field_config, json.loads(warehouse.read_text(encoding='utf-8')))
    if field['map_id'] != 'mat_ssafy_v3':
        raise ValueError('this test only accepts the selected v3 map')
    products = {item['id']: item for item in mapping.products}
    if not {'red', 'blue'} <= products.keys():
        raise ValueError('the test requires explicit red and blue product mappings')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 8000))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        broker_port = probe.getsockname()[1]
    run_dir = args.output_dir.resolve() / ('v3-rendered-' + uuid.uuid4().hex[:10])
    run_dir.mkdir(parents=True, exist_ok=False)
    token = secrets.token_urlsafe(32)
    python = getattr(sys, '_base_executable', sys.executable)
    server_cmd = server_command(python, args.sim_speed_mm_s)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    broker_env = clean_environment(os.environ)
    broker_env.update(PYTHONPATH=str(deps), TEMP=str(run_dir), TMP=str(run_dir))
    server_env = make_server_environment(os.environ, broker_port, token, warehouse)
    server_env.update(PYTHONPATH=os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                      TEMP=str(run_dir), TMP=str(run_dir), AGV_ORDER_PERSIST_DIR=str(run_dir / 'state'),
                      AGV_ORDER_FIELD_CONFIG=str(field_config))
    game_env = clean_environment(os.environ)
    game_env.update(AGV_TWIN_UNREAL_TOKEN=token, TEMP=str(run_dir), TMP=str(run_dir),
                    NO_PROXY='127.0.0.1,localhost')
    children = {'broker': None, 'server': None, 'unreal': None}
    result = {'passed': False, 'rendered': True, 'visual_observation': 'not_automated',
              'field_map_id': field['map_id'], 'project': str(project), 'evidence_dir': str(run_dir)}
    if args.sim_speed_mm_s is not None:
        result['simulation_speed_mm_per_second'] = args.sim_speed_mm_s
    print('EVIDENCE', run_dir, flush=True)
    try:
        with (run_dir / 'broker.log').open('w', encoding='utf-8') as blog, \
             (run_dir / 'server.log').open('w', encoding='utf-8') as slog, \
             (run_dir / 'game-stdout.log').open('w', encoding='utf-8') as glog:
            broker = children['broker'] = subprocess.Popen([python, '-c', BROKER_SCRIPT, str(broker_port)],
                cwd=ROOT, env=broker_env, stdout=blog, stderr=blog, creationflags=flags)
            wait_for_ready_log(broker, run_dir / 'broker.log',
                               '[AGV_ISOLATED_BROKER_READY]:' + str(broker.pid), 'broker')
            server = children['server'] = subprocess.Popen(server_cmd, cwd=ROOT, env=server_env,
                                                           stdout=slog, stderr=slog, creationflags=flags)
            wait_for_demo(server, run_dir / 'server.log')
            cmd = [str(editor), str(project), args.level, '-game', '-windowed', '-ResX=1200', '-ResY=720',
                   '-nosound', '-NoSplash', '-ABSLOG=' + str(run_dir / 'unreal.log')]
            if args.ddc:
                cmd.append('-LocalDataCachePath=' + str(args.ddc.resolve()))
            children['unreal'] = subprocess.Popen(cmd, cwd=ROOT, env=game_env, stdout=glog, stderr=glog)
            print('UNREAL_STARTING', flush=True)

            def ready():
                status, inventory = _get('/order-demo/api/status'), _get('/order-demo/api/inventory')
                return inventory if (status.get('accepting_orders') and inventory.get('fresh')
                    and inventory.get('pickup', {}).get('fresh')) else None
            before = _wait(ready, args.start_timeout, 'Unreal world did not become ready', children)
            result['before_inventory'] = before
            print('UNREAL_READY: rendered game window open; two orders will start', flush=True)
            time.sleep(args.observe_delay)
            with ThreadPoolExecutor(max_workers=2) as pool:
                request_ids = list(pool.map(_post, ('red', 'blue')))
            if len(set(request_ids)) != 2:
                raise AssertionError('distinct request IDs required')
            result['request_ids'] = request_ids
            print('ORDERS_SUBMITTED', *request_ids, flush=True)

            def terminal():
                orders = [_get('/order-demo/api/orders/' + rid) for rid in request_ids]
                if any(item.get('status') in ('failed', 'unknown') for item in orders):
                    raise RuntimeError('order ended in failure or unknown: ' + repr(orders))
                return orders if all(item.get('status') == 'completed' for item in orders) else None
            orders = _wait(terminal, args.order_timeout, 'orders did not complete', children)
            after = _get('/order-demo/api/inventory')
            result['orders'], result['after_inventory'] = orders, after
            result['unreal_observations'] = verify_outcome(before, after, products, orders,
                                                          read_unreal_messages(run_dir / 'unreal.log'))
            result['passed'] = True
            print('ORDERS_COMPLETED: inspect the visible Unreal window', flush=True)
            time.sleep(args.hold_seconds)
    except BaseException as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        for name in ('unreal', 'server', 'broker'):
            stop_process(children[name])
        (run_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('EVIDENCE', run_dir, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unreal-editor', type=Path, required=True)
    parser.add_argument('--uproject', type=Path, required=True, help='separate prepared and built v3 test copy')
    parser.add_argument('--field-config', type=Path, required=True)
    parser.add_argument('--ddc', type=Path)
    parser.add_argument('--broker-deps', type=Path, default=ROOT / '.venv/broker-deps')
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.venv/v3-rendered-test')
    parser.add_argument('--level', default='/Game/AGV/NewMap')
    parser.add_argument('--start-timeout', type=float, default=900)
    parser.add_argument('--order-timeout', type=float, default=180)
    parser.add_argument('--observe-delay', type=float, default=5)
    parser.add_argument('--hold-seconds', type=float, default=15)
    parser.add_argument('--sim-speed-mm-s', type=parse_speed,
                        help='optional isolated simulation speed in mm/s, from 1 to 1000')
    args = parser.parse_args()
    if min(args.start_timeout, args.order_timeout) <= 0 or min(args.observe_delay, args.hold_seconds) < 0:
        parser.error('timeouts must be positive and delays must be nonnegative')
    run(args)


if __name__ == '__main__':
    main()
