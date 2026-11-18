"""The proposed robot patch's event PUBACK on a disposable loopback broker."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

import paho.mqtt.client as mqtt
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]
TEAM = os.environ.get('AGV_FIELD_REPO')
BROKER_DEPS = os.environ.get('AMQTT_TEST_PATH')
PATCH = ROOT / 'server/docs/field_nav_sequence_limited.patch'


@unittest.skipUnless(TEAM and BROKER_DEPS, 'requires team source and isolated aMQTT')
class FieldRobotOneShotMqttTest(unittest.TestCase):
    def test_paho_reconnect_replays_an_unacknowledged_qos1_packet(self):
        # A tiny owned broker deliberately omits PUBACK, then records the
        # exact packet Paho emits after a manual reconnect.
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        listener.settimeout(5)
        port = listener.getsockname()[1]
        packets, errors = [], []

        def read_exact(conn, size):
            data = b''
            while len(data) < size:
                chunk = conn.recv(size - len(data))
                if not chunk:
                    raise EOFError('connection closed')
                data += chunk
            return data

        def packet(conn):
            head = read_exact(conn, 1)[0]
            length, multiplier = 0, 1
            while True:
                byte = read_exact(conn, 1)[0]
                length += (byte & 127) * multiplier
                multiplier *= 128
                if not byte & 128:
                    break
            return head, read_exact(conn, length)

        def broker():
            try:
                for _ in range(2):
                    conn, _address = listener.accept()
                    with conn:
                        conn.settimeout(5)
                        if packet(conn)[0] >> 4 != 1:
                            raise AssertionError('expected CONNECT')
                        conn.sendall(b'\x20\x02\x00\x00')
                        while True:
                            head, body = packet(conn)
                            if head >> 4 == 3:
                                packets.append((head, body))
                                break  # close without PUBACK
            except Exception as error:
                errors.append(repr(error))

        thread = threading.Thread(target=broker, daemon=True)
        thread.start()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='resend-test',
                             clean_session=True, reconnect_on_failure=False)
        connected = threading.Event()
        client.on_connect = lambda *_args: connected.set()
        try:
            client.connect('127.0.0.1', port, 10)
            for _ in range(60):
                if connected.is_set():
                    break
                client.loop(timeout=.1)
            self.assertTrue(connected.is_set())
            self.assertEqual(client.publish('agv/sim-r1/event', 'payload', qos=1).rc, 0)
            for _ in range(60):
                if packets:
                    break
                client.loop(timeout=.1)
            self.assertEqual(len(packets), 1, errors)
            for _ in range(30):
                if client.loop(timeout=.1) != 0:
                    break
            else:
                self.fail('Paho did not observe the withheld-ack disconnect')
            connected.clear()
            client.reconnect()
            for _ in range(60):
                client.loop(timeout=.1)
                if len(packets) >= 2:
                    break
            self.assertEqual(len(packets), 2, errors)
            self.assertFalse(packets[0][0] & 8)
            self.assertTrue(packets[1][0] & 8)  # MQTT DUP flag
            self.assertEqual(packets[0][1], packets[1][1])
        finally:
            client.disconnect()
            listener.close()
            thread.join(1)

    def test_real_broker_acknowledges_one_shot_event(self):
        source = Path(TEAM).resolve()
        port = free_port()
        env = isolated_environment(os.environ)
        env['PYTHONPATH'] = str(Path(BROKER_DEPS).resolve())
        with tempfile.TemporaryDirectory(prefix='robot-one-shot-') as folder, open(os.devnull, 'w') as log:
            root = Path(folder)
            for name in ('agv', 'maps', 'sim', 'mapping'):
                shutil.copytree(source / name, root / name,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            apply_env = os.environ.copy()
            apply_env['GIT_CEILING_DIRECTORIES'] = str(root.parent)
            applied = subprocess.run(['git', 'apply', str(PATCH)], cwd=root, env=apply_env,
                                     capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(applied.returncode, 0, applied.stderr)
            ready = str(root / 'broker.ready')
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                                       BROKER, str(port), ready], env=env, stdout=log, stderr=log,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                wait_for_owned_broker(self, broker, ready)
                child_env = os.environ.copy()
                child_env['PYTHONUTF8'] = '1'
                child_env['PYTHONPATH'] = str(root)
                code = '''
import json, queue, sys, threading
import paho.mqtt.client as mqtt
from agv.link.mqtt import MqttLink
port = int(sys.argv[1])
received, subscribed = queue.Queue(), threading.Event()
sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
sub.on_connect = lambda c,u,f,rc,p: c.subscribe('agv/sim-r1/event', qos=1)
sub.on_subscribe = lambda c,u,mid,reason,p: subscribed.set()
sub.on_message = lambda c,u,msg: received.put(json.loads(msg.payload))
sub.connect('127.0.0.1', port, 10)
sub.loop_start()
link = MqttLink('sim-r1', port=port)
link.start()
try:
    assert subscribed.wait(5) and link.wait_connected(5)
    assert link.publish_event_once({'event_id': 'puback-one-shot'})
    assert received.get(timeout=5) == {'event_id': 'puback-one-shot'}
finally:
    link.stop()
    sub.disconnect()
    sub.loop_stop()
'''
                child = subprocess.run([sys.executable, '-c', code, str(port)], cwd=root, env=child_env,
                                       capture_output=True, text=True, encoding='utf-8', timeout=30)
                self.assertEqual(child.returncode, 0, child.stdout + '\n' + child.stderr)
            finally:
                stop(broker)


if __name__ == '__main__':
    unittest.main()
