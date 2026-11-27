"""Team-slot web observations preserve unknown cells and process scope."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace

from server.order_demo_app import app
from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_web_adapter import TeamShelfSimInventory
from server.tests.test_order_demo_app import request
from server.tests.test_team_shelf_order_slots import snapshot


class TeamShelfObservationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='team-web-observation-')
        self.store = TeamShelfOrderSlots.create_new(Path(self.tmp.name) / 'slots.sqlite3')
        self.inventory = TeamShelfSimInventory(self.store)
        owner = object()
        req = self.inventory.begin_session(owner)
        data = {'request_id': req, 'world_id': 'A' * 32,
                'robots': [{'agv_id': name, 'x': 0., 'y': 0., 'yaw': 0., 'carrying': False,
                            'interpolating': False} for name in ('AGV_01', 'AGV_02')], 'slots': snapshot()}
        self.inventory.apply_snapshot(owner, data)
        self.inventory.reserve('N02', 'milk', request_id='internal-1')
        self.old = getattr(app.state, 'demo', None)
        app.state.demo = SimpleNamespace(journal=None, engine=SimpleNamespace(inventory=self.inventory),
            actions=SimpleNamespace(available_fast=lambda: True), team_shelves=True)

    async def asyncTearDown(self):
        app.state.demo = self.old
        self.tmp.cleanup()

    async def test_six_shelves_and_current_slot_order_are_visible(self):
        code, body = await request('GET', '/order-demo/api/inventory')
        self.assertEqual(code, 200)
        inventory = json.loads(body)
        self.assertEqual(inventory['scope'], 'team_shelf_process_only')
        self.assertEqual(set(inventory['shelves']), {'N01', 'N02', 'N03', 'N04', 'N05', 'N06'})
        self.assertIsNone(inventory['shelves']['N01']['slots'][3])
        self.assertEqual(inventory['shelves']['N02']['claimed'], [0])
        code, body = await request('GET', '/order-demo/api/orders')
        self.assertEqual(code, 200)
        orders = json.loads(body)
        self.assertEqual(orders['scope'], 'team_shelf_process_only')
        self.assertEqual(orders['orders'][0]['request_id'], 'internal-1')
        self.assertEqual(orders['orders'][0]['status'], 'active')
        code, body = await request('GET', '/order-demo/api/orders/internal-1')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['request_id'], 'internal-1')
        self.assertEqual((await request('GET', '/order-demo/api/orders/missing'))[0], 404)

    async def test_blocked_persistent_recovery_does_not_present_slots_as_known(self):
        app.state.demo.journal = object()
        app.state.demo._restart_blocked = True
        code, body = await request('GET', '/order-demo/api/inventory')
        self.assertEqual(code, 200)
        inventory = json.loads(body)
        self.assertFalse(inventory['fresh'])
        self.assertTrue(all(cell is None for row in inventory['shelves'].values() for cell in row['slots']))

    async def test_persistent_inventory_is_unknown_until_same_world_verified(self):
        app.state.demo.journal = object()
        app.state.demo._restart_blocked = False
        app.state.demo._world_ready = threading.Event()
        code, body = await request('GET', '/order-demo/api/inventory')
        self.assertEqual(code, 200)
        inventory = json.loads(body)
        self.assertFalse(inventory['fresh'])
        self.assertTrue(all(cell is None for row in inventory['shelves'].values() for cell in row['slots']))
