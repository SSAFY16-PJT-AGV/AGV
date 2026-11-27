"""Exercise a team-shelf server restart against one isolated rendered UE world.

The broker, world, server processes and databases are created only for this
run. A POST is never replayed on an uncertain response. The test observes
logs, API values and storage; it does not prove real robot or physical safety.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import sysconfig
import time
import uuid
from urllib.request import Request

from server.tools.order_demo import clean_environment, make_server_environment, wait_for_demo
from server.tools.team_shelf_rendered_test import slot_data
from server.tools.twin_multi_test import BROKER_SCRIPT, HTTP, ROOT, stop_process, wait_for_ready_log
from server.tools.v3_rendered_test import _get, _wait, read_unreal_messages


def post_once(product, key):
    req = Request('http://127.0.0.1:8000/order-demo/api/product-orders',
                  headers={'Host': '127.0.0.1:8000', 'Origin': 'http://127.0.0.1:8000',
                           'Content-Type': 'application/json', 'Idempotency-Key': key},
                  data=json.dumps({'product_id': product}).encode('utf-8'))
    with HTTP.open(req, timeout=5) as response:
        body = json.load(response)
    request_id = body.get('request_id')
    if not isinstance(request_id, str) or not request_id:
        raise ValueError('the original request was not identified')
    return request_id


def lookup_key(key):
    req = Request('http://127.0.0.1:8000/order-demo/api/submission-lookup',
                  headers={'Host': '127.0.0.1:8000', 'Idempotency-Key': key})
    with HTTP.open(req, timeout=3) as response:
        return json.load(response)


def order_done(request_id):
    result = _get('/order-demo/api/orders/' + request_id)
    if result.get('status') in ('unknown', 'failed'):
        raise RuntimeError('order did not complete: ' + repr(result))
    return result if result.get('status') == 'completed' else None


def poses(status):
    return {robot['robot_id']: {'current_node': robot['current_node'], 'pose': robot['pose']}
            for robot in status['robots']}


def run(args):
    editor, project, deps = args.unreal_editor.resolve(), args.uproject.resolve(), args.broker_deps.resolve()
    world = project.parent / 'Content/Data/warehouse_map.json'
    field = ROOT / 'server/examples/field_unreal_v3_sim.json'
    if any(not path.is_file() for path in (editor, project, world, field)):
        raise FileNotFoundError('editor, project, world map or field mapping missing')
    if not (deps / 'amqtt').is_dir():
        raise ValueError('isolated broker dependencies required')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 8000))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        broker_port = probe.getsockname()[1]
    folder = args.output_dir.resolve() / ('team-restart-ue-' + uuid.uuid4().hex[:10])
    folder.mkdir(parents=True, exist_ok=False)
    python, token = getattr(sys, '_base_executable', sys.executable), secrets.token_urlsafe(32)
    broker_env = clean_environment(os.environ)
    broker_env.update(PYTHONPATH=str(deps), TEMP=str(folder), TMP=str(folder))
    server_env = make_server_environment(os.environ, broker_port, token, world)
    server_env.update(PYTHONPATH=os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
        TEMP=str(folder), TMP=str(folder), AGV_ORDER_FIELD_CONFIG=str(field), AGV_ORDER_TEAM_SHELVES='1',
        AGV_ORDER_TEAM_WORLD_REGISTRY=str(folder / 'world-registry.sqlite3'),
        AGV_ORDER_PERSIST_DIR=str(folder / 'state'))
    game_env = clean_environment(os.environ)
    game_env.update(AGV_TWIN_UNREAL_TOKEN=token, TEMP=str(folder), TMP=str(folder), NO_PROXY='127.0.0.1,localhost')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    children = {'broker': None, 'server': None, 'unreal': None}
    result = {'passed': False, 'scope': 'isolated-rendered-team-shelf-restart',
              'rendered': True, 'visual_observation': 'not_automated', 'evidence_dir': str(folder)}
    print('EVIDENCE', folder, flush=True)
    try:
        with (folder / 'broker.log').open('w', encoding='utf-8') as blog, \
             (folder / 'server-1.log').open('w', encoding='utf-8') as first_log, \
             (folder / 'server-2.log').open('w', encoding='utf-8') as second_log, \
             (folder / 'game-stdout.log').open('w', encoding='utf-8') as glog:
            broker = children['broker'] = subprocess.Popen([python, '-c', BROKER_SCRIPT, str(broker_port)],
                cwd=ROOT, env=broker_env, stdout=blog, stderr=blog, creationflags=flags)
            wait_for_ready_log(broker, folder / 'broker.log', '[AGV_ISOLATED_BROKER_READY]:' + str(broker.pid), 'broker')
            command = [python, '-m', 'uvicorn', 'server.order_demo_app:app', '--host', '127.0.0.1',
                       '--port', '8000', '--workers', '1', '--no-proxy-headers']
            children['server'] = subprocess.Popen(command, cwd=ROOT, env=server_env, stdout=first_log,
                                                   stderr=first_log, creationflags=flags)
            wait_for_demo(children['server'], folder / 'server-1.log')
            game = [str(editor), str(project), '/Game/AGV/NewMap', '-game', '-windowed', '-ResX=1200',
                    '-ResY=720', '-nosound', '-NoSplash', '-ABSLOG=' + str(folder / 'unreal.log')]
            if args.ddc:
                game.append('-LocalDataCachePath=' + str(args.ddc.resolve()))
            children['unreal'] = subprocess.Popen(game, cwd=ROOT, env=game_env, stdout=glog, stderr=glog)
            print('UNREAL_STARTING', flush=True)
            _wait(lambda: _get('/order-demo/api/status').get('accepting_orders'), args.start_timeout,
                  'first server did not become ready', children)
            original_slots, _ = slot_data(folder)
            if original_slots[0] != (1, 'occupied', 'milk') or original_slots[4] != (5, 'empty', None):
                raise AssertionError('initial simulated milk slot differs')
            key = uuid.uuid4().hex
            first_id = post_once('milk', key)
            first = _wait(lambda: order_done(first_id), args.order_timeout, 'first order did not finish', children)
            before, before_orders = slot_data(folder)
            if before[0] != (1, 'empty', None) or before[4] != (5, 'occupied', 'milk'):
                raise AssertionError('first simulated delivery not stored')
            before_status = _get('/order-demo/api/status')
            before_poses = poses(before_status)
            original_game = children['unreal'].pid
            stop_process(children['server'])
            children['server'] = None
            if children['unreal'].poll() is not None or children['broker'].poll() is not None:
                raise RuntimeError('game or broker exited during server restart')
            children['server'] = subprocess.Popen(command, cwd=ROOT, env=server_env, stdout=second_log,
                                                   stderr=second_log, creationflags=flags)
            wait_for_demo(children['server'], folder / 'server-2.log')
            _wait(lambda: _get('/order-demo/api/status').get('accepting_orders'), args.start_timeout,
                  'same Unreal world was not restored', children)
            if children['unreal'].pid != original_game:
                raise AssertionError('Unreal process changed')
            restored = _get('/order-demo/api/orders/' + first_id)
            if restored.get('status') != 'completed' or lookup_key(key).get('request_id') != first_id:
                raise AssertionError('original order or request key was not restored')
            restored_slots, restored_orders = slot_data(folder)
            if restored_slots != before or restored_orders != before_orders:
                raise AssertionError('saved slots or completed order changed on restart')
            after_poses = poses(_get('/order-demo/api/status'))
            if before_poses != after_poses:
                raise AssertionError('simulated robot pose or orientation changed after restart')
            inventory = _get('/order-demo/api/inventory')
            if not inventory.get('fresh') or inventory['shelves']['N02']['slots'][0] is not True:
                raise AssertionError('web inventory did not restore the delivered milk')
            second_id = post_once('tissue', uuid.uuid4().hex)
            second = _wait(lambda: order_done(second_id), args.order_timeout, 'second order did not finish', children)
            final_slots, _ = slot_data(folder)
            if final_slots[1] != (2, 'empty', None) or final_slots[8] != (9, 'occupied', 'tissue'):
                raise AssertionError('second simulated delivery not stored')
            messages = read_unreal_messages(folder / 'unreal.log')
            actions = [m['data'] for m in messages if m.get('type') == 'team.shelf.action.v1']
            for product, src, dst in [('milk', 1, 5), ('tissue', 2, 9)]:
                matching = [(a['action'], a['slot_id']) for a in actions if a.get('product_type') == product]
                if matching != [('pick', src), ('place', dst)]:
                    raise AssertionError('unexpected action count or slot: ' + product)
            result.update(passed=True, first_order=first, second_order=second, restored_order=restored,
                          original_world_process=original_game, before_poses=before_poses,
                          after_poses=after_poses, before_slots=before, after_restart_slots=restored_slots,
                          final_slots=final_slots, unreal_actions=actions)
            print('TEAM_RESTART_UNREAL_PASSED', flush=True)
    except BaseException as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        for name in ('unreal', 'server', 'broker'):
            stop_process(children[name])
        (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('EVIDENCE', folder, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unreal-editor', type=Path, required=True)
    parser.add_argument('--uproject', type=Path, required=True)
    parser.add_argument('--ddc', type=Path)
    parser.add_argument('--broker-deps', type=Path, default=ROOT / '.venv/broker-deps')
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.venv/team-web-restart')
    parser.add_argument('--start-timeout', type=float, default=300)
    parser.add_argument('--order-timeout', type=float, default=240)
    args = parser.parse_args()
    if min(args.start_timeout, args.order_timeout) <= 0:
        parser.error('timeouts must be positive')
    run(args)


if __name__ == '__main__':
    main()
