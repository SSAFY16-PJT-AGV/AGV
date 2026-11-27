"""Test a web order through isolated MQTT and a rendered Unreal team shelf.

This tool owns its broker, server, game and a new transient slot DB. It never
contacts physical robots, never retries an uncertain HTTP order, and does not
infer visual verification from the resulting logs.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import sqlite3
import subprocess
import sys
import sysconfig
import threading
import time
import uuid

import paho.mqtt.client as mqtt

from server.tests.test_order_product_integration import request
from server.tests.test_twin_broker_integration import connect_publisher, stop_publisher
from server.tools.order_demo import clean_environment, make_server_environment, wait_for_demo
from server.tools.twin_multi_test import BROKER_SCRIPT, ROOT, stop_process, wait_for_ready_log
from server.tools.v3_rendered_test import _wait, read_unreal_messages


def slot_data(run_dir):
    paths = list(run_dir.rglob('team-slots.sqlite3'))
    if len(paths) != 1:
        raise ValueError('one owned slot database is required')
    with sqlite3.connect(paths[0].as_uri() + '?mode=ro', uri=True, timeout=3) as db:
        slots = db.execute('SELECT slot_id,state,product_type FROM slots ORDER BY slot_id').fetchall()
        orders = db.execute('SELECT product_type,source_slot_id,destination_slot_id,state FROM orders').fetchall()
    return slots, orders


def run(args):
    editor, project, deps = args.unreal_editor.resolve(), args.uproject.resolve(), args.broker_deps.resolve()
    world = project.parent / 'Content/Data/warehouse_map.json'
    field = ROOT / 'server/examples/field_unreal_v3_sim.json'
    for path in (editor, project, world, field):
        if not path.is_file():
            raise FileNotFoundError(path)
    if not (deps / 'amqtt').is_dir():
        raise ValueError('isolated broker dependencies required')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 8000))
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        broker_port = s.getsockname()[1]
    folder = args.output_dir.resolve() / ('team-web-ue-' + uuid.uuid4().hex[:10])
    folder.mkdir(parents=True, exist_ok=False)
    python, token = getattr(sys, '_base_executable', sys.executable), secrets.token_urlsafe(32)
    broker_env = clean_environment(os.environ)
    broker_env.update(PYTHONPATH=str(deps), TEMP=str(folder), TMP=str(folder))
    server_env = make_server_environment(os.environ, broker_port, token, world)
    server_env.update(PYTHONPATH=os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
        TEMP=str(folder), TMP=str(folder), AGV_ORDER_FIELD_CONFIG=str(field), AGV_ORDER_TEAM_SHELVES='1',
        AGV_ORDER_TEAM_WORLD_REGISTRY=str(folder / 'world-registry.sqlite3'))
    game_env = clean_environment(os.environ)
    game_env.update(AGV_TWIN_UNREAL_TOKEN=token, TEMP=str(folder), TMP=str(folder), NO_PROXY='127.0.0.1,localhost')
    children = {'broker': None, 'server': None, 'unreal': None}
    observer = None
    messages, subscribed = queue.Queue(), threading.Event()
    result = {'passed': False, 'rendered': True, 'visual_observation': 'not_automated',
              'scope': 'isolated-web-mqtt-unreal-team-shelves', 'evidence_dir': str(folder)}
    print('EVIDENCE', folder, flush=True)
    try:
        with (folder / 'broker.log').open('w', encoding='utf-8') as blog, \
             (folder / 'server.log').open('w', encoding='utf-8') as slog, \
             (folder / 'game-stdout.log').open('w', encoding='utf-8') as glog:
            broker = children['broker'] = subprocess.Popen([python, '-c', BROKER_SCRIPT, str(broker_port)],
                cwd=ROOT, env=broker_env, stdout=blog, stderr=blog, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            wait_for_ready_log(broker, folder / 'broker.log', '[AGV_ISOLATED_BROKER_READY]:' + str(broker.pid), 'broker')
            observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='team-ue-observer-' + secrets.token_hex(5),
                                   protocol=mqtt.MQTTv311, reconnect_on_failure=False)
            observer.on_connect = lambda client, _u, _f, _r, _p: client.subscribe('agv/+/order', qos=1)
            observer.on_subscribe = lambda _c, _u, _m, _r, _p: subscribed.set()
            observer.on_message = lambda _c, _u, msg: messages.put((msg.topic, json.loads(msg.payload)))
            connect_publisher(observer, broker_port)
            observer.loop_start()
            if not subscribed.wait(10):
                raise TimeoutError('owned MQTT observer did not subscribe')
            server = children['server'] = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app',
                '--host', '127.0.0.1', '--port', '8000', '--workers', '1', '--no-proxy-headers'],
                cwd=ROOT, env=server_env, stdout=slog, stderr=slog, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            wait_for_demo(server, folder / 'server.log')
            command = [str(editor), str(project), '/Game/AGV/NewMap', '-game', '-windowed', '-ResX=1200',
                       '-ResY=720', '-nosound', '-NoSplash', '-ABSLOG=' + str(folder / 'unreal.log')]
            if args.ddc:
                command.append('-LocalDataCachePath=' + str(args.ddc.resolve()))
            children['unreal'] = subprocess.Popen(command, cwd=ROOT, env=game_env, stdout=glog, stderr=glog)
            print('UNREAL_STARTING', flush=True)
            def ready():
                status = request(8000, '/order-demo/api/status')
                return status if status.get('accepting_orders') else None
            _wait(ready, args.start_timeout, 'Unreal world did not become ready', children)
            before, _ = slot_data(folder)
            specs = [('milk', 1, 5)] + ([('tissue', 2, 9)] if args.two_orders else [])
            if len(before) != 24 or any(before[src - 1] != (src, 'occupied', product) or
                    before[dst - 1] != (dst, 'empty', None) for product, src, dst in specs):
                raise AssertionError('initial Unreal slots do not match the selected products')
            # POST once. The request helper resolves 202 using GET and refuses
            # unknown outcomes without repeating the order.
            def submit(spec):
                return request(8000, '/order-demo/api/product-orders', {'product_id': spec[0]})
            with ThreadPoolExecutor(max_workers=len(specs)) as pool:
                orders_received = list(pool.map(submit, specs))
            assigned = [order.get('assigned_robot') for order in orders_received]
            if any(robot not in ('sim-r1', 'sim-r2') for robot in assigned) or len(set(assigned)) != len(specs):
                raise AssertionError('orders must be assigned to distinct simulated robots')
            result['orders'] = orders_received
            print('ORDERS_SUBMITTED', *assigned, flush=True)
            def terminal():
                status = request(8000, '/order-demo/api/status')
                selected = [next((item for item in status.get('robots', []) if item.get('robot_id') == robot), {})
                            for robot in assigned]
                if any(item.get('status') in ('failed', 'unknown') for item in selected):
                    raise RuntimeError('order ended in failure or unknown: ' + repr(selected))
                return selected if all(item.get('status') == 'completed' for item in selected) else None
            result['robots'] = _wait(terminal, args.order_timeout, 'orders did not complete', children)
            after, orders = slot_data(folder)
            if any(after[src - 1] != (src, 'empty', None) or after[dst - 1] != (dst, 'occupied', product)
                   or (product, src, dst, 'completed') not in orders for product, src, dst in specs):
                raise AssertionError('slot DB does not confirm each selected transfer')
            received = read_unreal_messages(folder / 'unreal.log')
            actions = [m['data'] for m in received if m.get('type') == 'team.shelf.action.v1']
            for product, source, destination in specs:
                matching = [(a['action'], a['slot_id']) for a in actions if a['product_type'] == product]
                if matching != [('pick', source), ('place', destination)]:
                    raise AssertionError('Unreal did not receive the two exact slot actions: ' + product)
            pose_counts = {}
            for selected in assigned:
                robot = 'AGV_01' if selected == 'sim-r1' else 'AGV_02'
                poses = [(m['data'].get('x'), m['data'].get('y')) for m in received
                         if m.get('type') == 'robot.state' and m.get('data', {}).get('agv_id') == robot]
                if len(set(poses)) < 2:
                    raise AssertionError('Unreal did not receive distinct positions: ' + robot)
                pose_counts[robot] = len(set(poses))
            published = []
            while not messages.empty():
                published.append(messages.get_nowait())
            if len(published) < 2 * len(specs) or not all(payload.get('map_id') == 'mat_ssafy_v3@v0' for _, payload in published):
                raise AssertionError('existing simulated MQTT order workflow was not observed')
            result.update(passed=True, before_slots=before, after_slots=after, actions=actions,
                          mqtt_orders=len(published), unreal_pose_counts=pose_counts)
            print('TEAM_WEB_UNREAL_PASSED', flush=True)
            time.sleep(args.hold_seconds)
    except BaseException as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        for name in ('unreal', 'server', 'broker'):
            stop_process(children[name])
        if observer is not None:
            stop_publisher(observer)
        (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('EVIDENCE', folder, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unreal-editor', type=Path, required=True)
    parser.add_argument('--uproject', type=Path, required=True)
    parser.add_argument('--ddc', type=Path)
    parser.add_argument('--broker-deps', type=Path, default=ROOT / '.venv/broker-deps')
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.venv/team-web-ue')
    parser.add_argument('--start-timeout', type=float, default=300)
    parser.add_argument('--order-timeout', type=float, default=240)
    parser.add_argument('--hold-seconds', type=float, default=3)
    parser.add_argument('--two-orders', action='store_true', help='submit milk and tissue concurrently')
    args = parser.parse_args()
    if min(args.start_timeout, args.order_timeout) <= 0 or args.hold_seconds < 0:
        parser.error('invalid timeout')
    run(args)


if __name__ == '__main__':
    main()
