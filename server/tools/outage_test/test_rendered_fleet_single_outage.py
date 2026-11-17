"""Rendered one-AGV MQTT outage. Only owns local broker, server, game and overlay.

The token-protected test probe reads executor state under the server's existing
lock; it never issues commands. It is not installed in the product server.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
from contextlib import closing
import json
import math
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import sysconfig
import threading
import paho.mqtt.client as mqtt
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from server.tools.order_demo import clean_environment, make_server_environment, wait_for_demo
from server.tools.twin_multi_test import BROKER_SCRIPT, HTTP, stop_process, wait_for_ready_log
from server.tools.outage_test.outage_cleanup import finalize_run

SCRIPT_DIR = Path(__file__).resolve().parent
PROBE = SCRIPT_DIR / 'rendered_single_outage_probe.py'
INDICATOR = ROOT / 'server/tools/outage_indicator.py'
# Assigned when main() starts, not when imported.
RUN = STATUS = ACK = None
HOLD = None
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def record(event, **details):
    value = dict(event=event, time=time.time(), **details)
    with (RUN / 'events.jsonl').open('a', encoding='utf-8') as output:
        output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def get(path, token=None):
    headers = {'Host': '127.0.0.1:8000'}
    if token:
        headers['X-Test-Observer-Token'] = token
    with HTTP.open(Request('http://127.0.0.1:8000' + path, headers=headers), timeout=3) as response:
        return json.load(response)


def get_probe(path, token, timeout=2.0):
    """Retry only the observer's transient lock-contention 503 response."""
    deadline = time.monotonic() + timeout
    retries = 0
    while True:
        try:
            result = get(path, token)
            if retries:
                record('probe_retry_succeeded', path=path, retries=retries)
            return result
        except HTTPError as exc:
            if exc.code != 503:
                raise
            retries += 1
            if time.monotonic() >= deadline:
                record('probe_retry_exhausted', path=path, retries=retries)
                raise
            time.sleep(.05)


def post(product):
    request = Request('http://127.0.0.1:8000/order-demo/api/product-orders',
                      headers={'Host': '127.0.0.1:8000', 'Origin': 'http://127.0.0.1:8000',
                               'Content-Type': 'application/json', 'Idempotency-Key': uuid.uuid4().hex},
                      data=json.dumps({'product_id': product}).encode())
    with HTTP.open(request, timeout=5) as response:
        return json.load(response)


def occupancy():
    with closing(sqlite3.connect(RUN / 'state/simulation.sqlite3', timeout=3)) as db:
        rows = db.execute('SELECT kind,resource_id,task_id FROM allocation_occupancy '
                          'ORDER BY kind,resource_id,task_id').fetchall()
        duplicates = db.execute('SELECT kind,resource_id FROM allocation_occupancy '
                                'GROUP BY kind,resource_id HAVING COUNT(DISTINCT task_id)>1').fetchall()
    if duplicates:
        raise AssertionError('overlapping occupancy: ' + repr(duplicates))
    return rows


def actions():
    logfile = RUN / 'unreal.log'
    if not logfile.exists():
        return []
    result = []
    for line in logfile.read_text(encoding='utf-8', errors='replace').splitlines():
        marker = '[WebSocket] Received: '
        if marker not in line:
            continue
        try:
            message = json.loads(line.split(marker, 1)[1])
        except (ValueError, TypeError):
            continue
        if message.get('type') == 'robot.action':
            data = message['data']
            result.append({key: data.get(key) for key in ('order_id', 'action_id', 'agv_id', 'action')})
    return result


def pose(snapshot):
    return {key: value['executor']['pose'] for key, value in snapshot['robots'].items()}


def xy_moved(current, previous):
    values = [current.get('x'), current.get('y'), previous.get('x'), previous.get('y')]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError('finite observed positions required')
    # A heading update alone is not evidence of translation.
    return math.hypot(values[0] - values[2], values[1] - values[3]) > 0.5


def wait_for(predicate, seconds, label, children, ignore=()):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for name, process in children.items():
            if name not in ignore and process is not None and process.poll() is not None:
                raise RuntimeError(name + ' exited with code ' + str(process.returncode))
        try:
            result = predicate()
            if result:
                return result
        except (OSError, URLError, ValueError, sqlite3.OperationalError):
            pass
        time.sleep(.1)
    raise TimeoutError(label)


def main():
    global RUN, STATUS, ACK, HOLD
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unreal-editor', type=Path, required=True,
                        help='Path to UnrealEditor.exe for the prepared Unreal project')
    parser.add_argument('--uproject', type=Path, required=True,
                        help='Path to the patched and built agvSimulation.uproject')
    parser.add_argument('--ddc', type=Path, help='Optional Unreal local derived-data cache directory')
    parser.add_argument('--broker-deps', type=Path, default=ROOT / '.venv/broker-deps',
                        help='Directory containing isolated aMQTT dependencies')
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.venv/outage-test',
                        help='Parent directory for a new, unique local evidence folder')
    args = parser.parse_args()
    editor = args.unreal_editor.resolve()
    project = args.uproject.resolve()
    warehouse_map = project.parent / 'Content/Data/warehouse_map.json'
    broker_deps = args.broker_deps.resolve()
    for file in (editor, warehouse_map, project, PROBE, INDICATOR):
        if not file.is_file():
            raise FileNotFoundError(file)
    if not (broker_deps / 'amqtt').is_dir():
        raise FileNotFoundError(f'aMQTT not found in broker dependencies: {broker_deps}')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 8000))
    RUN = args.output_dir.resolve() / ('rendered-fleet-single-outage-' + uuid.uuid4().hex[:10])
    RUN.mkdir(parents=True, exist_ok=False)
    STATUS = RUN / 'indicator-state.json'
    ACK = RUN / 'indicator-ack.json'
    HOLD = RUN / 'sim-r2-reconnect-gate.txt'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        broker_port = probe.getsockname()[1]
    if broker_port == 8000:
        raise RuntimeError('broker port collision')
    token, observer_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    python = getattr(sys, '_base_executable', sys.executable)
    broker_env = clean_environment(os.environ)
    broker_env.update(PYTHONPATH=str(broker_deps), TEMP=str(RUN), TMP=str(RUN))
    server_env = make_server_environment(os.environ, broker_port, token, warehouse_map)
    server_env.update(PYTHONPATH=os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                      TEMP=str(RUN), TMP=str(RUN), AGV_ORDER_PERSIST_DIR=str(RUN / 'state'),
                      AGV_TEST_OBSERVER_TOKEN=observer_token)
    game_env = clean_environment(os.environ)
    game_env.update(AGV_TWIN_UNREAL_TOKEN=token, TEMP=str(RUN), TMP=str(RUN),
                    NO_PROXY='127.0.0.1,localhost')
    HOLD.write_text('held', encoding='utf-8')
    server_env['AGV_TEST_R2_RECONNECT_GATE'] = str(HOLD)
    takeover = None
    children = dict(broker=None, server=None, game=None, indicator=None)
    result = {'rendered': True, 'fault': 'sim-r2 MQTT duplicate-client disconnect', 'fault_injection': 'test-only r2 reconnect held until released', 'simulation_speed_mm_per_second': 400,
              'evidence_dir': str(RUN), 'passed': False}
    status_seq = 0

    def show(phase, message):
        nonlocal status_seq
        status_seq += 1
        pending = STATUS.with_suffix('.tmp')
        pending.write_text(json.dumps({'seq': status_seq, 'phase': phase, 'message': message,
                                       'at': time.strftime('%H:%M:%S')}, ensure_ascii=False), encoding='utf-8')
        os.replace(pending, STATUS)
        def acknowledged():
            try:
                ack = json.loads(ACK.read_text(encoding='utf-8'))
                return ack if (ack.get('event') == 'shown' and ack.get('seq') == status_seq
                               and ack.get('phase') == phase) else None
            except (OSError, ValueError):
                return None
        wait_for(acknowledged, 8, 'indicator failed to show phase', {'indicator': children['indicator']})
        record('indicator_shown', phase=phase, seq=status_seq, message=message)

    record('initializing', evidence_dir=str(RUN), broker_port=broker_port)
    try:
        with (RUN / 'broker.log').open('w', encoding='utf-8') as blog, \
             (RUN / 'server.log').open('w', encoding='utf-8') as slog, \
             (RUN / 'game-stdout.log').open('w', encoding='utf-8') as glog, \
             (RUN / 'indicator.log').open('w', encoding='utf-8') as ilog:
            indicator = children['indicator'] = subprocess.Popen(
                [sys.executable, str(INDICATOR), str(STATUS), str(ACK)], cwd=ROOT,
                stdout=ilog, stderr=ilog)
            def indicator_ready():
                try:
                    return json.loads(ACK.read_text(encoding='utf-8')).get('event') == 'ready'
                except (OSError, ValueError):
                    return False
            wait_for(indicator_ready, 15, 'indicator did not open', children)
            show('preparing', '격리 시험 준비 중')

            def start_broker():
                child = subprocess.Popen([python, '-c', BROKER_SCRIPT, str(broker_port)], cwd=ROOT,
                                         env=broker_env, stdout=blog, stderr=blog, creationflags=FLAGS)
                children['broker'] = child
                wait_for_ready_log(child, RUN / 'broker.log',
                                   '[AGV_ISOLATED_BROKER_READY]:' + str(child.pid), 'broker')
                return child

            broker = start_broker()
            server = children['server'] = subprocess.Popen([python, str(PROBE)], cwd=ROOT, env=server_env,
                                                           stdout=slog, stderr=slog, creationflags=FLAGS)
            wait_for_demo(server, RUN / 'server.log')
            game = children['game'] = subprocess.Popen(
                [str(editor), str(project), '/Game/AGV/NewMap',
                 '-game', '-windowed', '-ResX=1200', '-ResY=720', '-nosound', '-NoSplash',
                 '-ABSLOG=' + str(RUN / 'unreal.log')]
                + (['-LocalDataCachePath=' + str(args.ddc.resolve())] if args.ddc else []),
                cwd=ROOT, env=game_env, stdout=glog, stderr=glog)
            record('game_starting', game_pid=game.pid, server_pid=server.pid, broker_pid=broker.pid)

            def ready():
                status, inventory = get('/order-demo/api/status'), get('/order-demo/api/inventory')
                return (status, inventory) if (status.get('accepting_orders') and inventory.get('fresh')
                                               and inventory.get('pickup', {}).get('fresh')) else None

            initial, inventory = wait_for(ready, 900, 'Unreal world not ready', children)
            starts = pose(get_probe('/_isolated_test/fleet', observer_token))
            result['initial_inventory'] = inventory
            show('ready', '주문 시작 대기: 빨강·파랑 AGV')
            record('world_ready', game_pid=game.pid, world_id=inventory['pickup']['world_id'], starts=starts,
                   simulation_speed_mm_per_second=400)
            time.sleep(8)
            with ThreadPoolExecutor(max_workers=2) as pool:
                submissions = list(pool.map(post, ('red', 'blue')))
            ids = [x['request_id'] for x in submissions]
            if len(set(ids)) != 2:
                raise AssertionError('two distinct orders required')
            record('orders_submitted', requests=ids)

            def both_moving():
                observed = get_probe('/_isolated_test/fleet', observer_token, timeout=.4)
                fleet = observed['fleet']['robots']
                current = pose(observed)
                orders = [get('/order-demo/api/orders/' + rid) for rid in ids]
                if ({x.get('assigned_robot') for x in orders} == {'sim-r1', 'sim-r2'}
                        and all(item['status'] == 'moving' and item['phase'] == 'to_pickup' for item in fleet)
                        and all(xy_moved(current[key], starts[key]) for key in starts)):
                    return {'observation': observed, 'orders': orders, 'occupancy': occupancy()}
                if any(x.get('status') in ('failed', 'unknown', 'completed') for x in orders):
                    raise RuntimeError('an order ended before both robots moved: ' + repr(orders))
                return None

            before = wait_for(both_moving, 20, 'both robots did not move before fault', children)
            result['before_fault'] = before
            if not any(row[0] == 'edge' for row in before['occupancy']):
                raise AssertionError('no reserved edge while both robots move')
            record('both_moving_before_fault', poses=pose(before['observation']), occupancy=before['occupancy'],
                   orders=before['orders'])
            show('injecting', 'sim-r2 단절 주입 준비 · 두 AGV 이동 확인')
            before_stop = get_probe('/_isolated_test/fleet', observer_token, timeout=5)
            if not all(item['status'] == 'moving' and item['phase'] == 'to_pickup'
                       for item in before_stop['fleet']['robots']):
                raise RuntimeError('both robots were not moving immediately before sim-r2 fault')
            before_stop_occupancy = occupancy()
            result['immediately_before_fault'] = {'observation': before_stop,
                                                    'occupancy': before_stop_occupancy}
            fault_at = time.time()
            takeover_ready = threading.Event()
            takeover = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id='sim-r2-order-executor', clean_session=False,
                                   protocol=mqtt.MQTTv311, reconnect_on_failure=False)
            takeover.on_connect = lambda _c, _u, _f, reason, _p: takeover_ready.set() if not reason.is_failure else None
            if takeover.connect('127.0.0.1', broker_port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('duplicate-client fault could not connect')
            takeover.loop_start()
            if not takeover_ready.wait(8):
                raise TimeoutError('duplicate-client fault did not connect')
            record('duplicate_client_connected', injection_started_at=fault_at, broker_pid=broker.pid)

            def disconnected():
                current = get_probe('/_isolated_test/fleet', observer_token, timeout=.5)
                return current if (broker.poll() is None and current['recovering'] and current['broker_publisher_connected']
                                   and current['robots']['sim-r1']['ready']
                                   and not current['robots']['sim-r2']['ready']) else None

            lost = wait_for(disconnected, 20, 'isolated sim-r2 disconnection not detected', children, ignore=('broker',))
            frozen_occupancy = occupancy()
            frozen_pose = pose(lost)
            lost_actions = actions()
            result['disconnect_detected'] = {'observation': lost, 'occupancy': frozen_occupancy,
                                              'actions': lost_actions}
            record('disconnect_detected', poses=frozen_pose, occupancy=frozen_occupancy,
                   actions=lost_actions, observation=lost)
            show('disconnected', 'sim-r2 단절 감지 · 두 AGV 위치 확인 중')
            if not lost['unreal_owner_connected']:
                raise AssertionError('Unreal owner disconnected during broker fault')
            if frozen_occupancy != before_stop_occupancy:
                raise AssertionError('cannot establish unchanged reservation between injection and detection')
            samples, transient_503 = [], []
            window_start = 0
            sample_deadline = time.monotonic() + 30
            still_lost = None
            while time.monotonic() < sample_deadline:
                when = time.monotonic()
                try:
                    current = get('/_isolated_test/fleet-fast', observer_token)
                except HTTPError as exc:
                    if exc.code != 503:
                        raise
                    transient_503.append({'time': time.time(), 'reason': 'probe_503'})
                    time.sleep(.2)
                    continue
                current_occupancy = occupancy()
                current_actions = actions()
                if (pose(current) != frozen_pose or current_occupancy != frozen_occupancy
                        or not current['recovering'] or not current['broker_publisher_connected']
                        or not current['unreal_owner_connected'] or broker.poll() is not None
                        or not current['robots']['sim-r1']['ready']
                        or current['robots']['sim-r2']['ready']):
                    raise AssertionError('observed state or occupancy changed while disconnected')
                if current_actions != lost_actions:
                    raise AssertionError('new Unreal action appeared during disconnection')
                samples.append({'monotonic': when, 'time': time.time(), 'poses': pose(current),
                                'occupancy': current_occupancy, 'recovering': current['recovering'],
                                'ready': {key: item['ready'] for key, item in current['robots'].items()}})
                still_lost = current
                # The claim is bounded to recorded samples. Restart the four-
                # second interval after a long observation gap; do not infer
                # what happened inside that gap.
                if len(samples) > 1 and samples[-1]['monotonic'] - samples[-2]['monotonic'] > 1.0:
                    record('observation_gap', gap_seconds=samples[-1]['monotonic'] - samples[-2]['monotonic'])
                    window_start = len(samples) - 1
                if (len(samples) - window_start >= 9
                        and samples[-1]['monotonic'] - samples[window_start]['monotonic'] >= 4.0):
                    break
                time.sleep(.2)
            else:
                raise TimeoutError('insufficient observed samples during disconnection')
            gaps = [b['monotonic'] - a['monotonic'] for a, b in zip(samples, samples[1:])]
            window = samples[window_start:]
            window_gaps = [b['monotonic'] - a['monotonic'] for a, b in zip(window, window[1:])]
            result['while_disconnected'] = {'first': lost, 'after': still_lost,
                                             'occupancy': frozen_occupancy, 'actions': lost_actions,
                                             'samples': samples, 'transient_503': transient_503,
                                             'max_sample_gap_seconds': max(gaps),
                                             'confirmation_window_start_index': window_start}
            record('frozen_confirmed', observed_seconds=window[-1]['monotonic'] - window[0]['monotonic'],
                   sample_count=len(window), max_sample_gap_seconds=max(window_gaps),
                   total_samples=len(samples), max_total_gap_seconds=max(gaps),
                   transient_503=transient_503, poses=pose(still_lost), occupancy=frozen_occupancy)
            show('frozen', '관측 표본에서 두 AGV 위치·점유 유지 확인')
            time.sleep(3)
            takeover.disconnect()
            takeover.loop_stop()
            takeover = None
            HOLD.write_text('released', encoding='utf-8')
            record('sim_r2_reconnect_released', broker_pid=broker.pid)
            show('reconnecting', 'sim-r2 재접속 허용 · 연결 확인')

            def reconnected():
                current = get_probe('/_isolated_test/fleet', observer_token, timeout=.5)
                return current if (not current['recovering'] and current['broker_publisher_connected']
                                   and all(item['ready'] for item in current['robots'].values())
                                   and current['unreal_owner_connected']) else None

            recovered = wait_for(reconnected, 45, 'fleet did not reconnect', children)
            result['reconnected'] = recovered
            record('connection_recovered', poses=pose(recovered), occupancy=occupancy())
            show('reconnecting', 'sim-r2 연결됨 · AGV 재주행 확인 중')

            def both_advanced():
                current = get_probe('/_isolated_test/fleet', observer_token, timeout=.5)
                positions = pose(current)
                return current if all(xy_moved(positions[key], frozen_pose[key]) for key in frozen_pose) else None

            advanced = wait_for(both_advanced, 30, 'both robots did not resume after reconnect', children)
            record('both_resumed', poses=pose(advanced), occupancy=occupancy())
            show('recovered', 'sim-r2 복구 · 두 AGV 재주행 확인')
            def completed():
                orders = [get('/order-demo/api/orders/' + rid) for rid in ids]
                if any(x['status'] in ('unknown', 'failed') for x in orders):
                    raise RuntimeError('order did not safely resume: ' + repr(orders))
                return orders if all(x['status'] == 'completed' for x in orders) else None
            orders = wait_for(completed, 180, 'both orders did not finish', children)
            recorded_actions = actions()
            expected = sorted((agv, action) for agv in ('AGV_01', 'AGV_02') for action in ('grab', 'place'))
            got = sorted((a['agv_id'], a['action']) for a in recorded_actions)
            if got != expected or len({a['action_id'] for a in recorded_actions}) != 4:
                raise AssertionError('missing or duplicate Unreal actions: ' + repr(recorded_actions))
            final = get('/order-demo/api/inventory')
            show('recovered', '시험 완료 · 두 AGV 주문 처리 확인')
            result.update(passed=True, orders=orders, actions=recorded_actions, inventory=final)
            record('passed', orders=orders, actions=recorded_actions, inventory=final)
            time.sleep(10)
    except BaseException as exc:
        result['passed'] = False
        result['error'] = repr(exc)
        try:
            record('failed', error=repr(exc))
        except Exception as log_error:
            print('Could not record original failure: ' + repr(log_error), file=sys.stderr, flush=True)
        try:
            if children.get('indicator') is not None and children['indicator'].poll() is None:
                show('failed', '시험 중단 · 결과 확인 필요')
                time.sleep(8)
        except Exception:
            pass
        raise
    finally:
        finalize_run(result, RUN, children, stop_process, record,
                     takeover=takeover, had_error=sys.exc_info()[0] is not None)


if __name__ == '__main__':
    main()
