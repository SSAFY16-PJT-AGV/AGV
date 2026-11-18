"""Authenticated Unreal shelf snapshots gate simulated slot allocation."""
import threading
import unittest
import copy

from server.order_demo_actions import DemoActionSession
from server.tests.test_order_demo_actions import Engine


class Inventory:
    shelf_ids = ('SHELF_RED', 'SHELF_BLUE')

    def __init__(self):
        self.ready = False
        self.owner = None
        self.sequence = 0

    def begin_session(self, owner):
        self.owner = owner
        return self.request_snapshot(owner)

    def request_snapshot(self, owner):
        assert owner is self.owner
        self.ready = False
        self.sequence += 1
        return 'request-{}'.format(self.sequence)

    def apply_snapshot(self, owner, data):
        if owner is not self.owner or data.get('request_id') != 'request-{}'.format(self.sequence):
            return False
        self.ready = True
        return True

    def invalidate(self, owner):
        if owner is self.owner:
            self.ready = False
            self.owner = None


class ShelfSyncActionTests(unittest.TestCase):
    def test_nonpersistent_accepts_complete_unreal_snapshot_only_for_current_owner(self):
        from server.order_shelf_inventory import ShelfInventory
        inventory, owner, other = ShelfInventory({'SHELF_RED': 'Red'}), object(), object()
        session = DemoActionSession(Engine(), threading.RLock(), inventory=inventory)
        session.attach(owner, True)
        request = session.poll(owner, 1)
        data = {'request_id': request['data']['request_id'],
                'shelves': [{'shelf_id': 'SHELF_RED', 'product_type': 'Red', 'slots': [False] * 6}],
                'world_id': 'a' * 32,
                'robots': [{'agv_id': name, 'x': 0, 'y': 0, 'yaw': 0,
                            'carrying': False, 'interpolating': False}
                           for name in ('AGV_01', 'AGV_02')],
                'pickups': [{'source_id': source, 'product_type': product, 'available': 2}
                            for source, product in (('D01', 'Red'), ('D06', 'Blue'))]}
        self.assertFalse(session.receive(other, {'type': 'shelf.snapshot', 'data': data}, 2))
        stale = dict(data, request_id='outdated')
        self.assertFalse(session.receive(owner, {'type': 'shelf.snapshot', 'data': stale}, 2))
        malformed = copy.deepcopy(data)
        malformed['robots'][0]['x'] = float('nan')
        with self.assertRaises(ValueError):
            session.receive(owner, {'type': 'shelf.snapshot', 'data': malformed}, 2)
        malformed = dict(data, unexpected=True)
        with self.assertRaises(ValueError):
            session.receive(owner, {'type': 'shelf.snapshot', 'data': malformed}, 2)
        self.assertFalse(inventory.ready)
        self.assertTrue(session.receive(owner, {'type': 'shelf.snapshot', 'data': data}, 2))
        self.assertTrue(inventory.ready)

    def test_unanswered_snapshot_is_retried_without_restarting_its_fence(self):
        inventory, engine, owner = Inventory(), Engine(), object()
        requests = []
        session = DemoActionSession(engine, threading.RLock(), inventory=inventory,
                                    on_snapshot_request=lambda: requests.append('start'))
        self.assertTrue(session.attach(owner, True))
        first = session.poll(owner, 1)
        self.assertEqual(first['type'], 'shelf.snapshot.request')
        self.assertIsNone(session.poll(owner, 1.5))
        self.assertEqual(session.poll(owner, 4), first)
        self.assertEqual(requests, ['start'])
        response = {'type': 'shelf.snapshot', 'data': {'request_id': 'request-1', 'shelves': []}}
        self.assertTrue(session.receive(owner, response, 4))
        self.assertIsNone(session.poll(owner, 4))

    def test_snapshot_is_requested_before_actions_and_only_owner_can_supply_it(self):
        inventory, engine, owner, other = Inventory(), Engine(), object(), object()
        session = DemoActionSession(engine, threading.RLock(), settle_seconds=0, inventory=inventory)
        self.assertTrue(session.attach(owner, True))
        self.assertEqual(session.poll(owner, 1), {'type': 'shelf.snapshot.request',
                         'data': {'request_id': 'request-1', 'shelf_ids': ['SHELF_RED', 'SHELF_BLUE']}})
        response = {'type': 'shelf.snapshot', 'data': {'request_id': 'request-1', 'shelves': []}}
        self.assertFalse(session.receive(other, response, 1))
        self.assertTrue(session.receive(owner, response, 1))
        self.assertEqual(session.poll(owner, 2)['type'], 'robot.action')
        session.detach(owner)
        self.assertFalse(inventory.ready)

    def test_successful_action_requests_fresh_shelf_state(self):
        inventory, engine, owner = Inventory(), Engine(), object()
        session = DemoActionSession(engine, threading.RLock(), settle_seconds=0, inventory=inventory)
        session.attach(owner, True)
        session.poll(owner, 1)
        session.receive(owner, {'type': 'shelf.snapshot', 'data': {'request_id': 'request-1'}}, 1)
        session.poll(owner, 2)
        ack = {'type': 'robot.action.result', 'data': dict(engine.action, applied=True)}
        self.assertTrue(session.receive(owner, ack, 2))
        self.assertFalse(inventory.ready)
        self.assertEqual(session.poll(owner, 3)['data']['request_id'], 'request-2')


if __name__ == '__main__':
    unittest.main()
