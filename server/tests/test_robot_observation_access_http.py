"""Actual loopback HTTP authentication for observation endpoints."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from server.tests.test_twin_broker_integration import free_port, stop

ROOT = Path(__file__).resolve().parents[2]
CHILD = '''\
from contextlib import asynccontextmanager
import os
import uvicorn
from server.main import app
class Service:
    def snapshot(self): return {'connected': False, 'ready': False, 'robots': {}}
    def observation_diagnostics(self): return {'connected': False, 'ready': False, 'robots': {}}
@asynccontextmanager
async def local_lifespan(app):
    app.state.mqtt_service = Service()
    yield
app.router.lifespan_context = local_lifespan
uvicorn.run(app, host='127.0.0.1', port=int(os.environ['TEST_HTTP_PORT']),
            proxy_headers=False, log_level='warning')
'''


class RobotObservationHttpTest(unittest.TestCase):
    def test_owned_server_requires_configured_token(self):
        port, token = free_port(), secrets.token_urlsafe(48)
        env = os.environ.copy()
        env.update(AGV_OBSERVATION_API_TOKEN=token, TEST_HTTP_PORT=str(port),
                   PYTHONPATH=os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]))
        opener = build_opener(ProxyHandler({}))
        with tempfile.TemporaryDirectory(prefix='observation-http-') as folder, ExitStack() as cleanup:
            log = cleanup.enter_context(open(Path(folder) / 'server.log', 'w', encoding='utf-8'))
            proc = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', CHILD],
                cwd=ROOT, env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, proc)
            url = 'http://127.0.0.1:{}/robots/diagnostics'.format(port)
            deadline = time.monotonic() + 15
            while True:
                try:
                    opener.open(url, timeout=1)
                except HTTPError as exc:
                    self.assertEqual(exc.code, 401)
                    self.assertEqual(exc.headers.get('Cache-Control'), 'no-store')
                    break
                except URLError:
                    if proc.poll() is not None or time.monotonic() >= deadline:
                        log.flush()
                        self.fail('owned observation server failed to start: ' +
                                  (Path(folder) / 'server.log').read_text(encoding='utf-8', errors='replace'))
                    time.sleep(.05)
                else:
                    self.fail('observation endpoint accepted a request without credentials')
            req = Request(url, headers={'Authorization': 'Bearer ' + token})
            with opener.open(req, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)['robots'], {})


if __name__ == '__main__':
    unittest.main()
