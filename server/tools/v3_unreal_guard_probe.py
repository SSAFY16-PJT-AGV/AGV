"""Probe v3 Unreal action guards using a test-owned local WebSocket server.

This is a visible Unreal integration test, not a real-robot test. The server
does not connect to the product server or MQTT and only accepts its generated
token on loopback. A separate built v3 test project is required.
"""
import argparse
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import threading
import time
import uuid

from websockets.sync.server import serve

from server.tools.order_demo import clean_environment
from server.tools.twin_multi_test import ROOT, stop_process


def node_positions(warehouse):
    data = json.loads(Path(warehouse).read_text(encoding='utf-8'))
    if data.get('map_id') != 'mat_ssafy_v3' or data.get('coordinate_unit') != 'meter':
        raise ValueError('the project must load the selected v3 metre map')
    return {node['id']: (node['x'], node['y']) for node in data['nodes']}


def _send(ws, msg_type, data):
    ws.send(json.dumps({'type': msg_type, 'data': data}, separators=(',', ':')))


def _matching_reply(ws, msg_type, key, value, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = json.loads(ws.recv(timeout=max(.05, min(1.0, deadline - time.monotonic()))))
        except TimeoutError:
            continue
        if (isinstance(message, dict) and message.get('type') == msg_type
                and isinstance(message.get('data'), dict) and message['data'].get(key) == value):
            return message['data']
    raise TimeoutError('missing Unreal response: ' + msg_type + ' ' + value)


def _snapshot(ws, timeout=3):
    request_id = uuid.uuid4().hex
    _send(ws, 'shelf.snapshot.request', {'request_id': request_id,
                                        'shelf_ids': ['SHELF_RED', 'SHELF_BLUE']})
    return _matching_reply(ws, 'shelf.snapshot', 'request_id', request_id, timeout)


def _state(ws, robot, node, point):
    _send(ws, 'robot.state', {'agv_id': robot, 'current_node': node, 'status': 'IDLE',
                              'battery': .8, 'x': point[0], 'y': point[1], 'yaw': 0})


def _wait_pose(ws, positions, target, timeout):
    """Wait for an Unreal world snapshot proving its rendered pose is near target."""
    deadline = time.monotonic() + timeout
    next_state = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_state:
            _state(ws, 'AGV_01', target, positions[target])
            _state(ws, 'AGV_02', 'D08', positions['D08'])
            # Repeated identical poses reset UE's interpolation flag, so allow
            # ticks and snapshots between heartbeats.
            next_state = time.monotonic() + 1.0
        try:
            data = _snapshot(ws, timeout=2)
        except TimeoutError:
            continue
        robots = {item['agv_id']: item for item in data['robots']}
        pose = robots.get('AGV_01', {})
        try:
            dist = math.hypot(pose['x'] - positions[target][0], pose['y'] - positions[target][1])
        except (TypeError, KeyError):
            continue
        if not pose.get('interpolating') and dist <= .05:
            return data
        time.sleep(.15)
    raise TimeoutError('Unreal AGV did not reach ' + target)


def _action(ws, action, node, positions, *, shelf=None, wrong_position=False):
    action_id = uuid.uuid4().hex
    x, y = positions[node]
    data = {'agv_id': 'AGV_01', 'order_id': 'v3-isolated-guard-probe',
            'action_id': action_id, 'action': action, 'result': 'success',
            'expected_x': x + (5 if wrong_position else 0), 'expected_y': y}
    if action == 'grab':
        data['product_type'] = 'Red'
    else:
        data.update(shelf_id=shelf, slot_index=0)
    _send(ws, 'robot.action', data)
    result = _matching_reply(ws, 'robot.action.result', 'action_id', action_id, 10)
    if (result.get('order_id') != data['order_id'] or result.get('agv_id') != 'AGV_01'
            or result.get('action') != action or type(result.get('applied')) is not bool):
        raise AssertionError('invalid or unrelated Unreal action result')
    return result


def _stock(snapshot, source):
    return next(item['available'] for item in snapshot['pickups'] if item['source_id'] == source)


def _slots(snapshot, shelf):
    return next(item['slots'] for item in snapshot['shelves'] if item['shelf_id'] == shelf)


def run(args):
    editor, project = args.unreal_editor.resolve(), args.uproject.resolve()
    warehouse = project.parent / 'Content/Data/warehouse_map.json'
    for path in (editor, project, warehouse):
        if not path.is_file():
            raise FileNotFoundError(path)
    if project.suffix.lower() != '.uproject':
        raise ValueError('a separate Unreal test project is required')
    positions = node_positions(warehouse)
    if not {'D01', 'D03', 'D04', 'D06', 'D08'} <= positions.keys():
        raise ValueError('v3 source and shelf nodes are required')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 8000))
    run_dir = args.output_dir.resolve() / ('v3-unreal-guard-' + uuid.uuid4().hex[:10])
    run_dir.mkdir(parents=True, exist_ok=False)
    token = secrets.token_urlsafe(32)
    conn_queue, disconnected = queue.Queue(), threading.Event()

    def authorize(conn, request):
        if (request.path != '/ws/digital-twin'
                or request.headers.get('X-AGV-Twin-Token') != token):
            return conn.respond(403, 'Forbidden')
        return None

    def handler(ws):
        conn_queue.put(ws)
        disconnected.wait(args.timeout + 30)

    game = None
    result = {'passed': False, 'rendered': True, 'visual_observation': 'not_automated',
              'project': str(project), 'evidence_dir': str(run_dir), 'checks': []}
    print('EVIDENCE', run_dir, flush=True)
    try:
        with serve(handler, '127.0.0.1', 8000, process_request=authorize) as server, ExitStack() as release:
            # Release the connection handler before the WebSocket server
            # closes, including when a check raises an exception.
            release.callback(disconnected.set)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            game_env = clean_environment(os.environ)
            game_env.update(AGV_TWIN_UNREAL_TOKEN=token, TEMP=str(run_dir), TMP=str(run_dir),
                            NO_PROXY='127.0.0.1,localhost')
            cmd = [str(editor), str(project), args.level, '-game', '-windowed', '-ResX=1200',
                   '-ResY=720', '-nosound', '-NoSplash', '-ABSLOG=' + str(run_dir / 'unreal.log')]
            if args.ddc:
                cmd.append('-LocalDataCachePath=' + str(args.ddc.resolve()))
            with (run_dir / 'game-stdout.log').open('w', encoding='utf-8') as glog:
                game = subprocess.Popen(cmd, cwd=ROOT, env=game_env, stdout=glog, stderr=glog)
                print('UNREAL_STARTING', flush=True)
                try:
                    ws = conn_queue.get(timeout=args.timeout)
                except queue.Empty:
                    raise TimeoutError('Unreal did not connect to the isolated probe') from None
                before = _wait_pose(ws, positions, 'D01', args.timeout)
                if _stock(before, 'D01') < 1 or _slots(before, 'SHELF_RED')[0]:
                    raise AssertionError('fresh red pickup and empty first shelf slot required')
                result['initial_snapshot'] = before

                wrong_grab = _action(ws, 'grab', 'D01', positions, wrong_position=True)
                if wrong_grab['applied']:
                    raise AssertionError('wrong-position grab was applied')
                after_bad_grab = _snapshot(ws)
                if (_stock(after_bad_grab, 'D01') != _stock(before, 'D01')
                        or _slots(after_bad_grab, 'SHELF_RED') != _slots(before, 'SHELF_RED')):
                    raise AssertionError('wrong-position grab changed the world')
                result['checks'].append({'case': 'wrong_position_grab', 'result': wrong_grab})

                good_grab = _action(ws, 'grab', 'D01', positions)
                if not good_grab['applied']:
                    raise AssertionError('control grab at the correct position failed')
                picked = _snapshot(ws)
                if _stock(picked, 'D01') != _stock(before, 'D01') - 1:
                    raise AssertionError('control grab did not decrease pickup count')
                result['checks'].append({'case': 'correct_grab', 'result': good_grab})

                # Send an internally consistent expected pose while holding
                # red at the *blue* destination. This passes the generic pose
                # guard, so rejection tests the actual red shelf location guard.
                away_from_red = _wait_pose(ws, positions, 'D03', args.timeout)
                remote_red = _action(ws, 'place', 'D03', positions, shelf='SHELF_RED')
                if remote_red['applied']:
                    raise AssertionError('red shelf placement from D03 was applied')
                after_remote = _snapshot(ws)
                for shelf in ('SHELF_RED', 'SHELF_BLUE'):
                    if _slots(after_remote, shelf) != _slots(away_from_red, shelf):
                        raise AssertionError('remote shelf placement changed shelf occupancy')
                robot = next(r for r in after_remote['robots'] if r['agv_id'] == 'AGV_01')
                if not robot['carrying'] or _stock(after_remote, 'D01') != _stock(picked, 'D01'):
                    raise AssertionError('remote shelf placement changed the held product')
                result['checks'].append({'case': 'remote_red_shelf_at_valid_pose', 'result': remote_red})

                on_red_shelf = _wait_pose(ws, positions, 'D04', args.timeout)
                wrong_shelf = _action(ws, 'place', 'D04', positions, shelf='SHELF_BLUE')
                if wrong_shelf['applied']:
                    raise AssertionError('wrong-shelf placement was applied')
                after_wrong_shelf = _snapshot(ws)
                for shelf in ('SHELF_RED', 'SHELF_BLUE'):
                    if _slots(after_wrong_shelf, shelf) != _slots(on_red_shelf, shelf):
                        raise AssertionError('wrong-shelf placement changed shelf occupancy')
                result['checks'].append({'case': 'wrong_shelf_place', 'result': wrong_shelf})

                wrong_place = _action(ws, 'place', 'D04', positions, shelf='SHELF_RED', wrong_position=True)
                if wrong_place['applied']:
                    raise AssertionError('wrong-position placement was applied')
                after_bad_place = _snapshot(ws)
                if _slots(after_bad_place, 'SHELF_RED') != _slots(on_red_shelf, 'SHELF_RED'):
                    raise AssertionError('wrong-position placement changed shelf occupancy')
                result['checks'].append({'case': 'wrong_position_place', 'result': wrong_place})

                good_place = _action(ws, 'place', 'D04', positions, shelf='SHELF_RED')
                if not good_place['applied']:
                    raise AssertionError('control placement at the correct position failed')
                final = _snapshot(ws)
                if not _slots(final, 'SHELF_RED')[0]:
                    raise AssertionError('control placement did not fill shelf slot')
                result['checks'].append({'case': 'correct_place', 'result': good_place})
                result['final_snapshot'], result['passed'] = final, True
                print('GUARDS_VERIFIED: inspect the visible Unreal window', flush=True)
                time.sleep(args.hold_seconds)
    except BaseException as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        disconnected.set()
        stop_process(game)
        (run_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('EVIDENCE', run_dir, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unreal-editor', type=Path, required=True)
    parser.add_argument('--uproject', type=Path, required=True)
    parser.add_argument('--ddc', type=Path)
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.venv/v3-rendered-test')
    parser.add_argument('--level', default='/Game/AGV/NewMap')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--hold-seconds', type=float, default=10)
    args = parser.parse_args()
    if args.timeout <= 0 or args.hold_seconds < 0:
        parser.error('timeout must be positive and hold-seconds nonnegative')
    run(args)


if __name__ == '__main__':
    main()
