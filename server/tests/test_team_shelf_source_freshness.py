"""Only a recent correlated Unreal snapshot can support synthetic approval."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from server.order_demo_app import DemoRuntime
from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_synthetic_replenishment import SyntheticReplenishment
from server.team_shelf_web_adapter import TeamShelfSimInventory, TeamShelfActionSession
from server.tests.test_team_shelf_order_slots import snapshot
from server.tests.test_team_shelf_synthetic_replenishment import Fleet


class TeamShelfSourceFreshnessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='source-freshness-')
        self.addCleanup(tmp.cleanup)
        self.now = 100.0
        self.inventory = TeamShelfSimInventory(TeamShelfOrderSlots.create_new(Path(tmp.name) / 'slots.db'),
                                               clock=lambda: self.now)
        self.engine = Fleet(self.inventory)
        products = [{'id': 'milk', 'product_type': 'milk', 'pickup_node': 'D01',
                     'destination_node': 'D02', 'shelf_id': 'N02', 'slot_index': 0}]
        self.demo = DemoRuntime(self.engine, 1883, [], auto_recovery=True, products=products)
        self.demo.team_shelves = True
        self.demo.connected.set()
        self.demo.actions = TeamShelfActionSession(self.engine, self.demo.lock, self.inventory,
                                                  arrival_guard=lambda *_: True)
        self.owner = object()
        self.assertTrue(self.demo.actions.attach(self.owner, True))
        self.respond_to_pending_snapshot()
        self.service = SyntheticReplenishment(self.demo, clock=lambda: self.now, max_age_seconds=5,
                                              refresh_timeout_seconds=0.2)
        self.pickup = patch('server.team_shelf_synthetic_replenishment.pickup_is_clear', return_value=True)
        self.pickup.start()
        self.addCleanup(self.pickup.stop)
        self.addCleanup(self.demo.admission.join, 3)
        self.addCleanup(self.demo.admission.shutdown)

    def respond_to_pending_snapshot(self):
        message = self.demo.actions.poll(self.owner)
        self.assertEqual(message['type'], 'team.shelf.snapshot.request.v1')
        data = {'request_id': message['data']['request_id'], 'world_id': 'a' * 32,
                'robots': [{'agv_id': name, 'x': 0., 'y': 0., 'yaw': 0.,
                            'carrying': False, 'interpolating': False}
                           for name in ('AGV_01', 'AGV_02')], 'slots': snapshot()}
        self.assertTrue(self.demo.actions.receive(self.owner, {'type': 'team.shelf.snapshot.v1', 'data': data}))

    def test_stale_source_is_not_repackaged_as_current_when_peer_does_not_reply(self):
        self.now += 60
        with self.assertRaises((ValueError, ConnectionError)):
            self.service.capture(threshold=1)
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.inventory.store.inspect()['claims'], [])

    def test_stale_source_gets_a_fresh_snapshot_before_reporting(self):
        self.now += 60
        # Keep the asynchronous success case tolerant of a loaded CI host;
        # the separate no-reply test still exercises the short timeout.
        self.service.refresh_timeout = 2.0
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.service.capture, threshold=1)
            deadline = time.monotonic() + 2
            while self.inventory.pending is None and time.monotonic() < deadline:
                time.sleep(.005)
            self.respond_to_pending_snapshot()
            self.assertEqual(result.result(timeout=2)['milk']['status'], 'shortage')

    def test_original_snapshot_expiry_blocks_approval_even_if_capture_clock_is_fresh(self):
        self.service.capture(threshold=1)
        # Independently freeze the synthetic-frame clock to expose source age.
        self.service._perception._clock = lambda: 100.0
        self.now += 6
        with self.assertRaises((ValueError, ConnectionError)):
            self.service.submit('milk', threshold=1, approved=True, request_key='stale-ue')
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.inventory.store.inspect()['claims'], [])

    def test_backwards_clock_never_treats_old_source_as_fresh(self):
        self.now -= 1
        with self.assertRaises((ValueError, ConnectionError)):
            self.service.capture(threshold=1)
        self.assertEqual(self.inventory.store.inspect()['claims'], [])

    def test_a_new_snapshot_invalidates_an_earlier_approval_view(self):
        self.service.capture(threshold=1)
        self.demo.actions._shelf_request_id = self.inventory.request_snapshot(self.owner)
        self.demo.actions._shelf_request_sent = False
        self.respond_to_pending_snapshot()
        with self.assertRaises((ValueError, ConnectionError)):
            self.service.submit('milk', threshold=1, approved=True, request_key='changed-snapshot')
        self.assertEqual(self.inventory.store.inspect()['claims'], [])

    def test_does_not_request_snapshot_while_an_action_is_in_flight(self):
        self.now += 6
        self.demo.actions.sent_at = 100.0
        with self.assertRaises((ValueError, ConnectionError)):
            self.service.capture(threshold=1)
        self.assertIsNone(self.inventory.pending)
        self.assertEqual(self.inventory.store.inspect()['claims'], [])


if __name__ == '__main__':
    unittest.main()
