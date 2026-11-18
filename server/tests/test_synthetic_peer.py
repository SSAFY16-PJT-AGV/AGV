"""A local synthetic world must not fabricate or duplicate action results."""
import time
import threading
import unittest
from unittest.mock import Mock
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from server.synthetic_peer import SyntheticPeer
from server.order_demo_app import lifespan
from server.order_demo_actions import DemoActionSession
from server.tests.test_order_fleet_integration import scenario_map
from server.tools.order_demo import run


PRODUCTS = [{'id': 'red', 'product_type': 'Red', 'pickup_node': 'D01',
             'destination_node': 'D04', 'shelf_id': 'SHELF_RED', 'slot_count': 6}]
POSES = {'sim-r1': {'agv_id': 'AGV_01', 'x': 1., 'y': 2., 'yaw': 0.},
         'sim-r2': {'agv_id': 'AGV_02', 'x': 3., 'y': 4., 'yaw': 0.}}


def observation(x=1., y=2.):
    return {'connected': True, 'ready': True, 'received_at': time.monotonic(), 'revision': 1,
            'observation': {'state_received_live': True, 'state': {'map_id': 'twin-validation-v1',
                            'pose': {'x': x * 1000, 'y': y * 1000, 'th': 0.}}}}


class SyntheticPeerTests(unittest.TestCase):
    def setUp(self):
        self.observer = Mock()
        self.observer.twin_observation.side_effect = lambda robot: observation(*(POSES[robot][k] for k in ('x', 'y')))
        self.peer = SyntheticPeer(Mock(), self.observer, PRODUCTS, POSES, 'a' * 32)
        self.peer._refresh_poses()

    def action(self, **changes):
        return dict({'order_id': 'task', 'action_id': 'a1', 'agv_id': 'AGV_01', 'action': 'grab',
                     'product_type': 'Red', 'expected_x': 1., 'expected_y': 2., 'result': 'success'}, **changes)

    def test_one_grab_one_place_and_duplicate_is_not_reapplied(self):
        self.assertTrue(self.peer._apply(self.action())['data']['applied'])
        with self.assertRaises(ValueError):
            self.peer._apply(self.action())
        self.assertEqual(self.peer._stock['D01'], 2)
        self.assertTrue(self.peer._apply({'order_id': 'delivery', 'action_id': 'a2', 'agv_id': 'AGV_01',
                                          'action': 'place', 'shelf_id': 'SHELF_RED', 'slot_index': 0,
                                          'expected_x': 1., 'expected_y': 2., 'result': 'success'})['data']['applied'])
        self.assertTrue(self.peer._slots['SHELF_RED'][0])

    def test_missing_or_stale_pose_never_produces_result(self):
        self.peer._pose_at['AGV_01'] = time.monotonic() - 3
        with self.assertRaises(ValueError):
            self.peer._apply(self.action())
        self.assertEqual(self.peer._stock['D01'], 3)

    def test_wrong_robot_or_location_has_no_effect(self):
        for changes in ({'agv_id': 'AGV_BAD'}, {'expected_x': 20.}):
            with self.assertRaises(ValueError):
                self.peer._apply(self.action(**changes))
        self.assertEqual(self.peer._stock['D01'], 3)

    def test_snapshot_uses_synthetic_world_and_not_claimed_input(self):
        snapshot = self.peer._snapshot('request')
        self.assertEqual(snapshot['data']['world_id'], 'a' * 32)
        self.assertEqual(snapshot['data']['pickups'][0]['available'], 3)
        self.assertEqual(snapshot['data']['shelves'][0]['slots'], [False] * 6)

    def test_failed_response_detaches_and_does_not_repeat_world_mutation(self):
        detached = threading.Event()
        actions = Mock()
        actions.attach.return_value = True
        actions.can_dispatch.return_value = True
        actions.motion_guard.return_value = nullcontext(True)
        actions.poll.side_effect = [{'type': 'robot.action', 'data': self.action()}, None]
        actions.receive.return_value = False
        actions.detach.side_effect = lambda owner: detached.set()
        peer = SyntheticPeer(actions, self.observer, PRODUCTS, POSES, 'a' * 32)
        peer.start()
        self.assertTrue(detached.wait(2))
        peer.stop()
        self.assertEqual(peer._stock['D01'], 2)
        actions.receive.assert_called_once()
        actions.detach.assert_called_once_with(peer)
        self.assertEqual(actions.poll.call_count, 1)

    def test_stop_between_poll_and_effect_does_not_apply_action(self):
        observed, release, invalidated = threading.Event(), threading.Event(), threading.Event()
        actions = Mock()
        actions.attach.return_value = True
        actions.can_dispatch.return_value = True
        actions.motion_guard.return_value = nullcontext(True)
        actions.invalidate_owner.side_effect = lambda owner: invalidated.set()
        def pending(_owner):
            observed.set()
            self.assertTrue(release.wait(2))
            return {'type': 'robot.action', 'data': self.action()}
        actions.poll.side_effect = pending
        peer = SyntheticPeer(actions, self.observer, PRODUCTS, POSES, 'a' * 32).start()
        self.assertTrue(observed.wait(2))
        stopper = threading.Thread(target=peer.stop)
        stopper.start()
        try:
            self.assertTrue(invalidated.wait(2))
        finally:
            release.set()
            stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(peer._stock['D01'], 3)
        actions.receive.assert_not_called()

    def test_connection_lost_between_poll_and_effect_is_not_applied(self):
        observed, release, detached = threading.Event(), threading.Event(), threading.Event()
        actions = Mock()
        actions.attach.return_value = True
        actions.can_dispatch.return_value = True
        actions.motion_guard.return_value = nullcontext(True)
        actions.detach.side_effect = lambda owner: detached.set()
        def pending(_owner):
            observed.set()
            self.assertTrue(release.wait(2))
            return {'type': 'robot.action', 'data': self.action()}
        actions.poll.side_effect = pending
        peer = SyntheticPeer(actions, self.observer, PRODUCTS, POSES, 'a' * 32).start()
        self.assertTrue(observed.wait(2))
        actions.can_dispatch.return_value = False
        release.set()
        self.assertTrue(detached.wait(2))
        peer.stop()
        self.assertEqual(peer._stock['D01'], 3)
        actions.receive.assert_not_called()

    def test_real_owner_invalidation_fences_effect_before_and_during_apply(self):
        for at_effect in (False, True):
            with self.subTest(at_effect=at_effect):
                entered, release, invalidated = threading.Event(), threading.Event(), threading.Event()
                invalidating = threading.Event()
                actions = DemoActionSession(Mock(), threading.RLock(), action_source='synthetic',
                                            on_invalidate=invalidating.set)
                peer = SyntheticPeer(actions, self.observer, PRODUCTS, POSES, 'b' * 32)
                message = {'type': 'robot.action', 'data': self.action()}
                if at_effect:
                    actions.poll = Mock(return_value=message)
                    original = peer._apply
                    def blocking_apply(action):
                        entered.set()
                        self.assertTrue(release.wait(2))
                        return original(action)
                    peer._apply = blocking_apply
                else:
                    def blocking_poll(_owner):
                        entered.set()
                        self.assertTrue(release.wait(2))
                        return message
                    actions.poll = Mock(side_effect=blocking_poll)
                peer.start()
                self.assertTrue(entered.wait(2))
                def invalidate():
                    actions.invalidate_owner(peer)
                    invalidated.set()
                worker = threading.Thread(target=invalidate)
                worker.start()
                try:
                    if at_effect:
                        self.assertTrue(invalidating.wait(2))
                        self.assertFalse(invalidated.is_set())
                        self.assertFalse(actions.available_fast())
                    else:
                        self.assertTrue(invalidated.wait(2))
                finally:
                    release.set()
                    worker.join(2)
                    peer.stop()
                self.assertFalse(worker.is_alive())
                self.assertTrue(invalidated.is_set())
                self.assertEqual(peer._stock['D01'], 2 if at_effect else 3)


class SyntheticConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_incompatible_or_existing_paths_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            map_path = root / 'map.json'
            map_path.write_text(json.dumps(scenario_map(False)), encoding='utf-8')
            existing = root / 'partial'
            existing.mkdir()
            marker = existing / 'preserve.txt'
            marker.write_text('keep', encoding='utf-8')
            base = {'PYTHON_DOTENV_DISABLED': '1', 'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': '1883',
                    'AGV_ORDER_ACTION_SOURCE': 'synthetic', 'AGV_ORDER_DEMO_ENABLED': '1',
                    'AGV_ORDER_COORDINATED': '1', 'AGV_ORDER_FLEET': '1', 'AGV_ORDER_SHELF_SYNC': '1',
                    'AGV_ORDER_DEMO_MAP': str(map_path), 'AGV_ORDER_PERSIST_DIR': str(root / 'new')}
            for change in ({'AGV_ORDER_FIELD_CONFIG': 'field.json'}, {'AGV_ORDER_PERSIST_DIR': ''},
                           {'AGV_ORDER_FLEET': '0'}, {'AGV_ORDER_SHELF_SYNC': '0'},
                           {'AGV_ORDER_COORDINATED': '0'}, {'AGV_ORDER_ACTION_SOURCE': 'other'},
                           {'AGV_ORDER_PERSIST_DIR': str(existing)}):
                with self.subTest(change=change), patch.dict(os.environ, dict(base, **change), clear=True):
                    with self.assertRaises((ValueError, FileExistsError)):
                        async with lifespan(SimpleNamespace(state=SimpleNamespace())):
                            self.fail('invalid configuration must not start')
                self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')
                self.assertEqual(sorted(p.name for p in existing.iterdir()), ['preserve.txt'])
                self.assertFalse((root / 'new').exists())

    def test_launcher_rejects_ue_field_and_existing_path_before_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / 'keep'
            marker.write_text('sentinel', encoding='utf-8')
            for args in ({'editor': 'unreal.exe'}, {'project': 'unreal.uproject'},
                         {'field_config': 'field.json'}, {}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    run('not-needed.json', state_dir=root, synthetic=True, **args)
            self.assertEqual(marker.read_text(encoding='utf-8'), 'sentinel')

