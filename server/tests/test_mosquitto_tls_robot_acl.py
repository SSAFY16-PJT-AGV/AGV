"""Two robot principals against a test-owned TLS/ACL Mosquitto instance.

No real broker, credentials, robot or network interface is used. PUBACK does
not prove ACL acceptance: authorization is checked by actual delivery.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import queue
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

import paho.mqtt.client as mqtt

from server.mqtt_security import configure_mqtt_client
from server.tests.test_twin_broker_integration import free_port, stop
from sim.mqtt_order_robot import _verify_no_proxy


MOSQUITTO = os.environ.get('MOSQUITTO_EXE')
OPENSSL = shutil.which('openssl')


@unittest.skipUnless(MOSQUITTO and OPENSSL, 'set MOSQUITTO_EXE and install OpenSSL')
class IsolatedTlsRobotAclTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        broker = Path(MOSQUITTO).resolve()
        password_tool = broker.with_name('mosquitto_passwd.exe' if os.name == 'nt' else 'mosquitto_passwd')
        if not broker.is_file() or not password_tool.is_file():
            raise RuntimeError('explicit Mosquitto broker and password tool required')
        temp_parent = os.environ.get('AGV_MQTT_ASCII_TEMP')
        if os.name == 'nt' and not str(temp_parent or tempfile.gettempdir()).isascii():
            raise RuntimeError('set AGV_MQTT_ASCII_TEMP to an existing writable ASCII path; '
                               'Windows Mosquitto cannot read the Unicode temp path')
        cls.temp = tempfile.TemporaryDirectory(prefix='agv-mqtt-tls-acl-', dir=temp_parent)
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name).resolve()
        cls.passwords = {user: secrets.token_urlsafe(32) for user in ('robot-1', 'robot-2', 'observer')}
        password_file = root / 'passwords.txt'
        password_file.write_text(''.join(user + ':' + value + '\n' for user, value in cls.passwords.items()),
                                 encoding='ascii')
        subprocess.run([str(password_tool), '-U', str(password_file)], check=True, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        cert, key, other = root / 'broker.crt', root / 'broker.key', root / 'other.crt'
        for cert_path, key_path, name in ((cert, key, 'test-broker'), (other, root / 'other.key', 'other-test')):
            subprocess.run([OPENSSL, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=' + name, '-addext', 'subjectAltName=IP:127.0.0.1',
                            '-keyout', str(key_path), '-out', str(cert_path)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        acl = root / 'acl.txt'
        lines = []
        for index in (1, 2):
            lines.append('user robot-{}'.format(index))
            lines.extend('topic write agv/sim-r{}/{}'.format(index, suffix)
                         for suffix in ('state', 'event', 'conn'))
            lines.append('topic read agv/sim-r{}/order'.format(index))
        # Test-only observer: validates actual message delivery in both directions.
        lines += ['user observer', 'topic read agv/+/+', 'topic write agv/+/order']
        acl.write_text('\n'.join(lines) + '\n', encoding='ascii')
        cls.port = free_port()
        config = root / 'mosquitto.conf'
        config.write_text('\n'.join([
            'listener {} 127.0.0.1'.format(cls.port), 'allow_anonymous false',
            'persistence false', 'password_file ' + password_file.as_posix(),
            'acl_file ' + acl.as_posix(), 'certfile ' + cert.as_posix(),
            'keyfile ' + key.as_posix(), 'log_type error', 'log_type warning', '',
        ]), encoding='utf-8')
        cls.cert, cls.other = cert, other
        cls.log = (root / 'broker.log').open('w+', encoding='utf-8', errors='replace')
        cls.addClassCleanup(cls.log.close)
        cls.broker = subprocess.Popen([str(broker), '-c', str(config)], stdout=cls.log, stderr=cls.log,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        cls.addClassCleanup(stop, cls.broker)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.broker.poll() is not None:
                cls.log.flush()
                raise RuntimeError('test-owned broker failed to start: ' +
                                   (root / 'broker.log').read_text(encoding='utf-8', errors='replace'))
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=.2):
                    break
            except OSError:
                time.sleep(.05)
        else:
            raise TimeoutError('test-owned TLS broker did not start')

    def connect(self, user, password=None, ca=None, *, will=None):
        _verify_no_proxy()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
            client_id='test-' + secrets.token_hex(8), protocol=mqtt.MQTTv311,
            reconnect_on_failure=False)
        if user is not None:
            configure_mqtt_client(client, user, self.passwords[user] if password is None else password,
                                  str(ca or self.cert))
        else:
            client.tls_set(ca_certs=str(ca or self.cert))
        if will:
            client.will_set(*will, qos=1, retain=False)
        done, result = threading.Event(), []
        client.on_connect = lambda _c, _u, _f, reason, _p: (result.append(reason), done.set())
        client.connect('127.0.0.1', self.port, keepalive=8)
        client.loop_start()
        self.addCleanup(client.loop_stop)
        self.addCleanup(client.disconnect)
        self.assertTrue(done.wait(8), 'test client did not get CONNACK')
        return client, result[0]

    def subscribe(self, client, topics):
        done, received = threading.Event(), queue.Queue()
        client.on_message = lambda _c, _u, msg: received.put((msg.topic, bytes(msg.payload)))
        client.on_subscribe = lambda _c, _u, _m, _r, _p: done.set()
        status, _mid = client.subscribe([(topic, 1) for topic in topics])
        self.assertEqual(status, mqtt.MQTT_ERR_SUCCESS)
        self.assertTrue(done.wait(8), 'test subscription did not get SUBACK')
        return received

    def publish(self, client, topic, payload):
        info = client.publish(topic, payload, qos=1, retain=False)
        self.assertEqual(info.rc, mqtt.MQTT_ERR_SUCCESS)
        info.wait_for_publish(timeout=6)
        self.assertTrue(info.is_published(), 'test publish did not receive PUBACK')

    def test_tls_password_and_distinct_robot_topic_permissions(self):
        monitor, reason = self.connect('observer')
        self.assertFalse(reason.is_failure)
        monitor_messages = self.subscribe(monitor, ['agv/+/+'])
        robots = []
        for name in ('robot-1', 'robot-2'):
            robot, reason = self.connect(name)
            self.assertFalse(reason.is_failure)
            robots.append((robot, self.subscribe(robot, ['agv/sim-r1/order', 'agv/sim-r2/order'])))

        allowed = set()
        for index, (robot, _messages) in enumerate(robots, 1):
            for suffix in ('state', 'event', 'conn'):
                topic, payload = 'agv/sim-r{}/{}'.format(index, suffix), secrets.token_hex(12).encode()
                allowed.add((topic, payload))
                self.publish(robot, topic, payload)
        delivered = {monitor_messages.get(timeout=8) for _ in allowed}
        self.assertEqual(delivered, allowed)

        for index, (robot, _messages) in enumerate(robots, 1):
            other = 2 if index == 1 else 1
            for topic in ('agv/sim-r{}/state'.format(other), 'agv/sim-r{}/event'.format(other),
                          'agv/sim-r{}/conn'.format(other), 'agv/sim-r{}/order'.format(index)):
                payload = secrets.token_hex(12).encode()
                self.publish(robot, topic, payload)
            # The broker must process this robot's earlier messages before its
            # authorized barrier; monitor order makes dropped spoofing visible.
            barrier = ('agv/sim-r{}/event'.format(index), secrets.token_hex(12).encode())
            self.publish(robot, *barrier)
            received = monitor_messages.get(timeout=8)
            self.assertEqual(received, barrier, 'a denied publication reached the observer')
        # Still poll briefly; PUBACK alone is never used as ACL evidence.
        with self.assertRaises(queue.Empty):
            monitor_messages.get(timeout=.5)

        commands = []
        for index in (1, 2):
            topic, payload = 'agv/sim-r{}/order'.format(index), secrets.token_hex(12).encode()
            commands.append((topic, payload))
            self.publish(monitor, topic, payload)
        barriers = []
        for index in (1, 2):
            barrier = ('agv/sim-r{}/order'.format(index), secrets.token_hex(12).encode())
            barriers.append(barrier)
            self.publish(monitor, *barrier)
        for index, (_robot, messages) in enumerate(robots):
            self.assertEqual(messages.get(timeout=8), commands[index])
            self.assertEqual(messages.get(timeout=8), barriers[index], 'a cross-robot order was delivered')
            with self.assertRaises(queue.Empty):
                messages.get(timeout=.5)

    def test_own_lwt_is_delivered_but_cross_robot_lwt_is_not(self):
        monitor, reason = self.connect('observer')
        self.assertFalse(reason.is_failure)
        messages = self.subscribe(monitor, ['agv/+/conn'])
        spoof = secrets.token_hex(12).encode()
        robot2, reason = self.connect('robot-2', will=('agv/sim-r1/conn', spoof))
        self.assertFalse(reason.is_failure)
        robot2.socket().shutdown(socket.SHUT_RDWR)
        with self.assertRaises(queue.Empty):
            messages.get(timeout=1.5)
        valid = secrets.token_hex(12).encode()
        robot1, reason = self.connect('robot-1', will=('agv/sim-r1/conn', valid))
        self.assertFalse(reason.is_failure)
        robot1.socket().shutdown(socket.SHUT_RDWR)
        self.assertEqual(messages.get(timeout=8), ('agv/sim-r1/conn', valid))

    def test_plaintext_mqtt_cannot_establish_a_session(self):
        # MQTT 3.1.1 CONNECT, client ID "test", no credentials: a TLS-only
        # listener must not reply with a successful MQTT CONNACK.
        packet = b'\x10\x10\x00\x04MQTT\x04\x02\x00\x05\x00\x04test'
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as sock:
            sock.settimeout(3)
            sock.sendall(packet)
            try:
                data = sock.recv(32)
            except (ConnectionResetError, socket.timeout):
                data = b''
        self.assertNotEqual(data[:4], b'\x20\x02\x00\x00')

    def test_bad_password_anonymous_and_wrong_ca_are_rejected(self):
        for user, password in (('robot-1', secrets.token_urlsafe(32)), (None, None)):
            client, reason = self.connect(user, password=password)
            self.assertTrue(reason.is_failure)
        with self.assertRaises((ssl.SSLError, OSError)):
            self.connect('robot-1', ca=self.other)

    def test_tls_rejects_certificate_for_another_hostname(self):
        context = ssl.create_default_context(cafile=str(self.cert))
        with self.assertRaises(ssl.SSLCertVerificationError):
            with socket.create_connection(('127.0.0.1', self.port), timeout=3) as raw:
                with context.wrap_socket(raw, server_hostname='not-this-broker.invalid'):
                    pass


if __name__ == '__main__':
    unittest.main()
