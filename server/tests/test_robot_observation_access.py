"""Robot observations must not leak to nonlocal or unauthenticated callers."""
import asyncio
import os
import unittest
from unittest.mock import patch

from server.main import app

TOKEN = 'observation-test-token-' + 'x' * 32
PATHS = ('/robots', '/robots/battery', '/robots/diagnostics')


class Service:
    def snapshot(self):
        return {'connected': False, 'ready': False, 'robots': {}}

    def observation_diagnostics(self):
        return {'connected': False, 'ready': False, 'robots': {}}


async def call(path, *, client='127.0.0.1', headers=None):
    sent = []
    done = False
    raw_headers = [(b'host', b'127.0.0.1:8000')] if headers is None else headers
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'GET', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'root_path': '', 'headers': raw_headers,
             'client': (client, 1234), 'server': ('127.0.0.1', 8000)}

    async def receive():
        nonlocal done
        if not done:
            done = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m['type'] == 'http.response.start')
    return start['status'], dict(start['headers'])


class RobotObservationAccessTests(unittest.TestCase):
    def setUp(self):
        self.old = app.state._state.copy()
        app.state.mqtt_service = Service()
        self.addCleanup(lambda: (app.state._state.clear(), app.state._state.update(self.old)))

    def check(self, path, expected, **kwargs):
        status, headers = asyncio.run(call(path, **kwargs))
        self.assertEqual(status, expected, path)
        self.assertEqual(headers.get(b'cache-control'), b'no-store')

    def test_default_allows_only_direct_loopback_with_safe_host(self):
        with patch.dict(os.environ, {}, clear=True):
            for path in PATHS:
                self.check(path, 200)
                self.check(path, 403, client='192.0.2.10')
                self.check(path, 403, headers=[(b'host', b'evil.example')])
                self.check(path, 403, headers=[(b'host', b'127.0.0.1:8000'),
                                               (b'x-forwarded-for', b'192.0.2.10')])
                self.check(path, 403, headers=[(b'host', b'127.0.0.1:8000'),
                                               (b'origin', b'http://evil.example')])
                self.check(path, 403, headers=[(b'host', b'127.0.0.1:8000'),
                                               (b'host', b'127.0.0.1:8000')])
                self.check(path, 403, headers=[(b'host', b'localhost:8000'),
                                               (b'forwarded', b'for=192.0.2.10')])
                self.check(path, 200, headers=[(b'host', b'127.0.0.1:8000'),
                                               (b'origin', b'http://127.0.0.1:8000')])
                self.check(path, 200, client='::1', headers=[(b'host', b'[::1]:8000')])

    def test_configured_token_is_required_even_on_loopback(self):
        with patch.dict(os.environ, {'AGV_OBSERVATION_API_TOKEN': TOKEN}, clear=True):
            for path in PATHS:
                self.check(path, 401)
                self.check(path, 401, headers=[(b'host', b'127.0.0.1:8000'),
                                               (b'authorization', b'Bearer wrong')])
                self.check(path, 401, headers=[(b'host', b'127.0.0.1:8000'),
                                               (b'authorization', ('Bearer ' + TOKEN).encode()),
                                               (b'authorization', ('Bearer ' + TOKEN).encode())])
                self.check(path, 200, headers=[(b'host', b'127.0.0.1:8000'),
                                               (b'authorization', ('Bearer ' + TOKEN).encode())])
                self.check(path, 403, client='192.0.2.10', headers=[
                    (b'host', b'127.0.0.1:8000'),
                    (b'authorization', ('Bearer ' + TOKEN).encode())])

    def test_invalid_config_fails_closed_without_disclosing_it(self):
        with patch.dict(os.environ, {'AGV_OBSERVATION_API_TOKEN': 'too-short'}, clear=True):
            for path in PATHS:
                self.check(path, 503)


if __name__ == '__main__':
    unittest.main()
