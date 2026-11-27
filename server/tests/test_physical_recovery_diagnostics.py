"""Local diagnostics never turn physical claims into motion permission."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from server.main import app
from server.physical_recovery_diagnostics import PhysicalRecoveryDiagnostics
from server.tests.test_physical_observation_exchange import PhysicalReconcileExchangeTests


async def call(path, *, client='127.0.0.1', headers=None):
    output, consumed = [], False
    raw = [(b'host', b'127.0.0.1:8000')] if headers is None else headers
    scope = {'type':'http', 'asgi':{'version':'3.0'}, 'http_version':'1.1', 'method':'GET',
             'scheme':'http', 'path':path, 'raw_path':path.encode(), 'query_string':b'',
             'root_path':'', 'headers':raw, 'client':(client, 1234), 'server':('127.0.0.1', 8000)}
    async def receive():
        nonlocal consumed
        if not consumed:
            consumed = True
            return {'type':'http.request', 'body':b'', 'more_body':False}
        await asyncio.Event().wait()
    async def send(message):
        output.append(message)
    await app(scope, receive, send)
    start = next(message for message in output if message['type'] == 'http.response.start')
    body = b''.join(message.get('body', b'') for message in output if message['type'] == 'http.response.body')
    return start['status'], dict(start['headers']), json.loads(body)


class PhysicalRecoveryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = PhysicalReconcileExchangeTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.old = app.state._state.copy()
        self.addCleanup(lambda: (app.state._state.clear(), app.state._state.update(self.old)))
        self.diag = PhysicalRecoveryDiagnostics(self.fixture.gate, self.fixture.watcher, isolated=True)
        app.state.physical_recovery_diagnostics = self.diag

    def test_confirmed_is_historical_not_motion_proof_and_no_secret_is_exposed(self):
        f = self.fixture
        before = f.store.list_occupancy()
        p = f.service.begin('physical-1', 'T1', 'boot-1')
        commit = f.service.accept_snapshot(f.snapshot(p))
        f.service.accept_ack(f.ack(commit))
        with patch.dict(os.environ, {}, clear=True):
            status, headers, body = asyncio.run(call('/robots/physical-recovery'))
        self.assertEqual(status, 200)
        self.assertEqual(headers[b'cache-control'], b'no-store')
        self.assertEqual(body['scope'], 'isolated_physical_observation')
        row = body['tasks'][0]
        self.assertEqual((row['task_id'], row['robot_id'], row['gate']), ('T1', 'physical-1', 'blocked'))
        self.assertEqual((row['report'], row['signed_record'], row['event_state']),
                         ('recent_report', 'recorded', 'not_observed'))
        self.assertFalse(row['motion_authorized'])
        self.assertFalse(row['physical_stop_proven'])
        content = json.dumps(body)
        for secret in ('boot-1', p['nonce'], p['server_epoch'], p['mac'], 'event-1'):
            self.assertNotIn(secret, content)
        self.assertEqual(f.store.list_occupancy(), before)
        self.assertIn('T1', f.gate._blocked)

    def test_stale_and_disconnected_reports_are_not_reported_as_current(self):
        f = self.fixture
        f.time[0] = 3.0
        self.assertEqual(self.diag.snapshot()['tasks'][0]['report'], 'stale')
        f.watcher._connected.clear()
        self.assertEqual(self.diag.snapshot()['tasks'][0]['report'], 'unavailable')

    def test_signed_record_stays_historical_after_the_watcher_disconnects(self):
        f = self.fixture
        probe = f.service.begin('physical-1', 'T1', 'boot-1')
        commit = f.service.accept_snapshot(f.snapshot(probe))
        f.service.accept_ack(f.ack(commit))
        f.watcher._connected.clear()
        row = self.diag.snapshot()['tasks'][0]
        self.assertEqual((row['report'], row['signed_record']), ('unavailable', 'recorded'))
        self.assertFalse(row['physical_stop_proven'])
        self.assertFalse(row['motion_authorized'])

    def test_replaced_gate_epoch_is_unavailable_and_keeps_occupancy(self):
        from server.recovery import RecoveryGate
        f = self.fixture
        before = f.store.list_occupancy()
        RecoveryGate(f.path).initialize()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(asyncio.run(call('/robots/physical-recovery'))[0], 503)
        self.assertEqual(f.store.list_occupancy(), before)
        self.assertIn('T1', f.gate._blocked)

    def test_replaced_watcher_epoch_or_inconsistent_allocation_fails_closed(self):
        f = self.fixture
        f.gate._physical_watcher = None
        with self.assertRaises(ValueError):
            self.diag.snapshot()
        with patch.dict(os.environ, {}, clear=True):
            status, headers, body = asyncio.run(call('/robots/physical-recovery'))
        self.assertEqual(status, 503)
        self.assertEqual(headers[b'cache-control'], b'no-store')
        self.assertNotIn('boot-1', json.dumps(body))

    def test_http_enforces_existing_direct_loopback_and_optional_token(self):
        token = 'diagnostics-test-' + 'x' * 32
        with patch.dict(os.environ, {'AGV_OBSERVATION_API_TOKEN':token}, clear=True):
            self.assertEqual(asyncio.run(call('/robots/physical-recovery'))[0], 401)
            headers = [(b'host', b'127.0.0.1:8000'), (b'authorization', ('Bearer ' + token).encode())]
            self.assertEqual(asyncio.run(call('/robots/physical-recovery', headers=headers))[0], 200)
            self.assertEqual(asyncio.run(call('/robots/physical-recovery', client='192.0.2.1', headers=headers))[0], 403)
            headers.append((b'x-forwarded-for', b'192.0.2.1'))
            self.assertEqual(asyncio.run(call('/robots/physical-recovery', headers=headers))[0], 403)

    def test_no_explicit_observer_is_unavailable(self):
        app.state.physical_recovery_diagnostics = None
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(asyncio.run(call('/robots/physical-recovery'))[0], 503)


if __name__ == '__main__':
    unittest.main()
