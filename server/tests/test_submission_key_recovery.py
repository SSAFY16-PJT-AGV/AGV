"""Read-only recovery of simulated order submissions by their original key."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server.order_demo_admission import OrderAdmission
from server.order_demo_app import PRODUCTS, app
from server.order_persistence import PersistentOrderStore
from server.order_persistent_admission import PersistentAdmission


async def get_by_key(keys=(), *, client='127.0.0.1', host='127.0.0.1:8000'):
    path = '/order-demo/api/submission-lookup'
    headers = [(b'host', host.encode())] + [(b'idempotency-key', key.encode()) for key in keys]
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'GET',
             'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'',
             'root_path': '', 'headers': headers, 'client': (client, 50000), 'server': ('127.0.0.1', 8000)}
    messages = []
    delivered = False
    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await asyncio.Event().wait()
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return start['status'], dict(start['headers']), json.loads(body)


class VolatileLookupTest(unittest.TestCase):
    def test_lookup_does_not_execute_or_repeat_a_queued_operation(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def operation():
            calls.append('started')
            entered.set()
            release.wait(3)
            return {'status': 'moving'}
        admission = OrderAdmission(wait_seconds=0)
        self.addCleanup(lambda: (release.set(), admission.shutdown(), admission.join(3)))
        pending = admission.submit(operation, 'product:red', request_key='key-1')
        self.assertTrue(entered.wait(1))
        self.assertEqual(admission.get_by_key('key-1'), pending)
        self.assertEqual(calls, ['started'])
        with self.assertRaises(KeyError):
            admission.get_by_key('unseen')
        release.set()
        self.assertTrue(admission.join(3))
        recovered = admission.get_by_key('key-1')
        self.assertEqual(recovered['status'], 'completed')
        self.assertEqual(recovered['request_id'], pending['request_id'])
        self.assertEqual(calls, ['started'])


class DurableLookupTest(unittest.TestCase):
    def test_key_lookup_survives_reopen_without_restarting_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'orders.sqlite3'
            store = PersistentOrderStore(path, 'map@v1', PRODUCTS)
            original = store.submit('red', 'browser-key')
            reopened = PersistentOrderStore(path, 'map@v1', PRODUCTS)
            calls = []
            admission = PersistentAdmission(reopened, lambda row: calls.append(row), threading.Event())
            recovered = admission.get_by_key('browser-key')
            self.assertEqual(recovered, {'status': 'pending', 'request_id': original['request_id']})
            self.assertEqual(calls, [])
            self.assertEqual(reopened.list_orders()[0]['state'], 'queued')
            with self.assertRaises(KeyError):
                admission.get_by_key('absent')

    def test_interrupted_claim_remains_unknown_without_a_new_order(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'orders.sqlite3'
            store = PersistentOrderStore(path, 'map@v1', PRODUCTS)
            original = store.submit('red', 'lost-response')
            store.claim(original['request_id'])
            reopened = PersistentOrderStore(path, 'map@v1', PRODUCTS)
            reopened.recover_interrupted()
            admission = PersistentAdmission(reopened, lambda _row: self.fail('must not dispatch'), threading.Event())
            recovered = admission.get_by_key('lost-response')
            self.assertEqual(recovered, {'status': 'unknown', 'request_id': original['request_id']})
            self.assertEqual(len(reopened.list_orders()), 1)
            self.assertEqual(reopened.submit('red', 'lost-response')['request_id'], original['request_id'])


class LookupApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old = getattr(app.state, 'demo', None)
        self.admission = OrderAdmission(wait_seconds=1)
        self.admission.submit(lambda: {'status': 'moving'}, 'product:red', request_key='browser-key')
        app.state.demo = type('Demo', (), {'admission': self.admission})()

    def tearDown(self):
        self.admission.shutdown()
        self.admission.join(2)
        app.state.demo = self.old

    async def test_local_lookup_is_no_store_and_key_does_not_appear_in_url(self):
        code, headers, body = await get_by_key(['browser-key'])
        self.assertEqual(code, 200)
        self.assertEqual(headers.get(b'cache-control'), b'no-store')
        self.assertEqual(body['status'], 'completed')
        self.assertEqual(body['result'], {'status': 'moving'})

    async def test_missing_duplicate_and_remote_are_rejected(self):
        self.assertEqual((await get_by_key())[0], 422)
        self.assertEqual((await get_by_key(['a', 'b']))[0], 422)
        code, headers, _ = await get_by_key(['absent'])
        self.assertEqual(code, 404)
        self.assertEqual(headers.get(b'cache-control'), b'no-store')
        self.assertEqual((await get_by_key(['browser-key'], client='192.0.2.1'))[0], 403)


if __name__ == '__main__':
    unittest.main()
