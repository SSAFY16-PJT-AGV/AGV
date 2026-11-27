"""Slot-aware adapter keeps claims across lost or failed action results."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_web_adapter import TeamShelfSimInventory, TeamShelfActionSession
from server.tests.test_team_shelf_order_slots import snapshot


class Engine:
    def __init__(self, inventory):
        self.inventory = inventory
        self.pending = None
        self._selected_action = 'sim-r1'
        self._slot_reservations = {}
        self.applied = []
        self.unknown = False

    def snapshot(self):
        return {'pending_action': self.pending, 'current_node': 'D01',
                'pose': {'x': 0, 'y': 0, 'th': 0}}

    def action_unknown(self):
        self.unknown = True

    def apply_action_result(self, *args):
        self.applied.append(args)
        self.pending = None
        return True


class TeamShelfWebAdapterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='team-web-adapter-')
        self.addCleanup(tmp.cleanup)
        self.store = TeamShelfOrderSlots.create_new(Path(tmp.name) / 'slots.sqlite3')
        self.inventory = TeamShelfSimInventory(self.store)
        self.engine = Engine(self.inventory)
        self.owner = object()
        self.actions = TeamShelfActionSession(self.engine, threading.RLock(), self.inventory,
                                              arrival_guard=lambda *args: True, settle_seconds=0)
        self.assertTrue(self.actions.attach(self.owner, True))
        req = self.actions.poll(self.owner, 1)
        self.assertEqual(req['type'], 'team.shelf.snapshot.request.v1')
        data = {'request_id': req['data']['request_id'], 'world_id': 'A' * 32,
                'robots': [{'agv_id': 'AGV_01', 'x': 0., 'y': 0., 'yaw': 0., 'carrying': False,
                            'interpolating': False},
                           {'agv_id': 'AGV_02', 'x': 0., 'y': 0., 'yaw': 0., 'carrying': False,
                            'interpolating': False}], 'slots': snapshot()}
        self.assertTrue(self.actions.receive(self.owner, {'type': 'team.shelf.snapshot.v1', 'data': data}, 1))
        reservation = self.inventory.reserve('N02', 'milk', request_id='web-1')
        self.engine._slot_reservations['sim-r1'] = reservation
        self.engine.pending = {'order_id': 'web-pickup', 'action_id': 'old-action',
                               'agv_id': 'AGV_01', 'action': 'grab', 'product_type': 'milk'}

    def test_correlated_pick_converts_ack_to_existing_runtime_action(self):
        action = self.actions.poll(self.owner, 2)
        self.assertEqual(action['type'], 'team.shelf.action.v1')
        self.assertEqual(action['data']['world_id'], 'A' * 32)
        self.assertEqual((action['data']['request_id'], action['data']['slot_id']), ('web-1', 1))
        altered = {'type': 'team.shelf.action.result.v1', 'data': dict(action['data'], slot_id=2, applied=True)}
        self.assertFalse(self.actions.receive(self.owner, altered, 2.1))
        self.assertEqual(self.engine.applied, [])
        ack = {'type': 'team.shelf.action.result.v1', 'data': dict(action['data'], applied=True)}
        self.assertTrue(self.actions.receive(self.owner, ack, 2.2))
        self.assertEqual(self.engine.applied, [('web-pickup', 'old-action', 'AGV_01', 'grab', True)])
        self.assertEqual(self.store.get('web-1')['state'], 'carried')
        self.assertFalse(self.actions.receive(self.owner, ack, 2.3))

    def test_disconnect_before_ack_keeps_both_claims_unknown(self):
        self.actions.poll(self.owner, 2)
        self.actions.detach(self.owner)
        self.assertTrue(self.engine.unknown)
        self.assertEqual(self.store.get('web-1')['state'], 'unknown')
        self.assertEqual({x['slot_id'] for x in self.store.inspect()['claims']}, {1, 5})

    def test_failed_slot_commit_marks_runtime_unknown_and_holds_claims(self):
        action = self.actions.poll(self.owner, 2)
        ack = {'type': 'team.shelf.action.result.v1', 'data': dict(action['data'], applied=True)}
        with patch.object(self.store, 'complete_action', side_effect=OSError('commit failed')):
            self.assertFalse(self.actions.receive(self.owner, ack, 2.2))
        self.assertTrue(self.engine.unknown)
        self.assertEqual(self.store.get('web-1')['state'], 'unknown')
        self.assertEqual({x['slot_id'] for x in self.store.inspect()['claims']}, {1, 5})

    def test_snapshot_request_failure_after_ack_disables_new_work(self):
        action = self.actions.poll(self.owner, 2)
        ack = {'type': 'team.shelf.action.result.v1', 'data': dict(action['data'], applied=True)}
        with patch.object(self.inventory, 'request_snapshot', side_effect=OSError('snapshot unavailable')):
            self.assertFalse(self.actions.receive(self.owner, ack, 2.2))
        self.assertTrue(self.engine.unknown)
        self.assertFalse(self.actions.available_fast())
        self.assertEqual(self.store.get('web-1')['state'], 'unknown')
        self.assertEqual({x['slot_id'] for x in self.store.inspect()['claims']}, {1, 5})
