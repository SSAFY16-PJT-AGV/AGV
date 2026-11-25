"""Loopback-only Mosquitto/Paho watcher test; never contacts a physical robot."""
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import paho.mqtt.client as mqtt

from server.physical_state_proof import PhysicalStateWatcher
from server.tools.twin_multi_test import stop_process


@unittest.skipUnless(os.environ.get('MOSQUITTO_EXE'), 'set MOSQUITTO_EXE for isolated broker')
class WatcherMqttTests(unittest.TestCase):
    def test_suback_live_state_offline_and_reconnect_do_not_reuse_evidence(self):
        parent = Path(__file__).resolve().parents[2] / '.venv'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='physical-watch-mqtt-', dir=parent) as temp:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            config = Path(temp) / 'mosquitto.conf'
            config.write_text('listener %d 127.0.0.1\nallow_anonymous true\n' % port, encoding='utf-8')
            broker = subprocess.Popen([os.environ['MOSQUITTO_EXE'], '-c', str(config)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        socket.create_connection(('127.0.0.1', port), timeout=0.2).close()
                        break
                    except OSError:
                        if broker.poll() is not None or time.monotonic() >= deadline:
                            self.fail('isolated Mosquitto failed to start')
                        time.sleep(0.02)
                robot = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
                connected = threading.Event()
                robot.on_connect = lambda _c, _u, _f, reason, _p: connected.set() if not reason.is_failure else None
                robot.connect('127.0.0.1', port, keepalive=10)
                robot.loop_start()
                try:
                    self.assertTrue(connected.wait(5))
                    state = {'robot_session': 'boot-1', 'status': 'WAITING', 'order_id': 'T1',
                             'seq': 1, 'last_node': 'B', 'released_until': 'B', 'map_id': 'm@v1',
                             'error': None, 'rejected': None}
                    def send(topic, payload, *, retain=True):
                        info = robot.publish(topic, payload, qos=1, retain=retain)
                        info.wait_for_publish(timeout=5)
                        self.assertTrue(info.is_published())

                    def until(predicate, label):
                        deadline = time.monotonic() + 5
                        while not predicate():
                            if time.monotonic() >= deadline:
                                self.fail('timed out: ' + label)
                            time.sleep(0.01)

                    send('agv/physical-1/conn', b'online')
                    send('agv/physical-1/state', json.dumps(state))
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
                    watcher = PhysicalStateWatcher(['physical-1'], client=client, host='127.0.0.1',
                                                   port=port, log=lambda _message: None)
                    watcher.start()
                    try:
                        until(lambda: watcher._online.get('physical-1') is True, 'retained online')
                        self.assertIsNone(watcher.latest('physical-1'), 'retained state must not authorize')
                        blocked = threading.Event()
                        watcher._runtime = SimpleNamespace(
                            _tasks={'T1': ('physical-1', 'boot-1')},
                            gate=SimpleNamespace(invalidate_many=lambda _tasks: blocked.set()))
                        watcher._sessions = {'physical-1': 'boot-1'}
                        send('agv/physical-1/state', json.dumps(state))
                        until(lambda: watcher.latest('physical-1') == state, 'fresh live state')
                        self.assertIsNotNone(watcher._token('physical-1', state))
                        send('agv/physical-1/conn', b'offline')
                        self.assertTrue(blocked.wait(5), 'offline must invalidate task')
                        self.assertIsNone(watcher.latest('physical-1'))
                        send('agv/physical-1/conn', b'online')
                        send('agv/physical-1/state', json.dumps(state))
                        until(lambda: watcher._online.get('physical-1') is True, 'online after offline')
                        self.assertIsNone(watcher._token('physical-1', state))
                    finally:
                        watcher.stop()
                finally:
                    robot.disconnect()
                    robot.loop_stop()
            finally:
                stop_process(broker)


if __name__ == '__main__':
    unittest.main()
