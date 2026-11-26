"""Reroute proposals inherit the local read-only simulation API policy."""
import asyncio
import unittest
from fastapi import FastAPI
from server.sim_api import router
from server.tests.test_sim_api import RecordingRuntime, request


class RerouteApiTest(unittest.TestCase):
    def test_local_token_and_read_only(self):
        app = FastAPI()
        app.include_router(router)
        app.state.sim_api_token = 'secret-token'
        app.state.sim_runtime = runtime = RecordingRuntime()
        path = '/simulation/tasks/T1/reroute-candidate'
        code, body, headers = asyncio.run(request(app, 'GET', path, token='Bearer secret-token'))
        self.assertEqual((code, body), (200, {'method': 'reroute_candidate'}))
        self.assertEqual(headers.get(b'cache-control'), b'no-store')
        for token, host, expected in [(None, '127.0.0.1', 401),
                                      ('Bearer wrong', '127.0.0.1', 401),
                                      ('Bearer secret-token', '192.0.2.10', 403)]:
            code, _, _ = asyncio.run(request(app, 'GET', path, token=token, host=host))
            self.assertEqual(code, expected)
        code, _, _ = asyncio.run(request(app, 'POST', path, token='Bearer secret-token'))
        self.assertEqual(code, 405)
        self.assertEqual(runtime.calls, [('reroute_candidate', ('T1',))])
