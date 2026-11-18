"""Authenticated, bounded simulation reference API; never a control command."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from server.main import load_sim_runtime
from server.sim_api import router
from server.sim_runtime import SimRuntime
from server.tests.test_occurrence_allocation_lab import laboratory_map
from server.tests.test_sim_api import RecordingRuntime


async def request(app, method, path, *, payload=b'', token='Bearer secret-token',
                  host='127.0.0.1', query=b'', chunks=None):
    messages, received = [], []
    headers = [(b'content-type', b'application/json')]
    if token is not None:
        headers.append((b'authorization', token.encode()))
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': query, 'root_path': '', 'headers': headers,
             'client': (host, 1234), 'server': ('localhost', 8000)}
    parts = list(chunks if chunks is not None else [payload])
    async def receive():
        received.append(True)
        if parts:
            body = parts.pop(0)
            if isinstance(body, BaseException):
                raise body
            return {'type': 'http.request', 'body': body, 'more_body': bool(parts)}
        return {'type': 'http.disconnect'}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    raw = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return start['status'], json.loads(raw), dict(start['headers']), len(received)


class InvestigationApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.state.sim_api_token = 'secret-token'
        self.runtime = RecordingRuntime()
        self.app.state.sim_runtime = self.runtime
        self.path = '/simulation/tasks/T1/investigation-notes'
        self.body = json.dumps({'request_id': 'a' * 32, 'category': 'connection'}).encode()

    def call(self, method, **kwargs):
        return asyncio.run(request(self.app, method, self.path, **kwargs))

    def test_authentication_precedes_body_and_requires_local_client(self):
        for token, host, expected in ((None, '127.0.0.1', 401),
                                      ('Bearer wrong', '127.0.0.1', 401),
                                      ('Bearer secret-token', '192.0.2.5', 403)):
            with self.subTest(host=host, token=token):
                code, _body, headers, reads = self.call('POST', payload=self.body, token=token, host=host)
                self.assertEqual(code, expected)
                self.assertEqual(reads, 0)
                self.assertEqual(headers[b'cache-control'], b'no-store')
        self.assertEqual(self.runtime.calls, [])

    def test_valid_post_and_bounded_get_dispatch(self):
        code, response, headers, _ = self.call('POST', payload=self.body)
        self.assertEqual((code, response), (200, {'method': 'record_investigation_note'}))
        self.assertEqual(self.runtime.calls[-1], ('record_investigation_note', ('T1', 'a' * 32, 'connection')))
        self.assertEqual(headers[b'cache-control'], b'no-store')
        code, response, _headers, _ = self.call('GET', query=b'after_id=5&limit=2')
        self.assertEqual((code, response), (200, {'method': 'list_investigation_notes'}))
        self.assertEqual(self.runtime.calls[-1], ('list_investigation_notes', ('T1', 5, 2)))

    def test_invalid_duplicate_extra_and_oversize_body_do_not_leak_or_dispatch(self):
        cases = [b'{', b'{"request_id":"' + b'a' * 32 + b'","category":"other","category":"action"}',
                 json.dumps({'request_id': 'a' * 32, 'category': 'private-secret'}).encode(),
                 json.dumps({'request_id': 'a' * 32, 'category': 'other', 'text': 'private-secret'}).encode(),
                 json.dumps({'request_id': 'A' * 32, 'category': 'other'}).encode()]
        for payload in cases:
            with self.subTest(payload=payload[:24]):
                code, body, headers, _ = self.call('POST', payload=payload)
                self.assertEqual(code, 422)
                self.assertNotIn('private-secret', repr(body))
                self.assertEqual(headers[b'cache-control'], b'no-store')
        code, body, headers, reads = self.call('POST', chunks=[b'x' * 300, b'y' * 300, b'not-read'])
        self.assertEqual(code, 413)
        self.assertEqual(reads, 2)
        self.assertNotIn('not-read', repr(body))
        self.assertEqual(headers[b'cache-control'], b'no-store')
        self.assertEqual(self.runtime.calls, [])

    def test_query_bounds_and_backend_errors_are_sanitized(self):
        for query in (b'limit=0', b'limit=21', b'after_id=-1', b'after_id=9223372036854775808'):
            with self.subTest(query=query):
                code, _body, headers, _ = self.call('GET', query=query)
                self.assertEqual(code, 422)
                self.assertEqual(headers[b'cache-control'], b'no-store')
        self.assertEqual(self.runtime.calls, [])
        self.runtime.failure = RuntimeError('secret database contents')
        code, body, headers, _ = self.call('POST', payload=self.body)
        self.assertEqual(code, 503)
        self.assertNotIn('secret database contents', repr(body))
        self.assertEqual(headers[b'cache-control'], b'no-store')

    def test_stream_failure_is_sanitized_without_swallowing_cancellation(self):
        code, body, headers, _ = self.call('POST', chunks=[OSError('sensitive-stream-failure')])
        self.assertEqual(code, 422)
        self.assertNotIn('sensitive-stream-failure', repr(body))
        self.assertEqual(headers[b'cache-control'], b'no-store')
        code, body, headers, _ = self.call('POST', chunks=[])
        self.assertEqual(code, 422)
        self.assertEqual(headers[b'cache-control'], b'no-store')
        with self.assertRaises(asyncio.CancelledError):
            self.call('POST', chunks=[asyncio.CancelledError()])
        self.assertEqual(self.runtime.calls, [])


class InvestigationConfigurationTests(unittest.TestCase):
    def test_explicit_bool_and_default(self):
        with tempfile.TemporaryDirectory(prefix='sim-notes-config-') as folder:
            root = Path(folder)
            options = dict(port=18883, client_id='test')
            def runtime(**kwargs):
                return SimRuntime(root / 'state.sqlite', laboratory_map(),
                                  {'sim-r1': {'T1': 'boot-1'}}, **options, **kwargs)
            self.assertFalse(runtime()._investigation_notes_enabled)
            self.assertTrue(runtime(investigation_notes=True)._investigation_notes_enabled)
            for value in ('true', 1, None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    runtime(investigation_notes=value)
            map_path = root / 'map.json'
            map_path.write_text(json.dumps(laboratory_map()), encoding='utf-8')
            config_path = root / 'config.json'
            config = {'database': 'state.sqlite', 'map_file': 'map.json',
                      'robots': {'sim-r1': {'T1': 'boot-1'}}, 'mqtt_port': 18883,
                      'mqtt_client_id': 'test', 'investigation_notes': True}
            with patch.dict(os.environ, {'AGV_SIM_CONFIG': str(config_path),
                                         'AGV_SIM_API_TOKEN': 'a' * 32}, clear=True):
                config_path.write_text(json.dumps(config), encoding='utf-8')
                loaded, _ = load_sim_runtime()
                self.assertTrue(loaded._investigation_notes_enabled)
                config['investigation_notes'] = 1
                config_path.write_text(json.dumps(config), encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_sim_runtime()


if __name__ == '__main__':
    unittest.main()
