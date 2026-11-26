import asyncio
import json
import unittest

from server.main import app
from server.mqtt_client import MqttService
from server.tests.test_mqtt_client import FakeClient, message, reason


async def request(path):
    messages = []
    received = False
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'GET', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'root_path': '', 'headers': [(b'host', b'127.0.0.1:8000')],
             'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 8000)}

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    first = next(m for m in messages if m['type'] == 'http.response.start')
    body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return first['status'], body, dict(first.get('headers', []))


class RobotBatteryApiTest(unittest.TestCase):
    def setUp(self):
        self.old_state = app.state._state.copy()
        self.client = FakeClient()
        self.service = MqttService('localhost', 1883, client=self.client)
        app.state.mqtt_service = self.service
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_subscribe(self.client, None, 17, [reason(), reason()], None)

    def tearDown(self):
        app.state._state.clear()
        app.state._state.update(self.old_state)

    def state(self, robot, battery, retained=False):
        self.client.on_message(self.client, None, message(
            'agv/{}/state'.format(robot), battery, retained))

    def test_summary_distinguishes_zero_null_missing_invalid_and_no_state(self):
        self.state('zero', {'battery': 0})
        self.state('unknown', {'battery': None}, retained=True)
        self.state('missing', {'status': 'IDLE'})
        self.state('invalid', {'battery': True})
        self.client.on_message(self.client, None, message('agv/not-seen/conn', b'online'))
        self.client.on_message(self.client, None, message('agv/unknown/conn', b'offline', True))
        status, body, headers = asyncio.run(request('/robots/battery'))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get(b'cache-control'), b'no-store')
        data = json.loads(body)
        self.assertEqual(data['robots']['zero'],
                         {'status': 'reported', 'value': 0, 'ratio_range': True,
                          'state_received_live': True, 'connection_report': None,
                          'connection_received_live': False})
        self.assertEqual(data['robots']['unknown'],
                         {'status': 'unknown', 'value': None, 'ratio_range': None,
                          'state_received_live': False, 'connection_report': 'offline',
                          'connection_received_live': False})
        self.assertEqual(data['robots']['missing'],
                         {'status': 'not_reported', 'value': None, 'ratio_range': None,
                          'state_received_live': True, 'connection_report': None,
                          'connection_received_live': False})
        self.assertEqual(data['robots']['invalid'],
                         {'status': 'invalid', 'value': None, 'ratio_range': None,
                          'state_received_live': True, 'connection_report': None,
                          'connection_received_live': False})
        self.assertEqual(data['robots']['not-seen'],
                         {'status': 'no_state', 'value': None, 'ratio_range': None,
                          'state_received_live': False, 'connection_report': 'online',
                          'connection_received_live': True})

    def test_disconnected_report_is_historical_and_unfamiliar_numbers_are_preserved(self):
        self.state('r1', {'battery': 0.75})
        self.state('r2', {'battery': 1.01})
        self.client.on_disconnect(self.client, None, None, reason(), None)
        status, body, _ = asyncio.run(request('/robots/battery'))
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data['connected'])
        self.assertEqual(data['robots']['r1'],
                         {'status': 'reported', 'value': 0.75, 'ratio_range': True,
                          'state_received_live': False, 'connection_report': None,
                          'connection_received_live': False})
        self.assertEqual(data['robots']['r2']['status'], 'reported')
        self.assertEqual(data['robots']['r2']['value'], 1.01)
        self.assertFalse(data['robots']['r2']['ratio_range'])

    def test_missing_mqtt_service_returns_503_without_details(self):
        app.state._state.pop('mqtt_service', None)
        status, body, headers = asyncio.run(request('/robots/battery'))
        self.assertEqual(status, 503)
        self.assertNotIn(b'Traceback', body)
        self.assertEqual(headers.get(b'cache-control'), b'no-store')


if __name__ == '__main__':
    unittest.main()
