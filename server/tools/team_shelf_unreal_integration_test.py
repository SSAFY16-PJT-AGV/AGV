"""Four-SKU, six-shelf slot protocol against a real isolated Unreal game.

The game connects only to a test-owned loopback WebSocket. Movement here is
test-injected and is not a physical robot or proof of collision safety.
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

from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_twin_protocol import TeamShelfTwinSession
from server.tools.order_demo import clean_environment
from server.tools.twin_multi_test import ROOT, stop_process
from server.tools.v3_unreal_guard_probe import _matching_reply, _state, node_positions


def _snapshot(ws, timeout=5):
    request_id = uuid.uuid4().hex
    ws.send(json.dumps({'type': 'team.shelf.snapshot.request.v1', 'data': {'request_id': request_id}}))
    return _matching_reply(ws, 'team.shelf.snapshot.v1', 'request_id', request_id, timeout)


def _wait_pose(ws, positions, target, timeout):
    deadline = time.monotonic() + timeout
    next_update = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_update:
            _state(ws, 'AGV_01', target, positions[target])
            _state(ws, 'AGV_02', 'D08', positions['D08'])
            next_update = time.monotonic() + 1.0
            # The UE state handler starts interpolation even for an unchanged
            # target. Let its game thread tick before asking whether it stopped.
            time.sleep(.1)
        try:
            snap = _snapshot(ws, 2)
        except TimeoutError:
            continue
        robots = {row['agv_id']: row for row in snap['robots']}
        pose = robots.get('AGV_01', {})
        if (not pose.get('interpolating') and type(pose.get('x')) in (int, float)
                and type(pose.get('y')) in (int, float)
                and math.hypot(pose['x'] - positions[target][0], pose['y'] - positions[target][1]) <= .05):
            return snap
        time.sleep(.1)
    raise TimeoutError('Unreal did not reach ' + target)


def run(args):
    editor, project = args.unreal_editor.resolve(), args.uproject.resolve()
    world = project.parent / 'Content/Data/warehouse_map.json'
    for path in (editor, project, world):
        if not path.is_file():
            raise FileNotFoundError(path)
    positions = node_positions(world)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 8000))
    folder = args.output_dir.resolve() / ('team-shelf-unreal-' + uuid.uuid4().hex[:10])
    folder.mkdir(parents=True, exist_ok=False)
    token = secrets.token_urlsafe(32)
    connections, disconnected = queue.Queue(), threading.Event()

    def authorize(conn, request):
        if (request.path != '/ws/digital-twin' or
                request.headers.get('X-AGV-Twin-Token') != token):
            return conn.respond(403, 'Forbidden')
        return None

    def handler(ws):
        connections.put(ws)
        disconnected.wait(args.timeout + 30)

    result = {'passed': False, 'scope': 'isolated-four-sku-unreal-protocol', 'evidence_dir': str(folder)}
    print('EVIDENCE', folder, flush=True)
    game = None
    try:
        with serve(handler, '127.0.0.1', 8000, process_request=authorize) as server, ExitStack() as release:
            release.callback(disconnected.set)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            env = clean_environment(os.environ)
            env.update(AGV_TWIN_UNREAL_TOKEN=token, TEMP=str(folder), TMP=str(folder), NO_PROXY='127.0.0.1,localhost')
            cmd = [str(editor), str(project), '/Game/AGV/NewMap', '-game', '-windowed', '-ResX=1200', '-ResY=720',
                   '-nosound', '-NoSplash', '-ABSLOG=' + str(folder / 'unreal.log')]
            if args.ddc:
                cmd.append('-LocalDataCachePath=' + str(args.ddc.resolve()))
            with (folder / 'stdout.log').open('w', encoding='utf-8') as output:
                game = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=output, stderr=output)
                print('UNREAL_STARTING', flush=True)
                try:
                    ws = connections.get(timeout=args.timeout)
                except queue.Empty:
                    raise TimeoutError('isolated Unreal did not connect') from None
                first = _wait_pose(ws, positions, 'D01', args.timeout)
                if not isinstance(first.get('world_id'), str) or len(first.get('slots', [])) != 24:
                    raise AssertionError('Unreal did not provide its complete team slot snapshot')
                initial = {row['slot_id']: row for row in first['slots']}
                for slot, product in enumerate(('milk', 'tissue', 'egg', 'juice'), start=1):
                    if (initial[slot]['state'], initial[slot]['product_type']) != ('occupied', product):
                        raise AssertionError('unexpected source slot ' + str(slot))
                if any(initial[index]['state'] != 'empty' for index in range(5, 25)):
                    raise AssertionError('destination shelves must begin empty')
                journal = TeamShelfOrderSlots.create_new(folder / 'slots.sqlite3')
                session = journal.begin_session(first['world_id'])
                journal.apply_snapshot(session, first['slots'], fence=journal.begin_snapshot(session))
                reached = {'node': 'D01'}
                owner = object()
                twin = TeamShelfTwinSession(journal, session, first['world_id'], owner,
                    lambda request, agv, action, node: reached['node'] == node)
                results = []
                for index, product in enumerate(('milk', 'tissue', 'egg', 'juice')):
                    request = 'pilot-' + product
                    _wait_pose(ws, positions, 'D01', args.timeout)
                    reached['node'] = 'D01'
                    row = journal.reserve(session, request, product)
                    if (row['source_slot_id'], row['destination_slot_id']) != (index + 1, 5 + 4 * index):
                        raise AssertionError('incorrect reserved team slot')
                    pick = twin.begin(request, 'AGV_01', 'pick')
                    if index == 0:
                        # Slot 2 contains tissue; the milk request must fail
                        # without modifying either SKU or destination slots.
                        wrong = dict(pick['data'], action_id=uuid.uuid4().hex, slot_id=2, slot_index=1)
                        ws.send(json.dumps({'type': 'team.shelf.action.v1', 'data': wrong}))
                        nack = _matching_reply(ws, 'team.shelf.action.result.v1', 'action_id', wrong['action_id'], 10)
                        if nack.get('applied') is not False:
                            raise AssertionError('wrong source SKU must be rejected')
                        after_wrong = _snapshot(ws)
                        if after_wrong['slots'] != first['slots']:
                            raise AssertionError('wrong source SKU changed the world')
                    ws.send(json.dumps(pick))
                    ack = _matching_reply(ws, 'team.shelf.action.result.v1', 'action_id', pick['data']['action_id'], 10)
                    if ack.get('applied') is not True or not twin.receive(owner, {'type': 'team.shelf.action.result.v1', 'data': ack}):
                        raise AssertionError('matching pick failed')
                    target = row['destination_node']
                    _wait_pose(ws, positions, target, args.timeout)
                    reached['node'] = target
                    place = twin.begin(request, 'AGV_01', 'place')
                    ws.send(json.dumps(place))
                    ack = _matching_reply(ws, 'team.shelf.action.result.v1', 'action_id', place['data']['action_id'], 10)
                    if ack.get('applied') is not True or not twin.receive(owner, {'type': 'team.shelf.action.result.v1', 'data': ack}):
                        raise AssertionError('matching place failed')
                    results.append({'product_type': product, 'source_slot': row['source_slot_id'],
                                    'destination_slot': row['destination_slot_id']})
                final = _snapshot(ws)
                final_slots = {row['slot_id']: row for row in final['slots']}
                if any(final_slots[index]['state'] != 'empty' for index in range(1, 5)):
                    raise AssertionError('source slots were not emptied')
                for index, product in enumerate(('milk', 'tissue', 'egg', 'juice')):
                    row = final_slots[5 + 4 * index]
                    if (row['state'], row['product_type']) != ('occupied', product):
                        raise AssertionError('destination slot differs for ' + product)
                result.update(passed=True, wrong_sku_rejected=True, orders=results, before=first, after=final)
                print('TEAM_SHELF_UNREAL_PASSED', flush=True)
                time.sleep(args.hold_seconds)
    except BaseException as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        disconnected.set()
        stop_process(game)
        (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('EVIDENCE', folder, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unreal-editor', type=Path, required=True)
    parser.add_argument('--uproject', type=Path, required=True)
    parser.add_argument('--ddc', type=Path)
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.venv/team-shelf-unreal')
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--hold-seconds', type=float, default=3)
    args = parser.parse_args()
    if args.timeout <= 0 or args.hold_seconds < 0:
        parser.error('invalid timeout')
    run(args)


if __name__ == '__main__':
    main()
