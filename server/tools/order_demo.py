"""Launch a loopback-only web order demo with a temporary MQTT broker.

The optional Unreal project must already be built and contain the local
authentication patch. This tool never terminates an Unreal process.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError

from server.order_demo_engine import OrderDemoEngine
from server.tools.twin_multi_test import (BROKER_SCRIPT, HTTP, ROOT, SERVER_PORT,
    isolated_environment, server_environment, stop_process, validate_paths,
    wait_for_ready_log)


def clean_environment(source):
    # The shared helper strips MQTT, selected AGV settings and all proxies.
    # The standalone demo additionally excludes every other inherited AGV_ key.
    return isolated_environment({key: value for key, value in source.items()
                                 if not key.lower().startswith('agv_')})


def make_server_environment(source, port, token, map_path):
    env = server_environment(clean_environment(source), port, token)
    env['AGV_ORDER_DEMO_ENABLED'] = '1'
    env['AGV_ORDER_COORDINATED'] = '1'
    env['AGV_ORDER_FLEET'] = '1'
    env['AGV_ORDER_DEMO_MAP'] = str(Path(map_path).resolve())
    return env


def validate_inputs(map_path, editor, project, ddc, broker_deps):
    if bool(editor) != bool(project):
        raise ValueError('Unreal editor와 project는 함께 지정해야 합니다')
    if ddc and not editor:
        raise ValueError('DDC 경로는 Unreal editor와 함께 지정해야 합니다')
    if editor:
        editor, project, ddc = validate_paths(editor, project, ddc)
    else:
        editor = project = ddc = None
    path = Path(map_path).resolve()
    if not path.is_file():
        raise ValueError('Unreal 맵 JSON을 찾을 수 없습니다')
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('맵 파일이 너무 큽니다')
    try:
        OrderDemoEngine(json.loads(raw.decode('utf-8')))
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ValueError('유효한 Unreal 맵 JSON이 아닙니다') from exc
    deps = Path(broker_deps).resolve()
    if not (deps / 'amqtt').is_dir():
        raise ValueError('aMQTT 의존성 폴더를 찾을 수 없습니다')
    return path, editor, project, ddc, deps


def _select_broker_port():
    # Test the fixed Unreal listener port before launching any children.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', SERVER_PORT))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    if port == SERVER_PORT:
        raise RuntimeError('임시 브로커 포트가 충돌했습니다. 다시 실행하세요')
    return port


def wait_for_demo(server, log_path, timeout=20):
    wait_for_ready_log(server, log_path,
                       'INFO:     Started server process [{}]'.format(server.pid), 'server', timeout)
    wait_for_ready_log(server, log_path,
                       'INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)',
                       'server', timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError('시연 서버가 준비되기 전에 종료됐습니다')
        try:
            with HTTP.open('http://127.0.0.1:8000/order-demo/api/status', timeout=1) as response:
                if json.load(response).get('connected') is True and server.poll() is None:
                    return
        except (OSError, URLError, ValueError):
            pass
        time.sleep(0.1)
    raise TimeoutError('시연 서버가 MQTT에 연결되지 않았습니다')


def run(map_path, editor=None, project=None, ddc=None, broker_deps=ROOT / '.venv' / 'broker-deps'):
    map_path, editor, project, ddc, broker_deps = validate_inputs(
        map_path, editor, project, ddc, broker_deps)
    broker_port = _select_broker_port()
    token = secrets.token_urlsafe(32)
    broker_env = clean_environment(os.environ)
    broker_env['PYTHONPATH'] = str(broker_deps)
    server_env = make_server_environment(os.environ, broker_port, token, map_path)
    editor_env = clean_environment(os.environ)
    editor_env['AGV_TWIN_UNREAL_TOKEN'] = token
    base_python = getattr(sys, '_base_executable', sys.executable)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    broker = server = None
    try:
        with tempfile.TemporaryDirectory(prefix='agv-order-demo-') as folder:
            broker_path, server_path = Path(folder) / 'broker.log', Path(folder) / 'server.log'
            with broker_path.open('w', encoding='utf-8') as broker_log, server_path.open('w', encoding='utf-8') as server_log:
                try:
                    broker = subprocess.Popen([base_python, '-c', BROKER_SCRIPT, str(broker_port)],
                                              cwd=ROOT, env=broker_env, stdout=broker_log,
                                              stderr=broker_log, creationflags=flags)
                    wait_for_ready_log(broker, broker_path,
                                       '[AGV_ISOLATED_BROKER_READY]:{}'.format(broker.pid), 'broker')
                    server = subprocess.Popen([base_python, '-m', 'uvicorn', 'server.order_demo_app:app',
                                               '--host', '127.0.0.1', '--port', str(SERVER_PORT),
                                               '--workers', '1', '--no-proxy-headers'],
                                              cwd=ROOT, env=server_env, stdout=server_log,
                                              stderr=server_log, creationflags=flags)
                    wait_for_demo(server, server_path)
                    if editor:
                        cmd = [str(editor), str(project), '-NoSplash',
                               '-asynctexturecompilationmaxconcurrency=1']
                        if ddc is not None:
                            cmd.append('-LocalDataCachePath=' + str(ddc))
                        subprocess.Popen(cmd, env=editor_env)
                        print('Unreal에서 Content/AGV/NewMap을 열고 플레이하세요.', flush=True)
                    print('주문 화면: http://127.0.0.1:8000/order-demo/', flush=True)
                    print('Ctrl+C로 시연 서버와 브로커를 종료합니다. Unreal은 직접 닫으세요.', flush=True)
                    while True:
                        if broker.poll() is not None or server.poll() is not None:
                            raise RuntimeError('시연 서버 또는 브로커가 종료됐습니다')
                        time.sleep(0.2)
                finally:
                    try:
                        stop_process(server)
                    finally:
                        stop_process(broker)
    except KeyboardInterrupt:
        print('시연을 종료했습니다. Unreal은 직접 닫으세요.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map', required=True, help='Unreal warehouse_map.json')
    parser.add_argument('--editor', help='UnrealEditor executable')
    parser.add_argument('--project', help='patched and built .uproject')
    parser.add_argument('--ddc-path', help='optional existing Unreal derived data cache directory')
    parser.add_argument('--broker-deps', default=str(ROOT / '.venv' / 'broker-deps'),
                        help='directory containing separately installed aMQTT')
    args = parser.parse_args()
    try:
        run(args.map, args.editor, args.project, args.ddc_path, args.broker_deps)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print('시연 실패: {}'.format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
