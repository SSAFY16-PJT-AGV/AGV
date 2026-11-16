"""HTTP must not wait for a synthetic slow robot operation indefinitely."""
import asyncio
import json
import threading
import unittest

from server.order_demo_app import DemoRuntime, app
from server.tests.test_order_demo_app import request


class SlowEngine:
    control_ready = True

    def __init__(self):
        self.in_tick = threading.Event()
        self.release_tick = threading.Event()
        self.orders = []
        self.order_started = threading.Event()
        self.state = {'status': 'idle', 'current_node': 'N1', 'route': [],
                      'battery': .8, 'pose': {'x': 0, 'y': 0, 'th': 0}}

    def snapshot(self):
        return self.state

    def tick(self, elapsed):
        self.in_tick.set()
        if not self.release_tick.wait(8):
            raise RuntimeError('test did not release slow operation')
        return self.snapshot()

    def create_product_order(self, *args):
        self.orders.append(args)
        self.order_started.set()
        return {'status': 'moving', 'product_type': args[0]}

    def action_unknown(self):
        pass


class Publisher:
    def __init__(self, demo):
        self.demo = demo

    def publish(self, *args, **kwargs):
        return type('Result', (), {'rc': 0})()


class DemoHttpResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.engine = SlowEngine()
        self.demo = DemoRuntime(self.engine, 1883, [])
        self.demo.client = Publisher(self.demo)
        self.demo.connected.set()
        self.demo.actions.attach(object(), True)
        self.old_demo = getattr(app.state, 'demo', None)
        app.state.demo = self.demo
        self.worker = threading.Thread(target=self.demo._run, daemon=True)
        self.worker.start()
        self.assertTrue(self.engine.in_tick.wait(2), 'worker did not begin slow operation')

    def tearDown(self):
        self.engine.release_tick.set()
        self.demo.stop_event.set()
        self.worker.join(3)
        app.state.demo = self.old_demo

    def _request_while_worker_is_blocked(self, method, path, body=None, origin=None):
        result, errors = [], []
        finished = threading.Event()

        def call():
            try:
                result.append(asyncio.run(request(method, path, body, origin)))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        try:
            self.assertTrue(finished.wait(.75), 'HTTP request blocked behind slow simulated robot I/O')
            self.assertFalse(errors, repr(errors))
            return result[0]
        finally:
            self.engine.release_tick.set()
            caller.join(3)

    def test_status_returns_bounded_response_while_tick_waits(self):
        code, body = self._request_while_worker_is_blocked('GET', '/order-demo/api/status')
        self.assertEqual(code, 200)
        state = json.loads(body)
        self.assertIs(state.get('stale'), True)
        self.assertIs(state.get('connected'), False)
        self.assertIs(state.get('accepting_orders'), False)

    def test_product_order_waits_as_pending_for_busy_control_without_duplicate(self):
        result, errors = [], []
        finished = threading.Event()

        def submit():
            try:
                result.append(asyncio.run(request(
                    'POST', '/order-demo/api/product-orders', {'product_id': 'red'},
                    'http://127.0.0.1:8000')))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        caller = threading.Thread(target=submit, daemon=True)
        caller.start()
        try:
            # The engine tick keeps the shared lock past the old 100 ms
            # timeout; admission should return a trackable pending response.
            self.assertTrue(finished.wait(.75), 'HTTP did not return promptly')
            self.assertFalse(errors, repr(errors))
            code, body = result[0]
            self.assertEqual(code, 202)
            accepted = json.loads(body)
            self.assertEqual(accepted.get('status'), 'pending')
            request_id = accepted.get('request_id')
            self.assertIsInstance(request_id, str)
            self.assertTrue(request_id)
            self.assertEqual(self.engine.orders, [], 'order started before the tick released control')
            self.engine.release_tick.set()
            self.assertTrue(self.engine.order_started.wait(3), 'pending order never started')
            self.assertTrue(self.demo.admission.join(3), 'pending order did not finish')
            self.assertEqual(len(self.engine.orders), 1)
            state = self.demo.admission.get(request_id)
            self.assertEqual(state['status'], 'completed')
            self.assertEqual(state['result']['product_type'], 'Red')
        finally:
            self.engine.release_tick.set()
            caller.join(3)

    def test_shutdown_prevents_a_queued_order_from_starting(self):
        result, errors = [], []
        finished = threading.Event()

        def submit():
            try:
                result.append(asyncio.run(request(
                    'POST', '/order-demo/api/product-orders', {'product_id': 'red'},
                    'http://127.0.0.1:8000')))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        caller = threading.Thread(target=submit, daemon=True)
        caller.start()
        try:
            self.assertTrue(finished.wait(.75), 'HTTP did not return promptly')
            self.assertFalse(errors, repr(errors))
            code, body = result[0]
            self.assertEqual(code, 202)
            request_id = json.loads(body)['request_id']
            self.demo.stop_event.set()
            self.demo.admission.shutdown()
            self.assertEqual(self.demo.admission.get(request_id)['status'], 'unknown')
            self.engine.release_tick.set()
            self.assertTrue(self.demo.admission.join(3), 'pending order did not stop')
            self.assertEqual(self.engine.orders, [], 'a queued order started after shutdown')
            self.assertEqual(self.demo.admission.get(request_id)['status'], 'unknown')
        finally:
            self.engine.release_tick.set()
            caller.join(3)


class CachedSnapshotTests(unittest.TestCase):
    def test_disconnection_makes_cached_order_availability_false(self):
        engine = SlowEngine()
        engine.state['accepting_orders'] = True
        demo = DemoRuntime(engine, 1883, [])
        owner = object()
        demo.connected.set()
        self.assertTrue(demo.actions.attach(owner, True))
        # Publish a fresh observed snapshot as the worker would after a tick.
        demo._store_snapshot(engine.snapshot(), True)
        self.assertIs(demo.snapshot().get('accepting_orders'), True)
        demo.connected.clear()
        state = demo.snapshot()
        self.assertIs(state.get('connected'), False)
        self.assertIs(state.get('accepting_orders'), False)
        demo.connected.set()
        demo._store_snapshot(engine.snapshot(), True)
        demo.actions.detach(owner)
        state = demo.snapshot()
        self.assertIs(state.get('unreal_connected'), False)
        self.assertIs(state.get('accepting_orders'), False)

    def test_status_result_is_a_deep_copy(self):
        engine = SlowEngine()
        demo = DemoRuntime(engine, 1883, [])
        demo.connected.set()
        initial = demo.snapshot()
        initial['pose']['x'] = 999
        initial['route'].append('wrong')
        again = demo.snapshot()
        self.assertEqual(again['pose']['x'], 0)
        self.assertEqual(again['route'], [])

    def test_old_cached_snapshot_is_not_ready(self):
        engine = SlowEngine()
        now = [100.0]
        demo = DemoRuntime(engine, 1883, [], snapshot_max_age=1.0, clock=lambda: now[0])
        demo.connected.set()
        now[0] += 2.0
        state = demo.snapshot()
        self.assertIs(state.get('stale'), True)
        self.assertIs(state.get('connected'), False)
        self.assertIs(state.get('accepting_orders'), False)


if __name__ == '__main__':
    unittest.main()
