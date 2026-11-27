"""Interrupted persistent team orders must not be published again."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack
import json
import subprocess
import sys
import sysconfig
import secrets

from server.tests import test_team_shelf_world_restart_http as previous


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class TeamShelfPersistentHttpTest(unittest.TestCase):
    def test_two_servers_cannot_own_same_state(self):
        with tempfile.TemporaryDirectory(prefix='team-persistent-owner-') as temp, ExitStack() as cleanup:
            folder = Path(temp)
            ports = set()
            while len(ports) < 3:
                port = previous.free_port()
                if port != 8000:
                    ports.add(port)
            broker_port, first_port, second_port = ports
            python = getattr(sys, '_base_executable', sys.executable)
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            env = previous.isolated_environment(os.environ)
            env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            null = cleanup.enter_context(open(os.devnull, 'w'))
            ready = folder / 'broker.ready'
            broker = subprocess.Popen([python, '-c', previous.BROKER, str(broker_port), str(ready)],
                env=env, cwd=previous.ROOT, stdout=null, stderr=null, creationflags=flags)
            cleanup.callback(previous.stop, broker)
            previous.wait_for_owned_broker(self, broker, str(ready))
            examples = previous.ROOT / 'server/examples'
            env = previous.isolated_environment({k: v for k, v in os.environ.items() if not k.upper().startswith('AGV_')})
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(previous.ROOT)]),
                'TEMP': temp, 'TMP': temp, 'AGV_ORDER_DEMO_ENABLED': '1', 'AGV_ORDER_COORDINATED': '1',
                'AGV_ORDER_FLEET': '1', 'AGV_ORDER_SHELF_SYNC': '1', 'AGV_ORDER_TEAM_SHELVES': '1',
                'AGV_ORDER_PERSIST_DIR': str(folder / 'state'),
                'AGV_ORDER_TEAM_WORLD_REGISTRY': str(folder / 'worlds.sqlite3'),
                'AGV_ORDER_DEMO_MAP': str(examples / 'mat_ssafy_v3_unreal_sim.json'),
                'AGV_ORDER_FIELD_CONFIG': str(examples / 'field_unreal_v3_sim.json'),
                'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(broker_port), 'AGV_TWIN_WEBSOCKET': '1',
                'AGV_TWIN_UNREAL_TOKEN': secrets.token_urlsafe(32),
                'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'})})
            first_log = cleanup.enter_context((folder / 'first.log').open('w', encoding='utf-8'))
            second_log = cleanup.enter_context((folder / 'second.log').open('w', encoding='utf-8'))
            def start(port, log):
                process = subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app', '--host',
                    '127.0.0.1', '--port', str(port), '--workers', '1', '--no-proxy-headers'],
                    cwd=previous.ROOT, env=env, stdout=log, stderr=log, creationflags=flags)
                cleanup.callback(previous.stop, process)
                return process
            first = start(first_port, first_log)
            previous.wait_for_owned_server(self, first, str(folder / 'first.log'), first_port)
            second = start(second_port, second_log)
            self.assertNotEqual(second.wait(timeout=20), 0)
            second_log.flush()
            self.assertIn('already in use', (folder / 'second.log').read_text(encoding='utf-8'))
            self.assertIsNone(first.poll())

    def test_interrupted_order_retains_key_and_never_republishes(self):
        with tempfile.TemporaryDirectory(prefix='team-persistent-http-') as temp:
            state = Path(temp) / 'state'
            original = previous.isolated_environment
            def persistent_environment(source):
                env = original(source)
                env['AGV_ORDER_PERSIST_DIR'] = str(state)
                return env
            case = previous.TeamShelfWorldRestartHttpTest(
                'test_same_world_and_request_key_after_server_restart_do_not_publish')
            with patch.object(previous, 'isolated_environment', side_effect=persistent_environment):
                case.test_same_world_and_request_key_after_server_restart_do_not_publish(expect_retry=False)
            self.assertTrue((state / 'orders.sqlite3').is_file())
            self.assertTrue((state / 'team-slots.sqlite3').is_file())


if __name__ == '__main__':
    unittest.main()
