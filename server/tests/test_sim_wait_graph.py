"""Read-only wait-graph contract; never commands or real-robot evidence."""
import asyncio
import unittest

from fastapi import FastAPI
from server.sim_api import router
from server.tests.test_sim_api import RecordingRuntime, request


class WaitGraphApiTest(unittest.TestCase):
    def test_read_only_endpoint_requires_existing_simulation_authentication(self):
        app = FastAPI()
        app.include_router(router)
        app.state.sim_api_token = 'secret-token'
        app.state.sim_runtime = runtime = RecordingRuntime()
        path = '/simulation/wait-graph'
        code, body, headers = asyncio.run(request(app, 'GET', path, token='Bearer secret-token'))
        self.assertEqual((code, body), (200, {'method': 'wait_graph'}))
        self.assertEqual(headers.get(b'cache-control'), b'no-store')
        self.assertEqual(runtime.calls, [('wait_graph', ())])
        for token, host, expected in [(None, '127.0.0.1', 401),
                                      ('Bearer wrong', '127.0.0.1', 401),
                                      ('Bearer secret-token', '192.0.2.10', 403)]:
            code, _, _ = asyncio.run(request(app, 'GET', path, token=token, host=host))
            self.assertEqual(code, expected)
        code, _, _ = asyncio.run(request(app, 'POST', path, token='Bearer secret-token'))
        self.assertEqual(code, 405)
        self.assertEqual(runtime.calls, [('wait_graph', ())])


if __name__ == '__main__':
    unittest.main()
