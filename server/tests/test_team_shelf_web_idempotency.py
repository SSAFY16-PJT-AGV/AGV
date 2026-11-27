"""Web admission and real slot claims cooperate under retries and lost replies."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.order_demo_app import DemoRuntime, app
from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_web_adapter import TeamShelfSimInventory
from server.tests.test_order_submission_responsiveness import request_with_key
from server.tests.test_team_shelf_order_slots import snapshot


class ControlledFleet:
    control_ready = True

    def __init__(self, inventory):
        self.inventory = inventory
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def snapshot(self):
        return {'mode': 'fleet', 'status': 'idle', 'shelf_inventory_ready': self.inventory.ready,
                'accepting_orders': self.inventory.ready,
                'robots': [{'robot_id': 'sim-r1', 'status': 'idle', 'orders_remaining': 5},
                           {'robot_id': 'sim-r2', 'status': 'idle', 'orders_remaining': 5}]}

    def can_dispatch_destination(self, destination):
        return True

    def create_product_order(self, product, pickup, destination, shelf, slot, *, request_id=None):
        reservation = self.inventory.reserve(shelf, product, request_id=request_id)
        self.calls.append(reservation)
        self.started.set()
        if not self.release.wait(8):
            raise RuntimeError('test failed to release simulated dispatch')
        return {'status': 'moving', 'assigned_robot': 'sim-r1', **reservation}

    def action_unknown(self):
        pass


class TeamShelfWebIdempotencyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='team-web-idempotency-')
        self.store = TeamShelfOrderSlots.create_new(Path(self.tmp.name) / 'slots.sqlite3')
        self.inventory = TeamShelfSimInventory(self.store)
        self.engine = ControlledFleet(self.inventory)
        products = [{'id': 'milk', 'product_type': 'milk', 'pickup_node': 'D01',
                     'destination_node': 'D02', 'shelf_id': 'N02', 'slot_index': 0},
                    {'id': 'egg', 'product_type': 'egg', 'pickup_node': 'D01',
                     'destination_node': 'D04', 'shelf_id': 'N04', 'slot_index': 0}]
        self.demo = DemoRuntime(self.engine, 1883, [], auto_recovery=True, products=products)
        self.demo.team_shelves = True
        self.demo.map_runtime = SimpleNamespace(map_data={'zones': []})
        gate = patch('server.team_shelf_pickup_gate.pickup_is_clear', return_value=True)
        gate.start()
        self.addCleanup(gate.stop)
        self.owner = object()
        self.assertTrue(self.demo.actions.attach(self.owner, True))
        state = {'request_id': self.inventory.pending, 'world_id': 'a' * 32,
                 'robots': [{'agv_id': name, 'x': 0., 'y': 0., 'yaw': 0.,
                             'carrying': False, 'interpolating': False}
                            for name in ('AGV_01', 'AGV_02')], 'slots': snapshot()}
        self.assertTrue(self.inventory.apply_snapshot(self.owner, state))
        self.demo.connected.set()
        self.old_demo = getattr(app.state, 'demo', None)
        app.state.demo = self.demo

    async def asyncTearDown(self):
        self.engine.release.set()
        self.demo.stop_event.set()
        self.demo.admission.shutdown()
        await asyncio.to_thread(self.demo.admission.join, 5)
        app.state.demo = self.old_demo
        self.tmp.cleanup()

    async def request(self, method, path, body, key):
        code, payload = await request_with_key(method, path, body, 'http://127.0.0.1:8000', key)
        return code, json.loads(payload)

    async def wait_finished(self, key):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            code, value = await self.request('GET', '/order-demo/api/submission-lookup', None, key)
            if code == 200 and value['status'] != 'pending':
                return value
            await asyncio.sleep(.02)
        self.fail('submission did not finish')

    async def test_duplicate_key_and_lost_response_keep_one_slot_claim(self):
        first = asyncio.create_task(self.request('POST', '/order-demo/api/product-orders',
                                                 {'product_id': 'milk'}, 'stable-key'))
        self.assertTrue(await asyncio.to_thread(self.engine.started.wait, 3))
        duplicate = asyncio.create_task(self.request('POST', '/order-demo/api/product-orders',
                                                     {'product_id': 'milk'}, 'stable-key'))
        (code1, result1), (code2, result2) = await asyncio.gather(first, duplicate)
        self.assertEqual((code1, code2), (202, 202))
        self.assertEqual(result1['request_id'], result2['request_id'])
        code, _ = await self.request('POST', '/order-demo/api/product-orders',
                                     {'product_id': 'egg'}, 'stable-key')
        self.assertEqual(code, 409)
        self.assertEqual(len(self.engine.calls), 1)
        # Simulate browser losing both POST responses and looking up its saved key.
        code, recovered = await self.request('GET', '/order-demo/api/submission-lookup', None, 'stable-key')
        self.assertEqual((code, recovered['request_id']), (200, result1['request_id']))
        self.assertEqual(len(self.store.inspect()['orders']), 1)
        self.assertEqual(self.store.inspect()['orders'][0]['request_id'], result1['request_id'])
        self.engine.release.set()
        self.assertEqual((await self.wait_finished('stable-key'))['status'], 'completed')
        self.assertEqual(len(self.store.inspect()['claims']), 2)

    async def test_distinct_parallel_keys_reserve_distinct_slots(self):
        first = asyncio.create_task(self.request('POST', '/order-demo/api/product-orders',
                                                 {'product_id': 'milk'}, 'key-a'))
        self.assertTrue(await asyncio.to_thread(self.engine.started.wait, 3))
        second = asyncio.create_task(self.request('POST', '/order-demo/api/product-orders',
                                                  {'product_id': 'milk'}, 'key-b'))
        (code1, result1), (code2, result2) = await asyncio.gather(first, second)
        self.assertEqual((code1, code2), (202, 202))
        self.assertNotEqual(result1['request_id'], result2['request_id'])
        self.engine.release.set()
        self.assertEqual((await self.wait_finished('key-a'))['status'], 'completed')
        self.assertEqual((await self.wait_finished('key-b'))['status'], 'completed')
        orders = self.store.inspect()['orders']
        self.assertEqual({x['source_slot_id'] for x in orders}, {1, 2})
        self.assertEqual({x['destination_slot_id'] for x in orders}, {5, 6})


if __name__ == '__main__':
    unittest.main()
