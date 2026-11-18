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
        actions.synthetic_effect_guard.side_effect = lambda *_args: nullcontext(True)
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
        actions.synthetic_effect_guard.side_effect = lambda *_args: nullcontext(True)
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
        actions.synthetic_effect_guard.side_effect = lambda *_args: nullcontext(True)
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

    def test_pose_is_refreshed_after_poll_before_world_effect(self):
        delivered = threading.Event()
        actions = Mock()
        actions.attach.return_value = True
        actions.can_dispatch.return_value = True
        actions.synthetic_effect_guard.side_effect = lambda *_args: nullcontext(True)
        fresh = {'value': False}
        def latest(robot):
            value = observation(*(POSES[robot][k] for k in ('x', 'y')))
            if not fresh['value']:
                value['received_at'] -= 2
            return value
        self.observer.twin_observation.side_effect = latest
        def poll(_owner):
            if not fresh['value']:
                # A slow poll has ended and a newer MQTT observation has arrived.
                fresh['value'] = True
                return {'type': 'robot.action', 'data': self.action()}
            return None
        actions.poll.side_effect = poll
        actions.receive.side_effect = lambda *_args: delivered.set() or True
        peer = SyntheticPeer(actions, self.observer, PRODUCTS, POSES, 'a' * 32).start()
        try:
            self.assertTrue(delivered.wait(2))
            self.assertEqual(peer._stock['D01'], 2)
        finally:
            peer.stop()

    def test_refresh_without_new_observation_cannot_fabricate_freshness(self):
        detached = threading.Event()
        actions = Mock()
        actions.attach.return_value = True
        actions.can_dispatch.return_value = True
        actions.synthetic_effect_guard.side_effect = [nullcontext(True), nullcontext(False)]
        actions.poll.return_value = {'type': 'robot.action', 'data': self.action()}
        actions.detach.side_effect = lambda _owner: detached.set()
        old_time = time.monotonic() - 3
        def stale(robot):
            value = observation(*(POSES[robot][k] for k in ('x', 'y')))
            value['received_at'] = old_time
            return value
        self.observer.twin_observation.side_effect = stale
        peer = SyntheticPeer(actions, self.observer, PRODUCTS, POSES, 'a' * 32).start()
        try:
            self.assertTrue(detached.wait(2))
            self.assertEqual(peer._stock['D01'], 3)
            actions.receive.assert_not_called()
        finally:
            peer.stop()

    def test_stale_observation_waits_without_locks_until_new_mqtt_state(self):
        waiting, delivered = threading.Event(), threading.Event()
        actions = Mock()
        actions.attach.return_value = True
        actions.can_dispatch.return_value = True
        actions.synthetic_effect_guard.side_effect = lambda *_args: nullcontext(True)
        actions.poll.side_effect = [{'type': 'robot.action', 'data': self.action()}, None]
        actions.receive.side_effect = lambda *_args: delivered.set() or True
        fresh = {'value': False}
        def latest(robot):
            value = observation(*(POSES[robot][k] for k in ('x', 'y')))
            if not fresh['value']:
                value['received_at'] -= 2
            return value
        self.observer.twin_observation.side_effect = latest
        peer = SyntheticPeer(actions, self.observer, PRODUCTS, POSES, 'a' * 32)
        original_wait = peer._stop.wait
        def observed_wait(timeout=None):
            waiting.set()
            return original_wait(timeout)
        peer._stop.wait = observed_wait
        peer.start()
        try:
            self.assertTrue(waiting.wait(2))
            self.assertFalse(delivered.is_set())
            fresh['value'] = True
            self.assertTrue(delivered.wait(2))
            self.assertEqual(peer._stock['D01'], 2)
        finally:
            peer.stop()

    def test_real_session_effect_guard_checks_owner_pending_and_timeout(self):
        engine = Mock()
        action = self.action()
        engine.snapshot.return_value = {'pending_action': {key: action[key] for key in
                                         ('order_id', 'action_id', 'agv_id', 'action')}}
        session = DemoActionSession(engine, threading.RLock(), action_source='synthetic',
                                    timeout_seconds=10)
        owner = object()
        self.assertTrue(session.attach(owner, True, source='synthetic'))
        session.action_id, session.sent_at = action['action_id'], time.monotonic()
        with session.synthetic_effect_guard(owner, action) as allowed:
            self.assertTrue(allowed)
        with session.synthetic_effect_guard(object(), action) as allowed:
            self.assertFalse(allowed)
        with session.synthetic_effect_guard(owner, dict(action, order_id='different')) as allowed:
            self.assertFalse(allowed)
        session.can_dispatch = lambda: False
        with session.synthetic_effect_guard(owner, action) as allowed:
            self.assertFalse(allowed)
        session.can_dispatch = lambda: True
        session.sent_at = time.monotonic() - 11
        with session.synthetic_effect_guard(owner, action) as allowed:
            self.assertFalse(allowed)
        unreal = DemoActionSession(engine, threading.RLock())
        self.assertTrue(unreal.attach(owner, True))
        unreal.action_id, unreal.sent_at = action['action_id'], time.monotonic()
        with unreal.synthetic_effect_guard(owner, action) as allowed:
            self.assertFalse(allowed)

    def test_timeout_during_observer_read_blocks_the_world_effect(self):
        entered, release = threading.Event(), threading.Event()
        engine = Mock()
        action = self.action()
        engine.snapshot.return_value = {'pending_action': {key: action[key] for key in
                                         ('order_id', 'action_id', 'agv_id', 'action')}}
        session = DemoActionSession(engine, threading.RLock(), action_source='synthetic', timeout_seconds=1)
        session.action_id, session.sent_at = action['action_id'], time.monotonic()
        session.poll = Mock(return_value={'type': 'robot.action', 'data': action})
        peer = SyntheticPeer(session, self.observer, PRODUCTS, POSES, 'a' * 32)
        original, calls = peer._refresh_poses, []
        def slow_observer():
            calls.append(True)
            if len(calls) == 2:
                entered.set()
                self.assertTrue(release.wait(2))
            original()
        peer._refresh_poses = slow_observer
        peer.start()
        try:
            self.assertTrue(entered.wait(2))
            # Test-only controlled deadline expiry while the observer blocks.
            session.sent_at = time.monotonic() - 2
        finally:
            release.set()
            peer._thread.join(2)
            peer.stop()
        self.assertFalse(peer._thread.is_alive())
        self.assertEqual(peer._stock['D01'], 3)

    def test_waiting_for_fresh_mqtt_does_not_hold_session_fences(self):
        waiting, invalidated = threading.Event(), threading.Event()
        engine = Mock()
        action = self.action()
        engine.snapshot.return_value = {'pending_action': {key: action[key] for key in
                                         ('order_id', 'action_id', 'agv_id', 'action')}}
        session = DemoActionSession(engine, threading.RLock(), action_source='synthetic')
        session.action_id, session.sent_at = action['action_id'], time.monotonic()
        session.poll = Mock(return_value={'type': 'robot.action', 'data': action})
        old_time = time.monotonic() - 3
        def stale(robot):
            value = observation(*(POSES[robot][key] for key in ('x', 'y')))
            value['received_at'] = old_time
            return value
        self.observer.twin_observation.side_effect = stale
        peer = SyntheticPeer(session, self.observer, PRODUCTS, POSES, 'a' * 32)
        original_wait = peer._stop.wait
        def observed_wait(timeout=None):
            waiting.set()
            return original_wait(timeout)
        peer._stop.wait = observed_wait
        peer.start()
        worker = None
        try:
            self.assertTrue(waiting.wait(2))
            self.assertTrue(session.lock.acquire(blocking=False))
            session.lock.release()
            def invalidate():
                session.invalidate_owner(peer)
                invalidated.set()
            worker = threading.Thread(target=invalidate, daemon=True)
            worker.start()
            self.assertTrue(invalidated.wait(2))
        finally:
            peer.stop()
            if worker is not None:
                worker.join(2)
        self.assertFalse(peer._thread.is_alive())
        self.assertEqual(peer._stock['D01'], 3)

    def test_real_owner_invalidation_fences_effect_before_and_during_apply(self):
        for at_effect in (False, True):
            with self.subTest(at_effect=at_effect):
                entered, release, invalidated = threading.Event(), threading.Event(), threading.Event()
                invalidating = threading.Event()
                engine = Mock()
                engine.snapshot.return_value = {'pending_action': {
                    key: self.action()[key] for key in ('order_id', 'action_id', 'agv_id', 'action')}}
                actions = DemoActionSession(engine, threading.RLock(), action_source='synthetic',
                                            on_invalidate=invalidating.set)
                actions.action_id, actions.sent_at = 'a1', time.monotonic()
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
            for change in ({'AGV_ORDER_FIELD_CONFIG': 'missing.json'}, {'AGV_ORDER_PERSIST_DIR': ''},
                           {'AGV_ORDER_FLEET': '0'}, {'AGV_ORDER_SHELF_SYNC': '0'},
                           {'AGV_ORDER_COORDINATED': '0'}, {'AGV_ORDER_ACTION_SOURCE': 'other'},
                           {'AGV_ORDER_PERSIST_DIR': str(existing)}):
                with self.subTest(change=change), patch.dict(os.environ, dict(base, **change), clear=True):
                    with self.assertRaises((ValueError, FileExistsError, FileNotFoundError)):
                        async with lifespan(SimpleNamespace(state=SimpleNamespace())):
                            self.fail('invalid configuration must not start')
                self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')
                self.assertEqual(sorted(p.name for p in existing.iterdir()), ['preserve.txt'])
                self.assertFalse((root / 'new').exists())

    def test_launcher_rejects_ue_and_existing_path_before_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / 'keep'
            marker.write_text('sentinel', encoding='utf-8')
            for args in ({'editor': 'unreal.exe'}, {'project': 'unreal.uproject'}, {}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    run('not-needed.json', state_dir=root, synthetic=True, **args)
            self.assertEqual(marker.read_text(encoding='utf-8'), 'sentinel')

    def test_launcher_accepts_explicit_field_option_for_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('server.tools.order_demo.validate_inputs', side_effect=RuntimeError('validate paths')) as check:
                with self.assertRaisesRegex(RuntimeError, 'validate paths'):
                    run('map.json', state_dir=Path(folder) / 'new', synthetic=True,
                        field_config='field-config.json')
                check.assert_called_once()

