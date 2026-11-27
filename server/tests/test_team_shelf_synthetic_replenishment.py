"""Synthetic shortage approval uses the existing team web dispatch."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.order_demo_app import DemoRuntime
from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_web_adapter import TeamShelfSimInventory
from server.team_shelf_synthetic_replenishment import SyntheticReplenishment
from server.tests.test_team_shelf_order_slots import snapshot


class Fleet:
    control_ready = True

    def __init__(self, inventory):
        self.inventory = inventory
        self.calls = []
        self.fail = False

    def snapshot(self):
        return {'mode': 'fleet', 'status': 'idle', 'shelf_inventory_ready': self.inventory.ready,
                'accepting_orders': self.inventory.ready,
                'robots': [{'robot_id': 'sim-r1', 'status': 'idle', 'orders_remaining': 8},
                           {'robot_id': 'sim-r2', 'status': 'idle', 'orders_remaining': 8}]}

    def can_dispatch_destination(self, _node):
        return True

    def create_product_order(self, product, pickup, destination, shelf, slot, *, request_id):
        reservation = self.inventory.reserve(shelf, product, request_id=request_id)
        self.calls.append(reservation)
        if self.fail:
            raise OSError('result lost after claim')
        return {'status': 'moving', 'assigned_robot': 'sim-r1', **reservation}

    def action_unknown(self):
        pass


class SyntheticReplenishmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='team-replenishment-')
        self.addCleanup(self.tmp.cleanup)
        self.store = TeamShelfOrderSlots.create_new(Path(self.tmp.name) / 'slots.sqlite3')
        self.inventory = TeamShelfSimInventory(self.store)
        self.engine = Fleet(self.inventory)
        self.products = [{'id': 'milk', 'product_type': 'milk', 'pickup_node': 'D01',
                          'destination_node': 'D02', 'shelf_id': 'N02', 'slot_index': 0}]
        self.demo = DemoRuntime(self.engine, 1883, [], auto_recovery=True, products=self.products)
        self.demo.map_runtime = SimpleNamespace(map_data={'zones': []})
        self.pickup_gate = patch('server.team_shelf_synthetic_replenishment.pickup_is_clear', return_value=True)
        self.mock_pickup = self.pickup_gate.start()
        self.addCleanup(self.pickup_gate.stop)
        self.demo.team_shelves = True
        self.owner = object()
        self.assertTrue(self.demo.actions.attach(self.owner, True))
        state = {'request_id': self.inventory.pending, 'world_id': 'a' * 32,
                 'robots': [{'agv_id': name, 'x': 0., 'y': 0., 'yaw': 0.,
                             'carrying': False, 'interpolating': False}
                            for name in ('AGV_01', 'AGV_02')], 'slots': snapshot()}
        self.assertTrue(self.inventory.apply_snapshot(self.owner, state))
        self.demo.connected.set()
        self.now = 1.0
        self.service = SyntheticReplenishment(self.demo, clock=lambda: self.now, max_age_seconds=5)
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.demo.stop_event.set()
        self.demo.admission.shutdown()
        self.demo.admission.join(3)

    def test_approval_uses_same_web_request_and_slot_reservation_once(self):
        report = self.service.capture(threshold=1)
        self.assertEqual(report['milk']['status'], 'shortage')
        result = self.service.submit('milk', threshold=1, approved=True, request_key='same')
        repeated = self.service.submit('milk', threshold=1, approved=True, request_key='same')
        self.assertEqual(result, repeated)
        self.assertEqual(len(self.engine.calls), 1)
        admission = self.demo.admission.get_by_key('same')
        self.assertEqual(self.store.inspect()['orders'][0]['request_id'], admission['request_id'])
        self.assertEqual({x['slot_id'] for x in self.store.inspect()['claims']}, {1, 5})

    def test_no_approval_stale_or_unobserved_stock_never_dispatches(self):
        self.service.capture(threshold=1)
        with self.assertRaises(ValueError):
            self.service.submit('milk', threshold=1, approved=False, request_key='not-approved')
        self.now = 7.0
        with self.assertRaises(ValueError):
            self.service.submit('milk', threshold=1, approved=True, request_key='stale')
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.store.inspect()['claims'], [])

    def test_owner_disconnect_invalidates_capture_before_approval(self):
        self.service.capture(threshold=1)
        self.demo.actions.detach(self.owner)
        with self.assertRaises((ConnectionError, ValueError)):
            self.service.submit('milk', threshold=1, approved=True, request_key='disconnected')
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.store.inspect()['claims'], [])

    def test_busy_common_pickup_rejects_before_claiming_slots(self):
        self.service.capture(threshold=1)
        self.mock_pickup.return_value = False
        with self.assertRaises(ValueError):
            self.service.submit('milk', threshold=1, approved=True, request_key='busy')
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.store.inspect()['claims'], [])

    def test_unknown_or_mismatched_unreal_slot_cannot_become_shortage(self):
        for state, product, expected in [('unknown', None, 'unknown'), ('occupied', 'egg', 'mismatch')]:
            req = self.inventory.request_snapshot(self.owner)
            slots = snapshot()
            slots[4].update(state=state, product_type=product)
            world = {'request_id': req, 'world_id': 'a' * 32,
                     'robots': [{'agv_id': name, 'x': 0., 'y': 0., 'yaw': 0.,
                                 'carrying': False, 'interpolating': False}
                                for name in ('AGV_01', 'AGV_02')], 'slots': slots}
            self.inventory.apply_snapshot(self.owner, world)
            self.assertEqual(self.service.capture(threshold=1)['milk']['status'], expected)
            with self.assertRaises(ValueError):
                self.service.submit('milk', threshold=1, approved=True, request_key=expected)
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.store.inspect()['claims'], [])

    def test_dispatch_outcome_unknown_retains_both_claims(self):
        self.service.capture(threshold=1)
        self.engine.fail = True
        result = self.service.submit('milk', threshold=1, approved=True, request_key='failure')
        deadline = time.monotonic() + 3
        while result['status'] == 'pending' and time.monotonic() < deadline:
            time.sleep(.01)
            result = self.demo.admission.get_by_key('failure')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.store.inspect()['orders'][0]['state'], 'unknown')
        self.assertEqual({x['slot_id'] for x in self.store.inspect()['claims']}, {1, 5})


if __name__ == '__main__':
    unittest.main()
