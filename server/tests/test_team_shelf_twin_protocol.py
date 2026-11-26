"""Slot-aware twin protocol must not be mistaken for a legacy grab."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_twin_protocol import TeamShelfTwinSession
from server.tests.test_team_shelf_order_slots import snapshot


class SlotTwinProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='team-twin-protocol-')
        self.addCleanup(self.tmp.cleanup)
        self.store = TeamShelfOrderSlots.create_new(Path(self.tmp.name) / 'slots.sqlite3')
        self.session = self.store.begin_session('a' * 32)
        self.store.apply_snapshot(self.session, snapshot(), fence=self.store.begin_snapshot(self.session))
        self.store.reserve(self.session, 'r1', 'milk')
        self.owner = object()
        self.twin = TeamShelfTwinSession(self.store, self.session, 'a' * 32, self.owner,
                                        lambda request, agv, action, node: True)

    def test_pick_and_place_are_correlated_to_reserved_one_based_slot(self):
        pick = self.twin.begin('r1', 'AGV_01', 'pick')
        self.assertEqual(pick['type'], 'team.shelf.action.v1')
        self.assertEqual((pick['data']['shelf_id'], pick['data']['slot_id'], pick['data']['slot_index']),
                         ('N01', 1, 0))
        self.assertEqual(pick['data']['product_type'], 'milk')
        reply = {'type': 'team.shelf.action.result.v1', 'data': dict(pick['data'], applied=True)}
        self.assertFalse(self.twin.receive(object(), reply))
        altered = {'type': reply['type'], 'data': dict(reply['data'], slot_id=2)}
        self.assertFalse(self.twin.receive(self.owner, altered))
        self.assertEqual(self.store.get('r1')['state'], 'picking')
        self.assertTrue(self.twin.receive(self.owner, reply))
        self.assertFalse(self.twin.receive(self.owner, reply))
        place = self.twin.begin('r1', 'AGV_01', 'place')
        self.assertEqual((place['data']['shelf_id'], place['data']['slot_id'], place['data']['slot_index']),
                         ('N02', 5, 0))
        self.assertTrue(self.twin.receive(self.owner,
            {'type': 'team.shelf.action.result.v1', 'data': dict(place['data'], applied=True)}))
        self.assertEqual(self.store.get('r1')['state'], 'completed')

    def test_unproven_arrival_does_not_emit_or_change_reservation(self):
        twin = TeamShelfTwinSession(self.store, self.session, 'a' * 32, self.owner,
                                    lambda request, agv, action, node: False)
        with self.assertRaises(ValueError):
            twin.begin('r1', 'AGV_01', 'pick')
        self.assertEqual(self.store.get('r1')['state'], 'reserved')

    def test_twin_world_must_match_journal_session(self):
        with self.assertRaisesRegex(ValueError, 'world'):
            TeamShelfTwinSession(self.store, self.session, 'b' * 32, self.owner,
                                 lambda request, agv, action, node: True)

    def test_disconnect_after_dispatch_leaves_both_slots_unknown_and_held(self):
        self.twin.begin('r1', 'AGV_01', 'pick')
        self.twin.disconnect(self.owner)
        self.assertEqual(self.store.get('r1')['state'], 'unknown')
        self.assertEqual({claim['slot_id'] for claim in self.store.inspect()['claims']}, {1, 5})

    def test_expired_ack_cannot_release_a_slot_or_complete_work(self):
        with patch('server.team_shelf_twin_protocol.time.monotonic', return_value=10):
            pick = self.twin.begin('r1', 'AGV_01', 'pick')
        with patch('server.team_shelf_twin_protocol.time.monotonic', return_value=21):
            reply = {'type': 'team.shelf.action.result.v1', 'data': dict(pick['data'], applied=True)}
            self.assertFalse(self.twin.receive(self.owner, reply))
        self.assertEqual(self.store.get('r1')['state'], 'unknown')
        self.assertEqual({claim['slot_id'] for claim in self.store.inspect()['claims']}, {1, 5})

    def test_runtime_ack_failure_preserves_both_claims_as_unknown(self):
        pick = self.twin.begin('r1', 'AGV_01', 'pick')
        reply = {'type': 'team.shelf.action.result.v1', 'data': dict(pick['data'], applied=True)}
        with self.assertRaises(RuntimeError):
            self.twin.receive_with_effect(self.owner, reply, lambda data: False)
        self.assertEqual(self.store.get('r1')['state'], 'unknown')
        self.assertEqual({claim['slot_id'] for claim in self.store.inspect()['claims']}, {1, 5})


if __name__ == '__main__':
    unittest.main()
