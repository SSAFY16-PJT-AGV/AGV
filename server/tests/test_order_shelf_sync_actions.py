"""Authenticated Unreal shelf snapshots gate simulated slot allocation."""
import threading
import unittest

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
