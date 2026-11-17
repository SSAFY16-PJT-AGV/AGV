"""A slow simulated MQTT publication cannot strand the web order response."""
import asyncio
import json
import threading
import time
import unittest

from server.order_demo_app import DemoRuntime, app
from server.tests.test_order_demo_app import request


async def request_with_key(method, path, body, origin, key):
    headers = [(b'host', b'127.0.0.1:8000'), (b'content-type', b'application/json'),
               (b'origin', origin.encode()), (b'idempotency-key', key.encode())]
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'root_path': '', 'headers': headers,
             'client': ('127.0.0.1', 50000), 'server': ('127.0.0.1', 8000)}
    data = json.dumps(body).encode()
    messages = []
    async def receive():
        return {'type': 'http.request', 'body': data, 'more_body': False}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    return (next(message for message in messages if message['type'] == 'http.response.start')['status'],
            b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body'))


class SlowSubmissionEngine:
    control_ready = True

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = []
        self.fail_after_mutation = False

    def snapshot(self):
        return {'status': 'idle', 'current_node': 'N1', 'route': [],
                'battery': .8, 'pose': {'x': 0, 'y': 0, 'th': 0}}

    def create_product_order(self, *args):
        self.calls.append(args)
        self.started.set()
        if not self.release.wait(8):
            raise RuntimeError('test did not release the publication')
        self.finished.set()
        if self.fail_after_mutation:
            raise OSError('simulated outcome uncertain after side effect')
        return {'status': 'moving', 'order_id': 'synthetic-order', 'product_type': args[0]}

    def action_unknown(self):
        pass


class OrderSubmissionResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.engine = SlowSubmissionEngine()
        self.demo = DemoRuntime(self.engine, 1883, [])
        self.demo.connected.set()
        self.assertTrue(self.demo.actions.attach(object(), True))
        self.old_demo = getattr(app.state, 'demo', None)
        app.state.demo = self.demo
        self.requests = []

    def tearDown(self):
        self.engine.release.set()
        for thread in self.requests:
            thread.join(3)
        if self.engine.started.is_set():
            self.engine.finished.wait(3)
        app.state.demo = self.old_demo

    def _request_thread(self, method, path, body=None, origin=None, key=None):
        result, errors = [], []
        finished = threading.Event()

        def call():
            try:
                call = request(method, path, body, origin) if key is None else request_with_key(method, path, body, origin, key)
                result.append(asyncio.run(call))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=call, daemon=True)
        self.requests.append(thread)
        thread.start()
        return result, errors, finished

    def _begin_slow_order(self, key=None):
        result, errors, finished = self._request_thread(
            'POST', '/order-demo/api/product-orders', {'product_id': 'red'},
            'http://127.0.0.1:8000', key)
        self.assertTrue(self.engine.started.wait(2),
                        'simulated publication did not begin; response={!r}; errors={!r}'.format(result, errors))
        self.assertTrue(finished.wait(.75), 'HTTP request blocked after publication began')
        self.assertFalse(errors, repr(errors))
        code, body = result[0]
        self.assertEqual(code, 202)
        accepted = json.loads(body)
        self.assertEqual(accepted.get('status'), 'pending')
        self.assertIsInstance(accepted.get('request_id'), str)
        self.assertTrue(accepted['request_id'])
        return accepted['request_id']

    def _submission(self, request_id):
        return asyncio.run(request('GET', '/order-demo/api/submissions/' + request_id))

    def test_slow_accepted_order_is_observable_until_completed(self):
        request_id = self._begin_slow_order()
        code, body = self._submission(request_id)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body).get('status'), 'pending')
        self.assertEqual(len(self.engine.calls), 1)
        self.engine.release.set()
        self.assertTrue(self.engine.finished.wait(2))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            code, body = self._submission(request_id)
            result = json.loads(body)
            if result.get('status') == 'completed':
                break
            time.sleep(.01)
        self.assertEqual(code, 200)
        self.assertEqual(result.get('status'), 'completed')
        self.assertEqual(result.get('result', {}).get('order_id'), 'synthetic-order')
        self.assertEqual(len(self.engine.calls), 1)

    def test_second_order_is_queued_without_side_effects_while_first_is_pending(self):
        request_id = self._begin_slow_order()
        result, errors, finished = self._request_thread(
            'POST', '/order-demo/api/product-orders', {'product_id': 'blue'},
            'http://127.0.0.1:8000')
        self.assertTrue(finished.wait(.75), 'second request should not block')
        self.assertFalse(errors, repr(errors))
        self.assertEqual(result[0][0], 202)
        second = json.loads(result[0][1])
        self.assertEqual(second['status'], 'pending')
        self.assertNotEqual(second['request_id'], request_id)
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(json.loads(self._submission(request_id)[1]).get('status'), 'pending')
        self.engine.release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = json.loads(self._submission(second['request_id'])[1])['status']
            if state == 'completed':
                break
            time.sleep(.01)
        self.assertEqual(state, 'completed')
        self.assertEqual(len(self.engine.calls), 2)
        self.assertEqual([args[0] for args in self.engine.calls], ['Red', 'Blue'])

    def test_same_key_returns_same_pending_id_and_conflicting_payload_is_rejected(self):
        key = 'stable-client-order-1'
        request_id = self._begin_slow_order(key)
        same, errors, finished = self._request_thread(
            'POST', '/order-demo/api/product-orders', {'product_id': 'red'},
            'http://127.0.0.1:8000', key)
        self.assertTrue(finished.wait(.75), 'same-key lookup must not wait on publication')
        self.assertFalse(errors, repr(errors))
        self.assertEqual(same[0][0], 202)
        self.assertEqual(json.loads(same[0][1]).get('request_id'), request_id)
        other, errors, finished = self._request_thread(
            'POST', '/order-demo/api/product-orders', {'product_id': 'blue'},
            'http://127.0.0.1:8000', key)
        self.assertTrue(finished.wait(.75), 'different payload must be rejected without waiting')
        self.assertFalse(errors, repr(errors))
        self.assertEqual(other[0][0], 409)
        self.assertEqual(len(self.engine.calls), 1)

    def test_unknown_submission_id_returns_404(self):
        code, _ = self._submission('does-not-exist')
        self.assertEqual(code, 404)

    def test_shutdown_never_executes_an_accepted_order_waiting_for_control(self):
        locked, release_control = threading.Event(), threading.Event()
        self.engine.release.set()

        def hold_control():
            with self.demo.lock:
                locked.set()
                release_control.wait(5)

        owner = threading.Thread(target=hold_control, daemon=True)
        owner.start()
        self.assertTrue(locked.wait(2))
        stopper = None
        try:
            result, errors, finished = self._request_thread(
                'POST', '/order-demo/api/product-orders', {'product_id': 'red'},
                'http://127.0.0.1:8000', 'shutdown-pending-order')
            self.assertTrue(finished.wait(.75), 'HTTP must not wait for the control lock')
            self.assertFalse(errors, repr(errors))
            self.assertEqual(result[0][0], 202)
            pending = json.loads(result[0][1])
            self.assertEqual(pending['status'], 'pending')
            request_id = pending['request_id']

            stopper = threading.Thread(target=self.demo.stop, daemon=True)
            stopper.start()
            self.assertTrue(self.demo.stop_event.wait(2), 'shutdown was not initiated')
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                _, body = self._submission(request_id)
                state = json.loads(body)['status']
                if state == 'unknown':
                    break
                time.sleep(.01)
            self.assertEqual(state, 'unknown')
            self.assertEqual(self.engine.calls, [])
        finally:
            release_control.set()
            owner.join(3)
            if stopper is not None:
                stopper.join(3)
        self.assertFalse(stopper.is_alive(), 'shutdown did not finish after control release')
        self.assertEqual(self.engine.calls, [], 'shutdown must prevent the queued order from starting')
        _, body = self._submission(request_id)
        self.assertEqual(json.loads(body)['status'], 'unknown')

    def test_exception_after_engine_side_effect_remains_unknown_and_is_never_replayed(self):
        key = 'uncertain-client-order-1'
        self.engine.fail_after_mutation = True
        self.engine.release.set()
        result, errors, finished = self._request_thread(
            'POST', '/order-demo/api/product-orders', {'product_id': 'red'},
            'http://127.0.0.1:8000', key)
        self.assertTrue(finished.wait(2))
        self.assertFalse(errors, repr(errors))
        self.assertEqual(result[0][0], 202)
        response = json.loads(result[0][1])
        self.assertEqual(response.get('status'), 'unknown')
        request_id = response.get('request_id')
        self.assertIsInstance(request_id, str)
        code, body = self._submission(request_id)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body).get('status'), 'unknown')
        repeated, errors, finished = self._request_thread(
            'POST', '/order-demo/api/product-orders', {'product_id': 'red'},
            'http://127.0.0.1:8000', key)
        self.assertTrue(finished.wait(2))
        self.assertFalse(errors, repr(errors))
        self.assertEqual(repeated[0][0], 202)
        again = json.loads(repeated[0][1])
        self.assertEqual((again.get('status'), again.get('request_id')), ('unknown', request_id))
        self.assertEqual(len(self.engine.calls), 1)


if __name__ == '__main__':
    unittest.main()
