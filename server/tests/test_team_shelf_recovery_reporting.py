"""Unverified persisted results must never clear the original browser key."""
import json
import threading
import unittest
from types import SimpleNamespace

from server.order_demo_admission import AdmissionStopped
from server.order_demo_app import DemoRuntime, app
from server.tests.test_order_demo_app import request
from server.tests.test_submission_key_recovery import get_by_key


class _Journal:
    row = {'request_id': 'r1', 'product_id': 'milk', 'state': 'completed',
           'result': {'assigned_robot': 'sim-r1', 'shelf_id': 'N02', 'slot_index': 0}}
    def list_orders(self): return [dict(self.row)]
    def get_order(self, request_id):
        if request_id != 'r1': raise KeyError(request_id)
        return dict(self.row)


class _Admission:
    def get_by_key(self, key):
        if key != 'existing': raise KeyError(key)
        return {'request_id': 'r1', 'status': 'completed', 'result': {'status': 'moving'}}
    def get(self, request_id):
        if request_id != 'r1': raise KeyError(request_id)
        return self.get_by_key('existing')
    def submit_product(self, *_args):
        raise AssertionError('blocked recovery must not submit a new order')


class RecoveryReportingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous = getattr(app.state, 'demo', None)
        self.demo = DemoRuntime.__new__(DemoRuntime)
        self.demo.team_shelves = True
        self.demo._restart_blocked = True
        self.demo.products = [{'id': 'milk'}]
        self.demo.journal = _Journal()
        self.demo.admission = _Admission()
        app.state.demo = self.demo

    def tearDown(self): app.state.demo = self.previous

    async def test_unverified_results_remain_unknown_in_all_read_views(self):
        code, _, lookup = await get_by_key(['existing'])
        self.assertEqual(code, 200)
        self.assertEqual(lookup, {'request_id': 'r1', 'status': 'unknown'})
        for path in ('/order-demo/api/orders/r1', '/order-demo/api/submissions/r1'):
            code, body = await request('GET', path)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)['status'], 'unknown')
        code, body = await request('GET', '/order-demo/api/orders')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['orders'][0]['status'], 'unknown')
        self.assertEqual((await request('GET', '/order-demo/api/metrics'))[0], 503)

    async def test_blocked_post_cannot_submit_or_confirm_a_new_order(self):
        self.assertEqual(self.demo.submit_product_order('milk', 'existing'),
                         {'request_id': 'r1', 'status': 'unknown'})
        with self.assertRaises(AdmissionStopped):
            self.demo.submit_product_order('milk', 'new')

    async def test_restart_waits_for_same_world_before_reporting_or_accepting(self):
        self.demo._restart_blocked = False
        self.demo._world_ready = threading.Event()
        code, _, lookup = await get_by_key(['existing'])
        self.assertEqual((code, lookup['status']), (200, 'unknown'))
        code, body = await request('GET', '/order-demo/api/orders/r1')
        self.assertEqual((code, json.loads(body)['status']), (200, 'unknown'))
        self.assertEqual((await request('GET', '/order-demo/api/metrics'))[0], 503)
        self.assertEqual(self.demo.submit_product_order('milk', 'existing'),
                         {'request_id': 'r1', 'status': 'unknown'})
        with self.assertRaises(AdmissionStopped):
            self.demo.submit_product_order('milk', 'new')
        self.demo._world_ready.set()
        code, body = await request('GET', '/order-demo/api/orders/r1')
        self.assertEqual((code, json.loads(body)['status']), (200, 'completed'))


if __name__ == '__main__':
    unittest.main()
