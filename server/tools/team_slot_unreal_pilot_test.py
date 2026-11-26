"""Isolated real Unreal probe for the opt-in one-product team-slot pilot.

It never starts a real robot or production MQTT. Milk is represented by the
existing red demo asset; this does not yet implement all four team products.
"""
import argparse
from contextlib import ExitStack
import json
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
from server.tools.v3_unreal_guard_probe import _matching_reply, _snapshot, _stock, _slots, _wait_pose, node_positions


def run(args):
    editor, project = args.unreal_editor.resolve(), args.uproject.resolve()
    world = project.parent / 'Content/Data/warehouse_map.json'
    for path in (editor, project, world):
        if not path.is_file():
            raise FileNotFoundError(path)
    positions = node_positions(world)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 8000))
    folder = args.output_dir.resolve() / ('team-slot-pilot-' + uuid.uuid4().hex[:10])
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

    result = {'passed': False, 'scope': 'isolated-single-product-unreal-pilot', 'evidence_dir': str(folder)}
    print('EVIDENCE', folder, flush=True)
    game = None
    try:
        with serve(handler, '127.0.0.1', 8000, process_request=authorize) as server, ExitStack() as release:
            release.callback(disconnected.set)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            env = clean_environment(os.environ)
            env.update(AGV_TWIN_UNREAL_TOKEN=token, TEMP=str(folder), TMP=str(folder),
                       NO_PROXY='127.0.0.1,localhost')
            cmd = [str(editor), str(project), '/Game/AGV/NewMap', '-game', '-windowed', '-ResX=1200',
                   '-ResY=720', '-nosound', '-NoSplash', '-ABSLOG=' + str(folder / 'unreal.log')]
            if args.ddc:
                cmd.append('-LocalDataCachePath=' + str(args.ddc.resolve()))
            with (folder / 'stdout.log').open('w', encoding='utf-8') as output:
                game = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=output, stderr=output)
                print('UNREAL_STARTING', flush=True)
                try:
                    ws = connections.get(timeout=args.timeout)
                except queue.Empty:
                    raise TimeoutError('isolated Unreal did not connect') from None
                before = _wait_pose(ws, positions, 'D01', args.timeout)
                world_id = before.get('world_id')
                if not isinstance(world_id, str) or _stock(before, 'D01') != 3:
                    raise AssertionError('expected the three tagged pilot items')
                initial = _slots(before, 'SHELF_RED')
                if initial != [False, False, False, False]:
                    raise AssertionError('pilot destination must expose four empty slots')
                slots = TeamShelfOrderSlots.create_new(folder / 'slots.sqlite3')
                session = slots.begin_session(world_id)
                snap = [{'slot_id': i, 'state': 'unknown', 'product_type': None} for i in range(1, 25)]
                for i in (1, 2, 3):
                    snap[i - 1].update(state='occupied', product_type='milk')
                for i in (5, 6, 7, 8):
                    snap[i - 1].update(state='empty')
                slots.apply_snapshot(session, snap, fence=slots.begin_snapshot(session))
                order = slots.reserve(session, 'pilot-1', 'milk')
                if (order['source_slot_id'], order['destination_slot_id']) != (1, 5):
                    raise AssertionError('unexpected simulated slot reservation')
                reached = {'node': 'D01'}
                owner = object()
                twin = TeamShelfTwinSession(slots, session, world_id, owner,
                    lambda request, agv, action, node: request == 'pilot-1' and reached['node'] == node)
                pick = twin.begin('pilot-1', 'AGV_01', 'pick')
                wrong_data = dict(pick['data'], action_id=uuid.uuid4().hex, slot_id=4, slot_index=3)
                ws.send(json.dumps({'type': 'team.shelf.action.v1', 'data': wrong_data}))
                wrong = _matching_reply(ws, 'team.shelf.action.result.v1', 'action_id', wrong_data['action_id'], 10)
                if wrong.get('applied') is not False:
                    raise AssertionError('empty source slot must be rejected')
                unchanged = _snapshot(ws)
                if _stock(unchanged, 'D01') != 3 or _slots(unchanged, 'SHELF_RED') != initial:
                    raise AssertionError('invalid source slot changed the world')
                ws.send(json.dumps(pick))
                picked = _matching_reply(ws, 'team.shelf.action.result.v1', 'action_id', pick['data']['action_id'], 10)
                if not twin.receive(owner, {'type': 'team.shelf.action.result.v1', 'data': picked}):
                    raise AssertionError('slot-correlated pick acknowledgement rejected')
                if picked.get('applied') is not True or _stock(_snapshot(ws), 'D01') != 2:
                    raise AssertionError('reserved source item was not picked once')
                _wait_pose(ws, positions, 'D02', args.timeout)
                reached['node'] = 'D02'
                place = twin.begin('pilot-1', 'AGV_01', 'place')
                ws.send(json.dumps(place))
                placed = _matching_reply(ws, 'team.shelf.action.result.v1', 'action_id', place['data']['action_id'], 10)
                if not twin.receive(owner, {'type': 'team.shelf.action.result.v1', 'data': placed}):
                    raise AssertionError('slot-correlated place acknowledgement rejected')
                after = _snapshot(ws)
                if (placed.get('applied') is not True or _slots(after, 'SHELF_RED') != [True, False, False, False]
                        or slots.get('pilot-1')['state'] != 'completed'):
                    raise AssertionError('reserved destination slot was not filled once')
                result.update(passed=True, source_slot=1, destination_slot=5,
                              incorrect_source_rejected=True, before=before, after=after)
                print('TEAM_SLOT_PILOT_PASSED', flush=True)
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
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.venv/team-slot-unreal-pilot')
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--hold-seconds', type=float, default=15)
    args = parser.parse_args()
    if args.timeout <= 0 or args.hold_seconds < 0:
        parser.error('invalid timeout')
    run(args)


if __name__ == '__main__':
    main()
