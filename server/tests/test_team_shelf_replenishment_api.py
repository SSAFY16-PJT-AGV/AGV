"""Synthetic shortage endpoints are explicit, local and key-correlated."""
import json
import unittest

from server.order_demo_app import app
from server.tests.test_order_demo_app import request
from server.tests.test_order_submission_responsiveness import request_with_key


class Service:
    def __init__(self):
        self.calls = []

    def capture(self, *, threshold):
        self.calls.append(('capture', threshold))
        return {'milk': {'status': 'shortage', 'known_count': 0}}

    def submit(self, product_id, *, threshold, approved, request_key):
        self.calls.append(('submit', product_id, threshold, approved, request_key))
        return {'status': 'pending', 'request_id': 'r1'}


class Demo:
    def __init__(self):
        self.synthetic_replenishment = Service()


class ReplenishmentApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old = getattr(app.state, 'demo', None)
        self.demo = Demo()
        app.state.demo = self.demo

    async def asyncTearDown(self):
        app.state.demo = self.old

    async def test_capture_and_approval_require_local_origin_and_key(self):
        path = '/order-demo/api/synthetic-shortages'
        code, _ = await request('POST', path, {'threshold': 1}, 'http://evil.example')
        self.assertEqual(code, 403)
        self.assertEqual(self.demo.synthetic_replenishment.calls, [])
        code, body = await request('POST', path, {'threshold': 1}, 'http://127.0.0.1:8000')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['products']['milk']['status'], 'shortage')
        approval = '/order-demo/api/synthetic-replenishment'
        data = {'product_id': 'milk', 'threshold': 1, 'approved': True}
        code, _ = await request('POST', approval, data, 'http://127.0.0.1:8000')
        self.assertEqual(code, 422)
        code, body = await request_with_key('POST', approval, data, 'http://127.0.0.1:8000', 'key-1')
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(body)['request_id'], 'r1')
        self.assertEqual(self.demo.synthetic_replenishment.calls[-1], ('submit', 'milk', 1, True, 'key-1'))

    async def test_off_and_invalid_requests_do_not_call_service(self):
        path = '/order-demo/api/synthetic-replenishment'
        data = {'product_id': 'milk', 'threshold': 1, 'approved': False}
        code, _ = await request_with_key('POST', path, data, 'http://127.0.0.1:8000', 'key')
        self.assertEqual(code, 422)
        self.assertEqual(self.demo.synthetic_replenishment.calls, [])
        app.state.demo = object()
        code, _ = await request('POST', '/order-demo/api/synthetic-shortages', {'threshold': 1},
                                'http://127.0.0.1:8000')
        self.assertEqual(code, 404)


if __name__ == '__main__':
    unittest.main()
