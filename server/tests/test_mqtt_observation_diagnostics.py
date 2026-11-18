"""Server-receipt diagnostics: never a device-liveness or motion claim."""
import asyncio
import json
import unittest
from unittest.mock import patch

from server.main import app
from server.mqtt_client import MqttService
from server.tests.test_mqtt_client import FakeClient, message, reason
from server.tests.test_robot_battery_api import request


class MqttObservationDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.now = 100.0
        self.service = MqttService('localhost', 1883, client=self.client, clock=lambda: self.now)
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_subscribe(self.client, None, 17, [reason(), reason()], None)

    def test_last_delivery_age_separates_absent_retained_and_disconnect(self):
        self.client.on_message(self.client, None, message('agv/r1/state', {'secret': 'payload'}, True))
        self.now = 105.0
        self.client.on_message(self.client, None, message('agv/r1/conn', b'online'))
        self.client.on_message(self.client, None, message('agv/r2/conn', b'offline', True))
        self.now = 107.0
        result = self.service.observation_diagnostics()
        self.assertEqual(result['robots']['r1']['state'],
                         {'age_seconds': 7.0, 'last_delivery': 'retained'})
        self.assertEqual(result['robots']['r1']['connection'],
                         {'age_seconds': 2.0, 'last_delivery': 'non_retained'})
        self.assertEqual(result['robots']['r2']['state'],
                         {'age_seconds': None, 'last_delivery': 'not_received'})
        self.assertEqual(result['robots']['r2']['connection']['last_delivery'], 'retained')
        self.client.on_disconnect(self.client, None, None, reason(), None)
        self.now = 111.0
        result = self.service.observation_diagnostics()
        self.assertFalse(result['connected'])
        self.assertEqual(result['robots']['r1']['state']['age_seconds'], 11.0)
        self.assertEqual(result['robots']['r1']['state']['last_delivery'], 'retained')
        self.assertEqual(result['robots']['r1']['connection']['last_delivery'], 'non_retained')
        self.assertNotIn('secret', json.dumps(result))

    def test_bad_input_counts_are_bounded_and_do_not_refresh_last_valid_age(self):
        self.client.on_message(self.client, None, message('agv/r1/state', {'battery': 0.5}))
        self.now = 102.0
        self.client.on_message(self.client, None, message('agv/r1/state', b'{"secret":'))
        self.client.on_message(self.client, None, message('agv/r1/conn', b'bad-secret'))
        self.client.on_message(self.client, None, message('agv/r1/state', b'x' * (256 * 1024 + 1)))
        self.client.on_message(self.client, None, message('bad/topic', b'private'))
        for index in range(1, 128):
            self.client.on_message(self.client, None, message('agv/r{}/state'.format(index + 1), {}))
        self.client.on_message(self.client, None, message('agv/overflow-secret/state', {}))
        result = self.service.observation_diagnostics()
        self.assertEqual(result['robots']['r1']['state']['age_seconds'], 2.0)
        self.assertEqual(result['dropped'], {'invalid_topic': 1, 'invalid_size': 1,
                                            'invalid_payload': 2, 'robot_limit': 1,
                                            'disconnected': 0})
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(len(result['robots']), 128)
        self.service._diagnostic_drops['invalid_payload'] = 2 ** 31 - 1
        self.client.on_message(self.client, None, message('agv/r1/state', b'{'))
        self.assertEqual(self.service.observation_diagnostics()['dropped']['invalid_payload'], 2 ** 31 - 1)
        self.client.on_disconnect(self.client, None, None, reason(), None)
        self.client.on_message(self.client, None, message('agv/r1/state', {}))
        self.assertEqual(self.service.observation_diagnostics()['dropped']['disconnected'], 1)

    def test_clock_inconsistency_is_unknown_not_falsely_zero(self):
        self.client.on_message(self.client, None, message('agv/r1/state', {}))
        self.now = 99.0
        self.assertIsNone(self.service.observation_diagnostics()['robots']['r1']['state']['age_seconds'])

    def test_diagnostic_clock_does_not_change_existing_twin_clock_domain(self):
        # Twin consumers compare this timestamp with process monotonic time.
        with patch('server.mqtt_client.time.monotonic', return_value=8000.0):
            self.client.on_message(self.client, None, message('agv/r1/state', {}))
        self.now = 103.0
        self.assertEqual(self.service.observation_diagnostics()['robots']['r1']['state']['age_seconds'], 3.0)
        self.assertEqual(self.service.twin_observation('r1')['received_at'], 8000.0)

    def test_api_redacts_payload_preserves_existing_api_and_disables_cache(self):
        old = app.state._state.copy()
        self.addCleanup(lambda: (app.state._state.clear(), app.state._state.update(old)))
        app.state.mqtt_service = self.service
        self.client.on_message(self.client, None, message('agv/r1/state', {'secret': 'private'}))
        status, body, headers = asyncio.run(request('/robots/diagnostics'))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get(b'cache-control'), b'no-store')
        self.assertNotIn(b'private', body)
        self.assertNotIn(b'secret', body)
        self.assertEqual(json.loads(body)['robots']['r1']['state']['age_seconds'], 0.0)
        self.assertEqual(json.loads(asyncio.run(request('/robots'))[1])['robots']['r1']['state'],
                         {'secret': 'private'})
        app.state._state.pop('mqtt_service')
        status, body, headers = asyncio.run(request('/robots/diagnostics'))
        self.assertEqual(status, 503)
        self.assertEqual(headers.get(b'cache-control'), b'no-store')
        self.assertNotIn(b'Traceback', body)
