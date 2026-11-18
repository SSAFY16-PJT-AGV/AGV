"""Disconnect during durable pickup callbacks must never start a later leg."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.order_demo_actions import DemoActionSession
from server.order_pickup_inventory import PickupInventory


class Engine:
    def __init__(self):
        self.applied = 0
        self.unknown = 0
        self.action = {'order_id': 'pickup-1', 'action_id': 'action-1',
                       'agv_id': 'AGV_01', 'action': 'grab', 'product_type': 'Red'}

    def snapshot(self):
        return {'pending_action': dict(self.action), 'pose': {'x': 0, 'y': 0}}

    def action_unknown(self):
        self.unknown += 1

    def apply_action_result(self, *args):
        self.applied += 1
        return True


class PickupAckFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PickupInventory.create_new(Path(self.tmp.name) / 'pickup.sqlite3', {'D01': 'Red'})
        self.token = self.store.begin_session('ab' * 16)
        fence = self.store.begin_snapshot(self.token)
        self.store.apply_snapshot(self.token, [{'source_id': 'D01', 'product_type': 'Red', 'available': 1}],
                                  snapshot_fence=fence)
        self.rid = self.store.reserve(self.token, 'request-1', 'D01', 'Red')['reservation_id']
        self.engine, self.client = Engine(), object()

    def prepare(self, action):
        self.store.mark_dispatched(self.token, self.rid, action['order_id'], action['action_id'], action['agv_id'])

    def test_simultaneous_duplicate_ack_consumes_pickup_once(self):
        """Two simultaneous synthetic Unreal replies cannot consume stock twice."""
        entered, release, second_entering = (threading.Event() for _ in range(3))
        results = []

        class ObservedLock:
            def __init__(self):
                self.lock = threading.RLock()

            def __enter__(self):
                if threading.current_thread().name == 'duplicate-ack':
                    second_entering.set()
                self.lock.acquire()
                return self

            def __exit__(self, *args):
                self.lock.release()

        def confirm_slow(action):
            self.store.confirm(self.token, self.rid, action['order_id'], action['action_id'],
                               action['agv_id'], applied=action['applied'])
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test did not release confirmation callback')

        session = DemoActionSession(self.engine, ObservedLock(), settle_seconds=0,
                                    on_prepare_action=self.prepare, on_action_result=confirm_slow)
        self.assertTrue(session.attach(self.client, authenticated=True))
        self.assertIsNotNone(session.poll(self.client, now=0))
        reply = {'type': 'robot.action.result', 'data': dict(self.engine.action, applied=True)}

        def first_receive():
            results.append(session.receive(self.client, reply, now=1))

        def second_receive():
            results.append(session.receive(self.client, reply, now=1))

        first = threading.Thread(target=first_receive)
        second = threading.Thread(target=second_receive, name='duplicate-ack')
        first.start()
        try:
            self.assertTrue(entered.wait(5), 'first ACK must reach the inventory confirmation')
            second.start()
            self.assertTrue(second_entering.wait(5), 'duplicate ACK must arrive while confirmation is pending')
            release.set()
            first.join(5)
            second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
        finally:
            release.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)

        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.engine.applied, 1)
        self.assertEqual(self.engine.unknown, 0)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['reservations'][0]['state'], 'consumed')
        self.assertEqual(snapshot['sources']['D01']['available'], 0)
        self.assertEqual(snapshot['sources']['D01']['reserved'], 0)

    def test_disconnect_during_slow_ack_persistence_never_applies_later_leg(self):
        entered, release, invalidated, invalidation_done = (threading.Event() for _ in range(4))
        result = []

        def confirm(action):
            self.store.confirm(self.token, self.rid, action['order_id'], action['action_id'],
                               action['agv_id'], applied=action['applied'])
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test did not release callback')

        session = DemoActionSession(self.engine, threading.RLock(), settle_seconds=0,
                                    on_prepare_action=self.prepare, on_action_result=confirm,
                                    on_invalidate=invalidated.set)
        self.assertTrue(session.attach(self.client, authenticated=True))
        self.assertIsNotNone(session.poll(self.client, now=0))
        reply = {'type': 'robot.action.result', 'data': dict(self.engine.action, applied=True)}
        receiver = threading.Thread(target=lambda: result.append(session.receive(self.client, reply, now=1)))
        receiver.start()
        self.addCleanup(lambda: (release.set(), receiver.join(5)))
        self.assertTrue(entered.wait(5))

        def invalidate():
            session.invalidate_owner(self.client)
            invalidation_done.set()
        stopper = threading.Thread(target=invalidate)
        stopper.start()
        self.addCleanup(lambda: (release.set(), stopper.join(5)))
        self.assertTrue(invalidated.wait(5))
        self.assertFalse(invalidation_done.is_set(), 'invalidation must wait for the fenced ACK callback')
        release.set()
        receiver.join(5)
        stopper.join(5)
        self.assertFalse(receiver.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(result, [False])
        self.assertEqual(self.engine.applied, 0)
        self.assertGreaterEqual(self.engine.unknown, 1)
        self.assertEqual(self.store.snapshot()['reservations'][0]['state'], 'consumed')
        self.assertEqual(self.store.snapshot()['sources']['D01']['available'], 0)

    def test_disconnect_during_durable_prepare_never_emits_grab(self):
        entered, release, invalidated, invalidation_done = (threading.Event() for _ in range(4))
        result = []

        def prepare_slow(action):
            self.prepare(action)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test did not release callback')

        session = DemoActionSession(self.engine, threading.RLock(), settle_seconds=0,
                                    on_prepare_action=prepare_slow, on_invalidate=invalidated.set,
                                    on_detach=lambda: self.store.end_session(self.token))
        self.assertTrue(session.attach(self.client, authenticated=True))
        poller = threading.Thread(target=lambda: result.append(session.poll(self.client, now=0)))
        poller.start()
        self.addCleanup(lambda: (release.set(), poller.join(5)))
        self.assertTrue(entered.wait(5))
        def invalidate():
            session.invalidate_owner(self.client)
            invalidation_done.set()
        stopper = threading.Thread(target=invalidate)
        stopper.start()
        self.addCleanup(lambda: (release.set(), stopper.join(5)))
        self.assertTrue(invalidated.wait(5))
        self.assertFalse(invalidation_done.is_set())
        release.set()
        poller.join(5)
        stopper.join(5)
        self.assertEqual(result, [None])
        self.assertGreaterEqual(self.engine.unknown, 1)
        session.detach(self.client)
        self.assertEqual(self.store.snapshot()['reservations'][0]['state'], 'unknown')

    def test_ack_expiring_during_slow_db_callback_cannot_start_later_leg(self):
        clock = [1.0]
        def confirm_slow(action):
            self.store.confirm(self.token, self.rid, action['order_id'], action['action_id'],
                               action['agv_id'], applied=action['applied'])
            clock[0] = 12.0
        session = DemoActionSession(self.engine, threading.RLock(), settle_seconds=0, timeout_seconds=10,
                                    on_prepare_action=self.prepare, on_action_result=confirm_slow)
        self.assertTrue(session.attach(self.client, authenticated=True))
        self.assertIsNotNone(session.poll(self.client, now=0))
        reply = {'type': 'robot.action.result', 'data': dict(self.engine.action, applied=True)}
        with patch('server.order_demo_actions.time.monotonic', side_effect=lambda: clock[0]):
            self.assertFalse(session.receive(self.client, reply))
        self.assertEqual(self.engine.applied, 0)
        self.assertGreaterEqual(self.engine.unknown, 1)
        self.assertEqual(self.store.snapshot()['reservations'][0]['state'], 'consumed')


if __name__ == '__main__':
    unittest.main()
