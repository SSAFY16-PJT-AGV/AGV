"""A single authenticated Unreal connection owns each simulated action."""
import threading
import unittest
from unittest.mock import patch

from server.order_demo_actions import DemoActionSession


class Engine:
    def __init__(self):
        self.action = {'order_id': 'o1', 'action_id': 'a1', 'agv_id': 'AGV_01',
                       'action': 'grab', 'product_type': 'Red'}
        self.results = []
        self.unknown = False

    def snapshot(self):
        return {'pending_action': self.action, 'pose': {'x': 1000, 'y': 2000}}

    def apply_action_result(self, *args):
        self.results.append(args)
        self.action = None
        return True

    def action_unknown(self):
        self.unknown = True
        self.action = None


class ActionSessionTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.session = DemoActionSession(self.engine, threading.RLock(), settle_seconds=1, timeout_seconds=3)
        self.owner, self.other = object(), object()

    def test_only_authenticated_single_owner_receives_and_can_ack(self):
        self.assertFalse(self.session.attach(self.owner, False))
        self.assertTrue(self.session.attach(self.owner, True))
        self.assertFalse(self.session.attach(self.other, True))
        self.assertIsNone(self.session.poll(self.owner, 10))
        self.assertIsNone(self.session.poll(self.other, 12))
        self.assertEqual(self.session.poll(self.owner, 12)['data']['action'], 'grab')
        self.assertIsNone(self.session.poll(self.owner, 12.1))
        ack = {'type': 'robot.action.result', 'data': dict(self.engine.action, applied=True)}
        self.assertFalse(self.session.receive(self.other, ack, 12.2))
        wrong = {'type': 'robot.action.result', 'data': dict(self.engine.action, action_id='wrong', applied=True)}
        self.assertFalse(self.session.receive(self.owner, wrong, 12.2))
        self.assertTrue(self.session.receive(self.owner, ack, 12.2))
        self.assertEqual(self.engine.results, [('o1', 'a1', 'AGV_01', 'grab', True)])
        self.assertFalse(self.session.receive(self.owner, ack, 12.2))

    def test_disconnect_or_timeout_never_reissues_sent_action(self):
        self.session.attach(self.owner, True)
        self.session.poll(self.owner, 10)
        self.assertIsNotNone(self.session.poll(self.owner, 11))
        self.session.detach(self.owner)
        self.assertTrue(self.engine.unknown)
        self.assertTrue(self.session.attach(self.other, True))
        self.assertIsNone(self.session.poll(self.other, 12))

        engine = Engine()
        session = DemoActionSession(engine, threading.RLock(), settle_seconds=1, timeout_seconds=3)
        session.attach(self.owner, True)
        session.poll(self.owner, 1)
        session.poll(self.owner, 2)
        self.assertIsNone(session.poll(self.owner, 5.1))
        self.assertTrue(engine.unknown)

    def test_ack_must_be_boolean_and_follow_send(self):
        self.session.attach(self.owner, True)
        ack = {'type': 'robot.action.result', 'data': dict(self.engine.action, applied=True)}
        self.assertFalse(self.session.receive(self.owner, ack, 1))
        self.session.poll(self.owner, 1)
        self.session.poll(self.owner, 2)
        ack['data']['applied'] = 'true'
        self.assertFalse(self.session.receive(self.owner, ack, 2.1))
        self.assertEqual(self.engine.results, [])

    def test_disconnected_mqtt_blocks_action_and_marks_order_unknown(self):
        ready = [True]
        session = DemoActionSession(self.engine, threading.RLock(), settle_seconds=1,
                                    timeout_seconds=3, can_dispatch=lambda: ready[0])
        session.attach(self.owner, True)
        session.poll(self.owner, 1)
        ready[0] = False
        self.assertIsNone(session.poll(self.owner, 2))
        self.assertTrue(self.engine.unknown)

    def test_late_ack_is_not_accepted(self):
        self.session.attach(self.owner, True)
        self.session.poll(self.owner, 1)
        self.session.poll(self.owner, 2)
        ack = {'type': 'robot.action.result', 'data': dict(self.engine.action, applied=True)}
        self.assertFalse(self.session.receive(self.owner, ack, 5.1))
        self.assertTrue(self.engine.unknown)
        self.assertEqual(self.engine.results, [])

    def test_fast_availability_is_nonblocking_and_tracks_owner(self):
        self.assertFalse(self.session.available_fast())
        self.assertTrue(self.session.attach(self.owner, True))
        entered, release = threading.Event(), threading.Event()
        def hold():
            with self.session.lock:
                entered.set()
                release.wait(2)
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertTrue(self.session.available_fast())
        finally:
            release.set()
            thread.join(2)
        self.session.detach(self.owner)
        self.assertFalse(self.session.available_fast())

    def test_action_changes_notify_invalidation_callback(self):
        changes = []
        session = DemoActionSession(self.engine, threading.RLock(), settle_seconds=1,
                                    timeout_seconds=3, on_change=lambda: changes.append('changed'))
        session.attach(self.owner, True)
        self.assertTrue(changes)
        session.poll(self.owner, 1)
        session.poll(self.owner, 2)
        count = len(changes)
        ack = {'type': 'robot.action.result', 'data': dict(self.engine.action, applied=True)}
        self.assertTrue(session.receive(self.owner, ack, 2.1))
        self.assertGreater(len(changes), count)
        session.detach(self.owner)
        self.assertFalse(session.available_fast())

    def test_poll_defaults_to_clock_after_lock_is_acquired(self):
        self.session.attach(self.owner, True)
        entered, release, polled, clock = (threading.Event() for _ in range(4))
        def hold():
            with self.session.lock:
                entered.set()
                release.wait(2)
        def poll():
            self.session.poll(self.owner)
            polled.set()
        def now():
            clock.set()
            return 123.0
        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(entered.wait(1))
        with patch('server.order_demo_actions.time.monotonic', side_effect=now):
            worker = threading.Thread(target=poll)
            worker.start()
            try:
                self.assertFalse(clock.wait(.1), 'clock used before the lock was acquired')
            finally:
                release.set()
                holder.join(2)
                worker.join(2)
        self.assertTrue(polled.is_set())
        self.assertTrue(clock.is_set())
        self.assertEqual(self.session.observed_at, 123.0)

    def test_live_ack_timeout_is_rechecked_after_snapshot_before_applying(self):
        self.session.attach(self.owner, True)
        self.session.poll(self.owner, 1)
        self.session.poll(self.owner, 2)
        ack = {'type': 'robot.action.result', 'data': dict(self.engine.action, applied=True)}
        with patch('server.order_demo_actions.time.monotonic', side_effect=[2.1, 5.1]):
            self.assertFalse(self.session.receive(self.owner, ack))
        self.assertTrue(self.engine.unknown)
        self.assertEqual(self.engine.results, [])

    def test_owner_can_be_invalidated_without_waiting_for_action_lock(self):
        self.assertTrue(self.session.attach(self.owner, True))
        self.session.invalidate_owner(self.other)
        self.assertTrue(self.session.available_fast())
        entered, release = threading.Event(), threading.Event()
        def hold():
            with self.session.lock:
                entered.set()
                release.wait(2)
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            self.session.invalidate_owner(self.owner)
            self.assertFalse(self.session.available_fast())
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(self.session.available())
        self.assertIsNone(self.session.poll(self.owner, 10))
        self.assertTrue(self.engine.unknown)
        self.session.detach(self.owner)
        self.assertTrue(self.session.attach(self.other, True))
        self.session.invalidate_owner(self.owner)
        self.assertTrue(self.session.available_fast(), 'old disconnect must not invalidate new owner')


if __name__ == '__main__':
    unittest.main()
