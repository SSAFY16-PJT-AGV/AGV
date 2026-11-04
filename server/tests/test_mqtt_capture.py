import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.mqtt_capture import CaptureError, CaptureSession


def reason(value=1, failure=False):
    return SimpleNamespace(value=value, is_failure=failure)


class Recorder:
    def __init__(self):
        self.events = []
        self.fail = False

    def record(self, *args):
        if self.fail:
            raise OSError('disk full')
        self.events.append(args)


class Client:
    def __init__(self):
        self.events = []
        self.script = lambda: (_ for _ in ()).throw(KeyboardInterrupt())

    def connect(self, host, port, keepalive):
        self.events.append(('connect', host, port, keepalive))
        return 0

    def username_pw_set(self, username, password):
        self.events.append(('auth', username, password))

    def tls_set(self, ca_certs):
        self.events.append(('tls', ca_certs))

    def subscribe(self, topic, qos):
        self.events.append(('subscribe', topic, qos))
        return (0, 5)

    def disconnect(self):
        self.events.append(('disconnect',))
        return 0

    def loop(self, timeout):
        return self.script()


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.recorder = Recorder()
        self.session = CaptureSession(self.client, self.recorder)

    def connect_and_ack(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_subscribe(self.client, None, 5, [reason()], None)

    def test_subscribes_all_user_topics_and_records_exact_received_bytes(self):
        self.connect_and_ack()
        self.assertIn(('subscribe', '#', 1), self.client.events)
        msg = SimpleNamespace(topic='agv/r1/state', payload=b'\x00\xff', qos=0, retain=True)
        self.client.on_message(self.client, None, msg)
        self.assertEqual(self.recorder.events, [('agv/r1/state', b'\x00\xff', 0, True)])

    def test_message_before_suback_is_recorded(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        msg = SimpleNamespace(topic='fleet/map', payload=b'{}', qos=1, retain=True)
        self.client.on_message(self.client, None, msg)
        self.client.on_subscribe(self.client, None, 5, [reason()], None)
        self.assertEqual(len(self.recorder.events), 1)
        self.assertTrue(self.session.ready)

    def test_denied_or_downgraded_subscription_fails_and_disconnects(self):
        for code in (reason(failure=True), reason(value=0)):
            session = CaptureSession(Client(), Recorder())
            session.client.on_connect(session.client, None, None, reason(), None)
            session.client.on_subscribe(session.client, None, 5, [code], None)
            self.assertFalse(session.ready)
            self.assertIsNotNone(session.error)
            self.assertIn(('disconnect',), session.client.events)

    def test_record_write_failure_stops_collection_and_reports_failure(self):
        self.connect_and_ack()
        self.recorder.fail = True
        msg = SimpleNamespace(topic='agv/r1/state', payload=b'{}', qos=0, retain=False)
        self.client.on_message(self.client, None, msg)
        self.assertIsNotNone(self.session.error)
        self.assertIn(('disconnect',), self.client.events)
        self.recorder.fail = False
        self.client.on_message(self.client, None, msg)
        self.assertEqual(self.recorder.events, [])

    def test_network_disconnect_is_a_failure_not_silent_reconnect(self):
        self.connect_and_ack()
        self.client.on_disconnect(self.client, None, None, reason(failure=True), None)
        self.assertFalse(self.session.ready)
        self.assertIsNotNone(self.session.error)
        self.assertIn(('disconnect',), self.client.events)
        before = list(self.client.events)
        self.client.on_connect(self.client, None, None, reason(), None)
        self.assertEqual(self.client.events, before)

    def test_control_c_stops_normally(self):
        def acknowledged_then_stop():
            self.connect_and_ack()
            raise KeyboardInterrupt()
        self.client.script = acknowledged_then_stop
        self.session.run('localhost', 1883)
        self.assertIn(('disconnect',), self.client.events)

    def test_control_c_before_subscription_ack_is_reported_as_failure(self):
        with self.assertRaises(CaptureError):
            self.session.run('localhost', 1883)

    def test_subscription_error_raises_from_run(self):
        def fail():
            self.client.on_connect(self.client, None, None, reason(), None)
            self.client.on_subscribe(self.client, None, 5, [reason(value=0)], None)
            return 0
        self.client.script = fail
        with self.assertRaises(CaptureError):
            self.session.run('localhost', 1883)

    def test_credentials_are_passed_before_connect(self):
        def acknowledged_then_stop():
            self.connect_and_ack()
            raise KeyboardInterrupt()
        self.client.script = acknowledged_then_stop
        self.session.run('localhost', 1883, username='user', password='pass', tls_ca='ca.pem')
        self.assertEqual(self.client.events[0], ('tls', 'ca.pem'))
        self.assertEqual(self.client.events[1], ('auth', 'user', 'pass'))
        self.assertEqual(self.client.events[2][0], 'connect')

    def test_credentials_without_tls_are_rejected_before_connect(self):
        with self.assertRaises(CaptureError):
            self.session.run('localhost', 1883, username='user', password='pass')
        self.assertFalse(any(item[0] in ('auth', 'connect') for item in self.client.events))

    def test_password_without_username_is_rejected_before_connect(self):
        with self.assertRaises(CaptureError):
            self.session.run('localhost', 1883, password='pass', tls_ca='ca.pem')
        self.assertFalse(any(item[0] in ('auth', 'connect') for item in self.client.events))

    def test_tls_setup_failure_is_rejected_before_connect(self):
        def fail(ca_certs):
            raise OSError('invalid CA')
        self.client.tls_set = fail
        with self.assertRaises(CaptureError):
            self.session.run('localhost', 1883, username='user', password='pass', tls_ca='ca.pem')
        self.assertFalse(any(item[0] in ('auth', 'connect') for item in self.client.events))

    def test_interrupt_during_write_marks_incomplete_capture(self):
        self.connect_and_ack()
        self.recorder.record = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        msg = SimpleNamespace(topic='agv/r1/state', payload=b'{}', qos=0, retain=False)
        self.client.on_message(self.client, None, msg)
        self.assertIsNotNone(self.session.error)
        self.assertIn(('disconnect',), self.client.events)

    def test_interrupted_recording_propagates_failure_from_run(self):
        self.recorder.record = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        def receive_then_interrupt():
            self.connect_and_ack()
            msg = SimpleNamespace(topic='agv/r1/state', payload=b'{}', qos=0, retain=False)
            self.client.on_message(self.client, None, msg)
            return 0
        self.client.script = receive_then_interrupt
        with self.assertRaises(CaptureError):
            self.session.run('localhost', 1883)

    def test_unacknowledged_subscription_does_not_wait_forever(self):
        ticks = iter([100.0, 111.0])
        session = CaptureSession(self.client, self.recorder, clock=lambda: next(ticks))
        self.client.script = lambda: 0
        with self.assertRaises(CaptureError):
            session.run('localhost', 1883)
        self.assertIn(('disconnect',), self.client.events)


if __name__ == '__main__':
    unittest.main()
