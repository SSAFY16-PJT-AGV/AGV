import json
import io
import os
import ssl
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from server.mqtt_client import MqttService, start_mqtt


class FakeClient:
    def __init__(self):
        self.subscriptions = []
        self.started = False
        self.stopped = False
        self.disconnected = False

    def connect(self, host, port, keepalive):
        self.connection = (host, port, keepalive)
        return 0

    def loop_start(self):
        self.started = True
        return 0

    def loop_stop(self):
        self.stopped = True
        return 0

    def disconnect(self):
        self.disconnected = True
        return 0

    def subscribe(self, topics):
        self.subscriptions.append(topics)
        return (0, 17)


def reason(failure=False):
    return SimpleNamespace(is_failure=failure)


def message(topic, payload, retained=False):
    if not isinstance(payload, bytes):
        payload = json.dumps(payload).encode('utf-8')
    return SimpleNamespace(topic=topic, payload=payload, retain=retained)


class MqttServiceTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.service = MqttService('localhost', 1883, client=self.client)

    def test_starts_subscribes_and_stops(self):
        self.service.start()
        self.assertTrue(self.client.started)
        self.client.on_connect(self.client, None, None, reason(), None)
        self.assertEqual(self.client.subscriptions, [[('agv/+/state', 0), ('agv/+/conn', 1)]])
        self.assertFalse(self.service.snapshot()['ready'])
        self.client.on_subscribe(self.client, None, 17, [reason(), reason()], None)
        self.assertTrue(self.service.snapshot()['ready'])
        self.service.stop()
        self.assertTrue(self.client.disconnected)
        self.assertTrue(self.client.stopped)
        self.assertFalse(self.service.snapshot()['ready'])

    def test_receives_two_robot_states_and_keeps_retained_origin(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_message(self.client, None, message('agv/r1/state', {'last_node': 'N1'}, True))
        self.client.on_message(self.client, None, message('agv/r2/state', {'last_node': 'N2'}))
        data = self.service.snapshot()['robots']
        self.assertEqual(data['r1']['state'], {'last_node': 'N1'})
        self.assertFalse(data['r1']['state_received_live'])
        self.assertEqual(data['r2']['state'], {'last_node': 'N2'})
        self.assertTrue(data['r2']['state_received_live'])
        data['r2']['state']['last_node'] = 'changed'
        self.assertEqual(self.service.snapshot()['robots']['r2']['state']['last_node'], 'N2')

    def test_retained_connection_message_is_only_an_observation(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_message(self.client, None, message('agv/r1/conn', b'online', True))
        data = self.service.snapshot()['robots']['r1']
        self.assertEqual(data['connection_report'], 'online')
        self.assertFalse(data['connection_received_live'])

    def test_disconnect_preserves_but_marks_observations_not_live(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_message(self.client, None, message('agv/r1/state', {'last_node': 'N1'}))
        self.client.on_message(self.client, None, message('agv/r1/conn', b'online'))
        self.client.on_disconnect(self.client, None, None, reason(), None)
        snapshot = self.service.snapshot()
        self.assertFalse(snapshot['ready'])
        self.assertEqual(snapshot['robots']['r1']['state']['last_node'], 'N1')
        self.assertFalse(snapshot['robots']['r1']['state_received_live'])
        self.assertFalse(snapshot['robots']['r1']['connection_received_live'])

    def test_invalid_messages_do_not_replace_last_valid_state(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_message(self.client, None, message('agv/r1/state', {'last_node': 'N1'}))
        for payload in (b'\xff', b'{', b'[]', b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}'):
            self.client.on_message(self.client, None, message('agv/r1/state', payload))
        self.assertEqual(self.service.snapshot()['robots']['r1']['state'], {'last_node': 'N1'})

    def test_unpaired_surrogates_in_nested_keys_and_values_are_rejected(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        valid = {'name': '로봇', 'values': ['정상']}
        self.client.on_message(self.client, None, message('agv/r1/state', valid))
        for payload in (b'{"name":"\\ud800"}', b'{"nested":{"\\udfff":1}}',
                        b'{"nested":[{"x":"\\ud800"}]}'):
            self.client.on_message(self.client, None, message('agv/r1/state', payload))
            self.assertEqual(self.service.snapshot()['robots']['r1']['state'], valid)

    def test_valid_unicode_and_surrogate_pairs_are_preserved(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        payload = b'{"name":"\\uc548\\ub155", "symbol":"\\ud83d\\ude80"}'
        self.client.on_message(self.client, None, message('agv/r1/state', payload))
        state = self.service.snapshot()['robots']['r1']['state']
        self.assertEqual(state, {'name': '안녕', 'symbol': '🚀'})
        json.dumps(state, ensure_ascii=False).encode('utf-8')

    def test_bad_subscription_does_not_mark_ready(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_subscribe(self.client, None, 17, [reason(), reason(True)], None)
        self.assertFalse(self.service.snapshot()['ready'])

    def test_subscribe_failure_does_not_escape_the_network_callback(self):
        def fail(_):
            raise OSError('disconnected during subscribe')
        self.client.subscribe = fail
        self.client.on_connect(self.client, None, None, reason(), None)
        self.assertFalse(self.service.snapshot()['ready'])

    def test_too_large_or_deep_state_preserves_existing_observation(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_message(self.client, None, message('agv/r1/state', {'last_node': 'N1'}))
        oversized = b'{"data":"' + b'a' * (256 * 1024) + b'"}'
        nested = b'{"data":' + b'[' * 40 + b'0' + b']' * 40 + b'}'
        for payload in (oversized, nested):
            self.client.on_message(self.client, None, message('agv/r1/state', payload))
        self.assertEqual(self.service.snapshot()['robots']['r1']['state'], {'last_node': 'N1'})

    def test_robot_identity_and_count_are_bounded(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_message(self.client, None, message('agv/' + '가' * 43 + '/state', {}))
        self.assertEqual(self.service.snapshot()['robots'], {})
        for index in range(128):
            self.client.on_message(self.client, None, message('agv/r{}/state'.format(index), {'seq': index}))
        self.client.on_message(self.client, None, message('agv/overflow/state', {}))
        self.client.on_message(self.client, None, message('agv/r0/state', {'seq': 999}))
        robots = self.service.snapshot()['robots']
        self.assertEqual(len(robots), 128)
        self.assertNotIn('overflow', robots)
        self.assertEqual(robots['r0']['state'], {'seq': 999})

    def test_stale_suback_not_allowed_to_mark_new_subscription_ready(self):
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_disconnect(self.client, None, None, reason(), None)
        self.client.on_subscribe(self.client, None, 17, [reason(), reason()], None)
        self.assertFalse(self.service.snapshot()['ready'])

    def test_connection_callback_after_stop_does_not_reopen_subscriptions(self):
        self.service.stop()
        self.client.on_connect(self.client, None, None, reason(), None)
        self.assertEqual(self.client.subscriptions, [])
        self.assertFalse(self.service.snapshot()['connected'])

    def test_lifespan_starts_and_stops_service(self):
        import asyncio
        from unittest.mock import patch
        from server.main import app

        async def run():
            with patch('server.main.start_mqtt', return_value=self.service) as start, \
                 patch('server.main.stop_mqtt') as stop:
                async with app.router.lifespan_context(app):
                    start.assert_called_once_with()
                    self.assertIs(app.state.mqtt_service, self.service)
                    stop.assert_not_called()
                stop.assert_called_once_with(self.service)

        asyncio.run(run())


class MqttSecurityConfigTest(unittest.TestCase):
    class RecordingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.events = []
            self.tls_error = None

        def tls_set_context(self, context):
            self.events.append(('tls', context.cafile))
            if self.tls_error:
                raise self.tls_error

        def username_pw_set(self, username, password):
            self.events.append(('auth', username, password))

        def connect(self, host, port, keepalive):
            self.events.append(('connect', host, port, keepalive))
            return super().connect(host, port, keepalive)

    def setUp(self):
        self.client = self.RecordingClient()
        self.base_env = {'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': '1883'}

    def start_with(self, extra=None):
        settings = dict(self.base_env)
        settings.update(extra or {})
        with patch.dict(os.environ, settings, clear=True), \
             patch('server.mqtt_client.load_dotenv'), \
             patch('ssl.create_default_context', side_effect=lambda *, cafile: SimpleNamespace(
                 cafile=cafile, check_hostname=True, verify_mode=ssl.CERT_REQUIRED)), \
             patch('server.mqtt_client.mqtt.Client', return_value=self.client):
            return start_mqtt()

    def test_default_anonymous_connection_remains_supported(self):
        service = self.start_with()
        self.assertIs(service.client, self.client)
        self.assertEqual(self.client.events, [('connect', '127.0.0.1', 1883, 60)])
        self.assertTrue(self.client.started)

    def test_tls_and_authentication_are_configured_before_connect(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.start_with({'MQTT_USERNAME': 'user', 'MQTT_PASSWORD': 'secret',
                             'MQTT_TLS_CA': 'example-ca.pem'})
        self.assertEqual(self.client.events, [('tls', 'example-ca.pem'),
                                             ('auth', 'user', 'secret'),
                                             ('connect', '127.0.0.1', 1883, 60)])
        self.assertNotIn('secret', output.getvalue())

    def test_username_with_tls_and_no_password_is_supported(self):
        self.start_with({'MQTT_USERNAME': 'user', 'MQTT_TLS_CA': 'example-ca.pem'})
        self.assertEqual(self.client.events, [('tls', 'example-ca.pem'),
                                             ('auth', 'user', None),
                                             ('connect', '127.0.0.1', 1883, 60)])

    def test_tls_only_is_supported(self):
        self.start_with({'MQTT_TLS_CA': 'example-ca.pem'})
        self.assertEqual(self.client.events, [('tls', 'example-ca.pem'),
                                             ('connect', '127.0.0.1', 1883, 60)])

    def test_explicit_empty_tls_or_username_never_downgrades_to_anonymous(self):
        for settings in ({'MQTT_TLS_CA': ''}, {'MQTT_TLS_CA': '   '},
                         {'MQTT_USERNAME': ''}, {'MQTT_USERNAME': '   '}):
            with self.subTest(settings=list(settings)):
                self.client = self.RecordingClient()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.start_with(settings)
                self.assertFalse(any(event[0] == 'connect' for event in self.client.events))

    def test_password_without_username_is_rejected_before_connect(self):
        with self.assertRaises((ValueError, RuntimeError)):
            self.start_with({'MQTT_PASSWORD': 'secret', 'MQTT_TLS_CA': 'example-ca.pem'})
        self.assertFalse(any(event[0] == 'connect' for event in self.client.events))

    def test_authenticated_connection_without_tls_is_rejected_before_connect(self):
        for credentials in ({'MQTT_USERNAME': 'user'},
                            {'MQTT_USERNAME': 'user', 'MQTT_PASSWORD': 'secret'},
                            {'MQTT_USERNAME': 'user', 'MQTT_PASSWORD': 'secret', 'MQTT_TLS_CA': ''}):
            with self.subTest(credentials=list(credentials)):
                self.client = self.RecordingClient()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.start_with(credentials)
                self.assertFalse(any(event[0] == 'connect' for event in self.client.events))

    def test_ca_configuration_failure_does_not_connect_or_expose_password(self):
        self.client.tls_error = OSError('invalid CA')
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises((ValueError, RuntimeError)) as caught:
                self.start_with({'MQTT_USERNAME': 'user', 'MQTT_PASSWORD': 'secret-value',
                                 'MQTT_TLS_CA': 'bad-ca.pem'})
        self.assertFalse(any(event[0] == 'connect' for event in self.client.events))
        self.assertNotIn('secret-value', str(caught.exception))
        self.assertNotIn('secret-value', output.getvalue())


if __name__ == '__main__':
    unittest.main()
