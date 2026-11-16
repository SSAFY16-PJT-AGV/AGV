"""Run a loopback-only, synthetic two-robot visual test.

Use an independently built Unreal project containing the local authentication
patch. This script does not modify, build or terminate the Unreal project.
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
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from unittest.mock import patch

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[2]
MAP_ID = 'twin-validation-v1'
SERVER_PORT = 8000
HTTP = build_opener(ProxyHandler({}))
BROKER_SCRIPT = r'''
import asyncio
import os
import sys
from amqtt.broker import Broker

async def main():
    broker = Broker({'listeners': {'default': {'type': 'tcp',
                     'bind': '127.0.0.1:' + sys.argv[1]}},
                     'sys_interval': 0, 'auth': {'allow-anonymous': True}})
    await broker.start()
    print('[AGV_ISOLATED_BROKER_READY]:' + str(os.getpid()), flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
'''


def isolated_environment(source):
    result = {}
    for key, value in source.items():
        lower = key.lower()
        if (lower.startswith(('mqtt_', 'agv_sim_', 'agv_twin_')) or
                lower.endswith('_proxy') or lower in ('pythonpath', 'pythonhome')):
            continue
        result[key] = value
    result.update({'NO_PROXY': '127.0.0.1,localhost',
                   'no_proxy': '127.0.0.1,localhost',
                   'PYTHON_DOTENV_DISABLED': '1'})
    return result


def server_environment(source, port, token):
    env = isolated_environment(source)
    env.update({
        'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
        'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(port),
        'AGV_SIM_CONFIG': '', 'AGV_TWIN_WEBSOCKET': '1',
        'AGV_TWIN_ROBOT_ID': '',
        'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}),
        'AGV_TWIN_UNREAL_TOKEN': token, 'AGV_TWIN_POSE_MAP_ID': MAP_ID,
    })
    return env


def validate_paths(editor, project, ddc):
    editor, project = Path(editor).resolve(), Path(project).resolve()
    ddc = Path(ddc).resolve() if ddc else None
    if not editor.is_file():
        raise ValueError('Unreal Editor 실행 파일을 찾을 수 없습니다.')
    if not project.is_file() or project.suffix.lower() != '.uproject':
        raise ValueError('기존 .uproject 파일을 지정해야 합니다.')
    if ddc is not None and not ddc.is_dir():
        raise ValueError('지정한 DDC 폴더가 존재하지 않습니다.')
    return editor, project, ddc


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def wait_for_ready_log(process, log_path, marker, label, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(label + ' exited before its listener was ready')
        try:
            lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            lines = []
        if marker in lines and process.poll() is None:
            return
        time.sleep(0.01)
    raise TimeoutError(label + ' listener did not become ready')


def wait_for_server(process, log_path):
    wait_for_ready_log(process, log_path,
                       'INFO:     Started server process [{}]'.format(process.pid), 'server')
    wait_for_ready_log(process, log_path,
                       'INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)',
                       'server')
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('server exited before MQTT subscription was ready')
        try:
            with HTTP.open('http://127.0.0.1:8000/robots', timeout=1) as response:
                if json.load(response).get('ready') is True and process.poll() is None:
                    return
        except (OSError, URLError, ValueError):
            pass
        time.sleep(0.1)
    raise TimeoutError('server MQTT subscription did not become ready')


def connect_local_publisher(client, port):
    if mqtt.socks is not None:
        proxy = mqtt.socks.get_default_proxy()
        if proxy and proxy[0] is not None:
            raise RuntimeError('global SOCKS proxy is incompatible with an isolated test')
    with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost',
                                 'no_proxy': '127.0.0.1,localhost'}):
        client.connect('127.0.0.1', port, keepalive=15)


def simulated_states(elapsed):
    result = {}
    for robot, start, end, y, speed in (
        ('sim-r1', 1500.0, 6500.0, 2000, 2400.0),
        ('sim-r2', 2800.0, 5500.0, 7000, 1400.0),
    ):
        leg = (end - start) / speed
        phase = elapsed % (2 * leg)
        forward = phase < leg
        x = min(end, max(start, start + (phase if forward else 2 * leg - phase) * speed))
        if robot == 'sim-r1':
            node = ('N01' if x >= 3500 else 'J01') if forward else ('N01' if x <= 3500 else 'J02')
        else:
            node = 'N02' if forward else 'N03'
        result[robot] = {'map_id': MAP_ID, 'last_node': node, 'status': 'MOVING',
                         'battery': 0.8, 'pose': {'x': x, 'y': y, 'th': 0}}
    return result


def publish_states(client, elapsed):
    for robot, state in simulated_states(elapsed).items():
        result = client.publish('agv/{}/state'.format(robot), json.dumps(state),
                                qos=0, retain=False)
        result.wait_for_publish(timeout=5)
        if not result.is_published():
            raise RuntimeError('virtual state publication failed')


def run(editor, project, ddc, broker_deps):
    editor, project, ddc = validate_paths(editor, project, ddc)
    broker_deps = Path(broker_deps).resolve()
    if not (broker_deps / 'amqtt').is_dir():
        raise ValueError('aMQTT 테스트 의존성 폴더를 찾을 수 없습니다: ' + str(broker_deps))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', SERVER_PORT))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        broker_port = probe.getsockname()[1]
    if broker_port == SERVER_PORT:
        raise RuntimeError('temporary broker port conflicts with server port; retry')

    token = secrets.token_urlsafe(32)
    broker_env = isolated_environment(os.environ)
    broker_env['PYTHONPATH'] = str(broker_deps)
    server_env = server_environment(os.environ, broker_port, token)
    editor_env = isolated_environment(os.environ)
    editor_env['AGV_TWIN_UNREAL_TOKEN'] = token
    broker = server = publisher = None
    publisher_running = False
    connected = threading.Event()
    failed = threading.Event()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    base_python = getattr(sys, '_base_executable', sys.executable)
    try:
        with tempfile.TemporaryDirectory(prefix='agv-twin-multi-') as folder:
            broker_path, server_path = Path(folder) / 'broker.log', Path(folder) / 'server.log'
            with broker_path.open('w', encoding='utf-8') as broker_log, server_path.open('w', encoding='utf-8') as server_log:
                try:
                    broker = subprocess.Popen([base_python, '-c', BROKER_SCRIPT, str(broker_port)],
                                              cwd=ROOT, env=broker_env, stdout=broker_log,
                                              stderr=broker_log, creationflags=flags)
                    wait_for_ready_log(broker, broker_path,
                                       '[AGV_ISOLATED_BROKER_READY]:{}'.format(broker.pid), 'broker')
                    server = subprocess.Popen([base_python, '-m', 'uvicorn', 'server.main:app',
                                               '--host', '127.0.0.1', '--port', str(SERVER_PORT),
                                               '--workers', '1', '--no-proxy-headers'],
                                              cwd=ROOT, env=server_env, stdout=server_log,
                                              stderr=server_log, creationflags=flags)
                    wait_for_server(server, server_path)
                    publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                            client_id='twin-multi-test-publisher',
                                            reconnect_on_failure=False)

                    def on_connect(client, userdata, flags, reason_code, properties):
                        (failed if reason_code.is_failure else connected).set()

                    def on_disconnect(client, userdata, flags, reason_code, properties):
                        connected.clear()

                    publisher.on_connect = on_connect
                    publisher.on_disconnect = on_disconnect
                    connect_local_publisher(publisher, broker_port)
                    publisher.loop_start()
                    publisher_running = True
                    if not connected.wait(10) or failed.is_set():
                        raise RuntimeError('virtual publisher did not connect')
                    cmd = [str(editor), str(project), '-NoSplash',
                           '-asynctexturecompilationmaxconcurrency=1']
                    if ddc is not None:
                        cmd.append('-LocalDataCachePath=' + str(ddc))
                    subprocess.Popen(cmd, env=editor_env)
                    print('격리 서버와 가상 로봇 두 대를 실행했습니다. Unreal에서 NewMap을 열고 플레이하세요.', flush=True)
                    print('Ctrl+C로 서버·브로커를 종료합니다. Unreal은 직접 닫으세요.', flush=True)
                    start = time.monotonic()
                    while True:
                        if broker.poll() is not None or server.poll() is not None or not connected.is_set():
                            raise RuntimeError('isolated server, broker, or publisher disconnected')
                        publish_states(publisher, time.monotonic() - start)
                        time.sleep(0.2)
                finally:
                    try:
                        if publisher is not None:
                            try:
                                publisher.disconnect()
                            finally:
                                if publisher_running:
                                    publisher.loop_stop()
                    finally:
                        try:
                            stop_process(server)
                        finally:
                            stop_process(broker)
    except KeyboardInterrupt:
        print('시험을 종료했습니다. Unreal은 직접 닫으세요.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--editor', required=True, help='UnrealEditor executable path')
    parser.add_argument('--project', required=True, help='patched and built .uproject path')
    parser.add_argument('--ddc-path', help='existing optional local derived data cache directory')
    parser.add_argument('--broker-deps', default=str(ROOT / '.venv' / 'broker-deps'),
                        help='directory containing separately installed aMQTT')
    args = parser.parse_args()
    try:
        run(args.editor, args.project, args.ddc_path, args.broker_deps)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print('시험 실패: {}'.format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
