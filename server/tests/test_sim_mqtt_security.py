"""Simulation MQTT security configuration tests without any broker connection."""
import os
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.sim_runtime import SimRuntime


class RecordingClient:
    def __init__(self, *args, **kwargs):
        self.options = kwargs
        self.events = []
        self.connect_timeout = None
        self.tls_error = None

    def tls_set_context(self, context):
        self.events.append(('tls', context.cafile))
        if self.tls_error is not None:
            raise self.tls_error

    def username_pw_set(self, username, password):
        self.events.append(('auth', username, password))

    def connect(self, host, port, keepalive):
        self.events.append(('connect', host, port, keepalive))
        return 0


def runtime(**kwargs):
    return SimRuntime('unused.sqlite3', {}, {'sim-r1': {'T1': 'boot-1'}},
                      port=18883, client_id='security-test', **kwargs)


class SimMqttSecurityTest(unittest.TestCase):
    def setUp(self):
        patcher = patch('ssl.create_default_context', side_effect=lambda *, cafile: SimpleNamespace(
            cafile=cafile, check_hostname=True, verify_mode=ssl.CERT_REQUIRED))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_receiver_and_one_shot_publisher_both_get_tls_and_credentials(self):
        settings = {'MQTT_USERNAME': 'user', 'MQTT_PASSWORD': 'secret',
                    'MQTT_TLS_CA': 'test-ca.pem'}
        with patch.dict(os.environ, settings, clear=True), \
             patch('server.sim_runtime.mqtt.Client', side_effect=RecordingClient):
            instance = runtime()
            receiver = instance._new_client(client_id='security-test', persistent=True)
            publisher = instance._new_client()
            for client in (receiver, publisher):
                client.connect(instance.host, instance.port, keepalive=15)
                self.assertEqual(client.events, [('tls', 'test-ca.pem'), ('auth', 'user', 'secret'),
                                                 ('connect', '127.0.0.1', 18883, 15)])
                self.assertIs(client.options['reconnect_on_failure'], False)
            self.assertIs(receiver.options['clean_session'], False)
            self.assertIs(publisher.options['clean_session'], True)

    def test_absent_security_configuration_preserves_loopback_anonymous_mode(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch('server.sim_runtime.mqtt.Client', side_effect=RecordingClient):
            instance = runtime()
            client = instance._new_client()
            client.connect(instance.host, instance.port, keepalive=15)
            self.assertEqual(client.events, [('connect', '127.0.0.1', 18883, 15)])
            self.assertIs(client.options['reconnect_on_failure'], False)

    def test_tls_without_credentials_is_allowed(self):
        with patch.dict(os.environ, {'MQTT_TLS_CA': 'test-ca.pem'}, clear=True), \
             patch('server.sim_runtime.mqtt.Client', side_effect=RecordingClient):
            client = runtime()._new_client()
            self.assertEqual(client.events, [('tls', 'test-ca.pem')])

    def test_invalid_authentication_combinations_never_connect(self):
        cases = ({'MQTT_USERNAME': 'user'}, {'MQTT_PASSWORD': 'secret'},
                 {'MQTT_TLS_CA': ''}, {'MQTT_TLS_CA': '   '},
                 {'MQTT_USERNAME': ''}, {'MQTT_USERNAME': '   '},
                 {'MQTT_USERNAME': 'user', 'MQTT_TLS_CA': ''})
        for settings in cases:
            client = RecordingClient()
            with self.subTest(settings=list(settings)), \
                 patch.dict(os.environ, settings, clear=True), \
                 patch('server.sim_runtime.mqtt.Client', return_value=client):
                with self.assertRaises((ValueError, RuntimeError)):
                    runtime()._new_client()
                self.assertFalse(any(event[0] == 'connect' for event in client.events))

    def test_tls_failure_does_not_connect_or_disclose_credentials(self):
        client = RecordingClient()
        client.tls_error = OSError('invalid CA')
        settings = {'MQTT_USERNAME': 'user', 'MQTT_PASSWORD': 'secret-value',
                    'MQTT_TLS_CA': 'bad-ca.pem'}
        with patch.dict(os.environ, settings, clear=True), \
             patch('server.sim_runtime.mqtt.Client', return_value=client):
            with self.assertRaises((ValueError, RuntimeError)) as caught:
                runtime()._new_client()
        self.assertFalse(any(event[0] == 'connect' for event in client.events))
        self.assertNotIn('secret-value', str(caught.exception))

    def test_unreadable_ca_is_rejected_before_network_connection(self):
        client = RecordingClient()
        settings = {'MQTT_USERNAME': 'user', 'MQTT_PASSWORD': 'secret-value',
                    'MQTT_TLS_CA': 'bad-ca.pem'}
        with patch.dict(os.environ, settings, clear=True), \
             patch('ssl.create_default_context', side_effect=OSError('CA read error')), \
             patch('server.sim_runtime.mqtt.Client', return_value=client):
            with self.assertRaises((ValueError, RuntimeError)) as caught:
                runtime()._new_client()
        self.assertFalse(any(event[0] == 'connect' for event in client.events))
        self.assertNotIn('secret-value', str(caught.exception))

    def test_non_loopback_broker_remains_rejected(self):
        with self.assertRaises(ValueError):
            runtime(host='example.com')


if __name__ == '__main__':
    unittest.main()
