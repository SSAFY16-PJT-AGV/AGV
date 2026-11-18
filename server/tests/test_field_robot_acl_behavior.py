"""PUBACK is not proof that an isolated MQTT broker delivered an event.

Uses a disposable copy of the team robot, an owned loopback aMQTT broker and
an explicit ACL denying the event topic. No real robot or shared broker is used.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from server.tests.test_twin_broker_integration import free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'server/docs/field_nav_sequence_limited.patch'

BROKER = r'''
import asyncio, logging, os, sys
from pathlib import Path
from amqtt.broker import Broker
logging.basicConfig(level=logging.INFO)
async def main():
    config = {
        'listeners': {'default': {'type': 'tcp', 'bind': '127.0.0.1:' + sys.argv[1]}},
        'sys_interval': 0,
        'auth': {'allow-anonymous': True},
        'topic-check': {'enabled': True, 'plugins': ['topic_acl'],
                        'publish-acl': {'anonymous': ['agv/+/conn']},
                        'subscribe-acl': {'anonymous': ['agv/+/event']}},
    }
    broker = Broker(config)
    await broker.start()
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding='ascii')
    await asyncio.Event().wait()
asyncio.run(main())
'''

WORKER = r'''
import json, sys, threading, time
import paho.mqtt.client as mqtt
from agv.link.mqtt import MqttLink
port = int(sys.argv[1])
messages, errors = [], []
subscribed = threading.Event()
observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='field-acl-test-observer')
def on_connect(client, _userdata, _flags, reason, _properties):
    if reason.is_failure:
        errors.append('observer connection rejected')
        subscribed.set()
    else:
        client.subscribe('agv/sim-r1/event', qos=1)
def on_subscribe(_client, _userdata, _mid, reasons, _properties):
    if not reasons or any(reason.is_failure for reason in reasons):
        errors.append('observer subscription rejected')
    subscribed.set()
observer.on_connect, observer.on_subscribe = on_connect, on_subscribe
observer.on_message = lambda _c, _u, msg: messages.append(json.loads(msg.payload))
link = None
try:
    observer.connect('127.0.0.1', port, keepalive=10)
    observer.loop_start()
    if not subscribed.wait(5) or errors:
        raise RuntimeError('isolated observer unavailable: ' + repr(errors))
    link = MqttLink('sim-r1', port=port)
    link.start()
    if not link.wait_connected(5):
        raise RuntimeError('isolated robot connection unavailable')
    acknowledged = link.publish_event_once({'type': 'rear_clear', 'event_id': 'acl-denial-test'})
    time.sleep(1)
    print(json.dumps({'acknowledged': acknowledged, 'received': messages}), flush=True)
finally:
    if link is not None:
        link.stop()
    observer.disconnect()
    observer.loop_stop()
'''


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH') and os.environ.get('AGV_FIELD_REPO'),
                     'requires isolated aMQTT and the team field-run source')
class FieldRobotAclBehaviorTest(unittest.TestCase):
    def test_denied_event_may_receive_puback_without_reaching_subscriber(self):
        source = Path(os.environ['AGV_FIELD_REPO']).resolve()
        deps = Path(os.environ['AMQTT_TEST_PATH']).resolve()
        with tempfile.TemporaryDirectory(prefix='field-acl-test-') as temp, ExitStack() as cleanup:
            root = Path(temp)
            target = root / 'team'
            target.mkdir()
            shutil.copytree(source / 'agv', target / 'agv',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            apply_env = os.environ.copy()
            apply_env['GIT_CEILING_DIRECTORIES'] = str(root)
            applied = subprocess.run(['git', 'apply', str(PATCH)], cwd=target, env=apply_env,
                                     capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(applied.returncode, 0, applied.stderr)

            port, ready = free_port(), root / 'broker.ready'
            broker_log_path = root / 'broker.log'
            broker_log = cleanup.enter_context(broker_log_path.open('w', encoding='utf-8'))
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = str(deps)
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', BROKER,
                                       str(port), str(ready)], env=broker_env, stdout=broker_log,
                                      stderr=broker_log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(ready))

            worker_env = isolated_environment(os.environ)
            worker_env['PYTHONPATH'] = str(target)
            worker = subprocess.run([sys.executable, '-c', WORKER, str(port)], cwd=target,
                                    env=worker_env, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=30,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(worker.returncode, 0, worker.stdout + '\n' + worker.stderr)
            result = json.loads(worker.stdout.strip().splitlines()[-1])
            self.assertEqual(result, {'acknowledged': True, 'received': []})
            broker_log.flush()
            log_text = broker_log_path.read_text(encoding='utf-8', errors='replace')
            self.assertIn('not allowed to publish to TOPIC agv/sim-r1/event', log_text)


if __name__ == '__main__':
    unittest.main()
