"""The local demo API accepts only explicit same-origin simulated orders."""
import json
import unittest

from server.order_demo_app import app, DemoRuntime


class FakeDemo:
    def __init__(self):
        self.orders = []

    def nodes(self):
        return [{'id': 'A', 'type': 'destination'}, {'id': 'B', 'type': 'destination'}]

    def snapshot(self):
        return {'status': 'idle', 'current_node': 'A', 'route': []}

    def create_order(self, pickup, destination):
        if pickup == 'bad':
            raise ValueError('invalid node')
        if self.orders:
            raise RuntimeError('busy')
        self.orders.append((pickup, destination))
        return {'status': 'moving', 'route': ['A', pickup, destination]}

    def create_product_order(self, product_id):
        if product_id != 'red':
            raise ValueError('unknown product')
        return {'status': 'moving', 'product_type': 'Red'}


async def request(method, path, body=None, origin=None, client='127.0.0.1', host='127.0.0.1:8000'):
    headers = [(b'host', host.encode()), (b'content-type', b'application/json')]
    if origin is not None:
        headers.append((b'origin', origin.encode()))
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'root_path': '', 'headers': headers,
             'client': (client, 50000), 'server': ('127.0.0.1', 8000)}
    data = json.dumps(body).encode() if body is not None else b''
    messages = []
    async def receive():
        return {'type': 'http.request', 'body': data, 'more_body': False}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    return (next(m for m in messages if m['type'] == 'http.response.start')['status'],
            b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body'))


class OrderDemoApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.demo = FakeDemo()
        app.state.demo = self.demo

    async def test_page_nodes_and_status(self):
        self.assertEqual((await request('GET', '/order-demo/'))[0], 200)
        self.assertEqual(json.loads((await request('GET', '/order-demo/api/nodes'))[1])['nodes'][0]['id'], 'A')
        self.assertEqual(json.loads((await request('GET', '/order-demo/api/status'))[1])['status'], 'idle')

    async def test_same_origin_order_and_busy_rejection(self):
        body = {'pickup_node': 'A', 'destination_node': 'B'}
        origin = 'http://127.0.0.1:8000'
        self.assertEqual((await request('POST', '/order-demo/api/orders', body, origin))[0], 200)
        self.assertEqual(self.demo.orders, [('A', 'B')])
        self.assertEqual((await request('POST', '/order-demo/api/orders', body, origin))[0], 409)

    async def test_cross_site_and_missing_origin_cannot_create_orders(self):
        body = {'pickup_node': 'A', 'destination_node': 'B'}
        for origin in (None, 'https://attacker.example'):
            self.assertEqual((await request('POST', '/order-demo/api/orders', body, origin))[0], 403)
        self.assertEqual(self.demo.orders, [])

    async def test_remote_and_spoofed_host_rejected(self):
        self.assertEqual((await request('GET', '/order-demo/', client='192.0.2.10'))[0], 403)
        self.assertEqual((await request('GET', '/order-demo/', host='attacker.example'))[0], 403)

    async def test_invalid_order_does_not_start(self):
        body = {'pickup_node': 'bad', 'destination_node': 'B'}
        self.assertEqual((await request('POST', '/order-demo/api/orders', body, 'http://127.0.0.1:8000'))[0], 422)
        self.assertEqual(self.demo.orders, [])

    async def test_product_catalog_and_same_origin_product_order(self):
        code, body = await request('GET', '/order-demo/api/products')
        self.assertEqual(code, 200)
        self.assertIn('red', [p['id'] for p in json.loads(body)['products']])
        code, body = await request('POST', '/order-demo/api/product-orders', {'product_id': 'red'},
                                   'http://127.0.0.1:8000')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['product_type'], 'Red')
        self.assertEqual((await request('POST', '/order-demo/api/product-orders', {'product_id': 'red'}))[0], 403)
        self.assertEqual((await request('POST', '/order-demo/api/product-orders', {'product_id': 'bad'},
                                        'http://127.0.0.1:8000'))[0], 422)


class DemoRuntimeFailureTest(unittest.TestCase):
    def test_fleet_publishes_each_robot_on_its_own_topic(self):
        class Engine:
            def tick(self, elapsed):
                return {'mode': 'fleet', 'robots': [
                    {'robot_id': 'sim-r1', 'status': 'moving', 'current_node': 'N1',
                     'battery': .8, 'pose': {'x': 0, 'y': 0, 'th': 0}},
                    {'robot_id': 'sim-r2', 'status': 'waiting', 'current_node': 'N2',
                     'battery': .8, 'pose': {'x': 1000, 'y': 0, 'th': 0}}]}
        class Publisher:
            def __init__(self, demo):
                self.demo, self.messages = demo, []
            def publish(self, topic, payload, qos, retain):
                self.messages.append((topic, json.loads(payload)))
                self.demo.stop_event.set()
                return type('Result', (), {'rc': 0})()
        demo = DemoRuntime(Engine(), 1883, [])
        publisher = Publisher(demo)
        demo.client = publisher
        demo.connected.set()
        demo._run()
        self.assertEqual([(topic, payload['status']) for topic, payload in publisher.messages],
                         [('agv/sim-r1/state', 'MOVING'), ('agv/sim-r2/state', 'WAITING')])

    def test_worker_failure_disables_further_orders(self):
        class BrokenEngine:
            def tick(self, elapsed):
                raise OSError('synthetic publisher failure')
        demo = DemoRuntime(BrokenEngine(), 1883, [])
        demo.connected.set()
        demo._run()
        self.assertFalse(demo.connected.is_set())
        with self.assertRaises(ConnectionError):
            demo.create_order('A', 'B')

    def test_unready_observer_blocks_order(self):
        class Observer:
            def snapshot(self):
                return {'connected': True, 'ready': False}
        demo = DemoRuntime(FakeDemo(), 1883, [], Observer())
        demo.connected.set()
        with self.assertRaises(ConnectionError):
            demo.create_order('A', 'B')

    def test_unready_coordinated_robot_is_not_ready(self):
        class CoordinatedEngine:
            control_ready = False
        demo = DemoRuntime(CoordinatedEngine(), 1883, [])
        demo.connected.set()
        self.assertFalse(demo._ready())

    def test_mqtt_state_preserves_waiting_and_failure_meaning(self):
        class Engine:
            def __init__(self, status):
                self.status = status
            def tick(self, elapsed):
                return {'status': self.status, 'current_node': 'N1', 'battery': 0.8,
                        'pose': {'x': 0, 'y': 0, 'th': 0}}
        class Publisher:
            def __init__(self, demo):
                self.demo = demo
                self.messages = []
            def publish(self, topic, payload, qos, retain):
                self.messages.append((topic, json.loads(payload), qos, retain))
                self.demo.stop_event.set()
                return type('Result', (), {'rc': 0})()
        for engine_status, expected in (('failed', 'ERROR'), ('unknown', 'ERROR'),
                                        ('waiting', 'WAITING'), ('moving', 'MOVING'),
                                        ('idle', 'IDLE'), ('completed', 'IDLE')):
            demo = DemoRuntime(Engine(engine_status), 1883, [])
            publisher = Publisher(demo)
            demo.client = publisher
            demo.connected.set()
            demo._run()
            with self.subTest(engine_status=engine_status):
                self.assertEqual(len(publisher.messages), 1)
                self.assertEqual(publisher.messages[0][0], 'agv/sim-r1/state')
                self.assertEqual(publisher.messages[0][1]['status'], expected)
