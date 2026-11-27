"""A slot commit failure must not leave a completed, reusable robot."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server.order_fleet import FleetProductControlEngine
from server.order_product_control import ProductControlEngine
from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_web_adapter import TeamShelfActionSession, TeamShelfSimInventory
from server.tests.test_team_shelf_order_slots import snapshot


class MinimalProductEngine(ProductControlEngine):
    """Use the real ACK and unknown transitions with only pose stubbed."""
    def __init__(self, twin, pending=None):
        self.lock = threading.RLock()
        self.TWIN = twin
        self._status = 'waiting' if pending else 'idle'
        self._phase = 'waiting_place' if pending else None
        self._pending = copy.deepcopy(pending)
        self._delivery_evidence = {}
        self._delivery_task = pending['order_id'] if pending else None
        self._destination = 'D02'
        self._last_place_ack = self._suspended_state = self._deferred_grab = None

    def snapshot(self):
        return {'status': self._status, 'phase': self._phase, 'orders_remaining': 8,
                'pending_action': copy.deepcopy(self._pending), 'current_node': 'D02',
                'destination_node': 'D02', 'pose': {'x': 0., 'y': 0., 'th': 0.}}

    def _validate_finished(self, *args):
        return {}


class PlaceCommitFailureTest(unittest.TestCase):
    def test_failed_place_commit_is_not_success_or_reusable(self):
        with tempfile.TemporaryDirectory(prefix='team-place-commit-') as folder:
            store = TeamShelfOrderSlots.create_new(Path(folder) / 'slots.sqlite3')
            inventory = TeamShelfSimInventory(store)
            pending = {'order_id': 'web-delivery', 'action_id': 'engine-place',
                       'agv_id': 'AGV_01', 'action': 'place', 'shelf_id': 'N02', 'slot_index': 0}
            robot = MinimalProductEngine('AGV_01', pending)
            other = MinimalProductEngine('AGV_02')
            data = json.loads((Path(__file__).resolve().parents[1] / 'examples/mat_ssafy_v3.json')
                              .read_text(encoding='utf-8'))
            fleet = FleetProductControlEngine({'sim-r1': robot, 'sim-r2': other}, data, inventory=inventory)
            owner = object()
            actions = TeamShelfActionSession(fleet, threading.RLock(), inventory,
                                             arrival_guard=lambda *args: True, settle_seconds=0)
            self.assertTrue(actions.attach(owner, True))
            request = actions.poll(owner, 1)
            world = {'request_id': request['data']['request_id'], 'world_id': 'a' * 32,
                     'robots': [{'agv_id': twin, 'x': 0., 'y': 0., 'yaw': 0.,
                                 'carrying': False, 'interpolating': False}
                                for twin in ('AGV_01', 'AGV_02')], 'slots': snapshot()}
            self.assertTrue(actions.receive(owner, {'type': 'team.shelf.snapshot.v1', 'data': world}, 1))
            reservation = inventory.reserve('N02', 'milk', request_id='web-1')
            fleet._slot_reservations['sim-r1'] = reservation
            store.mark_dispatched(inventory.session, 'web-1', 'pick', 'prior-pick')
            store.complete_action(inventory.session, 'web-1', 'prior-pick', True)
            action = actions.poll(owner, 2)
            self.assertEqual(action['data']['action'], 'place')
            ack = {'type': 'team.shelf.action.result.v1', 'data': dict(action['data'], applied=True)}
            with patch.object(store, 'complete_action', side_effect=OSError('slot commit failed')):
                self.assertFalse(actions.receive(owner, ack, 2.1))
            result = store.inspect()
            self.assertEqual(result['orders'][0]['state'], 'unknown')
            self.assertEqual({claim['slot_id'] for claim in result['claims']}, {1, 5})
            self.assertNotEqual(robot._status, 'completed', 'failed slot commit must not report completion')
            self.assertFalse(FleetProductControlEngine._available(robot.snapshot()),
                             'a robot with uncertain placement must not be reassigned')
            self.assertFalse(fleet.snapshot()['accepting_orders'])
            with self.assertRaisesRegex(RuntimeError, 'requires recovery'):
                fleet.create_product_order('milk', 'D01', 'D02', 'N02', 1, request_id='web-2')


if __name__ == '__main__':
    unittest.main()
