"""Synthetic physical evidence lifecycle; does not enable physical startup."""
import threading
import sqlite3
import unittest
from unittest.mock import patch

from server.order_publisher import OrderPublisher
from server.recovery import RecoveryError
from server.progress_publisher import ProgressPublisher
from server.reproductions import physical_safety_race as reproduction
from server.tests.test_progress_publisher import FakeClient, FakeInfo


class PhysicalFenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        fixture = self.fixture = reproduction.SafetyWindowTests('test_moving_state_must_block_automatic_receiver_progress')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

    def test_normal_next_segment_moving_ack_and_stop_can_progress(self):
        f = self.fixture
        result = f.progress.advance('physical-1', f.event)
        self.assertEqual((result['status'], result['next_seq']), ('advanced', 2))
        client = FakeClient()
        moving = dict(f.state, seq=2, status='MOVING', released_until='N5')
        client.info = FakeInfo(on_wait=lambda: f.watcher._on_message(None, None, reproduction.message('state', moving)))
        result = ProgressPublisher(f.progress, OrderPublisher(client, enabled=True)).publish('T1', 2, 'boot-1')
        self.assertEqual(result['state'], 'acked')
        self.assertEqual(f.progress.get_order('T1', 2)['state'], 'acked')
        self.assertEqual(len(client.publications), 1)
        self.assertNotIn('physical-1', f.watcher._invalidated)
        stopped = dict(f.state, seq=2, status='IDLE', last_node='N5', released_until='N5')
        f.watcher._on_message(None, None, reproduction.message('state', stopped))
        f.watcher._maybe_prove('physical-1', stopped)
        self.assertEqual(f.watcher._proven['physical-1'][0], ('T1', 2))
        final = dict(f.event, event_id='E2', seq=2, stopped_at='N5',
                     cleared_resources=[list(x) for x in sorted(set(f.next.resources) - {('node', 'N5')})])
        self.assertEqual(f.progress.advance('physical-1', final)['status'], 'finished')
        self.assertEqual(f.store.list_occupancy(), {('node', 'N5'): 'physical-1'})

    def test_unsent_next_sequence_moving_latches_robot(self):
        f = self.fixture
        moving = dict(f.state, seq=2, status='MOVING', released_until='N5')
        f.watcher._on_message(None, None, reproduction.message('state', moving))
        self.assertIn('physical-1', f.watcher._invalidated)

    def test_moving_callback_at_enqueue_waits_until_database_commit(self):
        f = self.fixture
        f.progress.advance('physical-1', f.event)
        client = FakeClient()
        moving = dict(f.state, seq=2, status='MOVING', released_until='N5')
        entered, done = threading.Event(), threading.Event()
        threads = []
        def on_publish():
            def callback():
                entered.set()
                f.watcher._on_message(None, None, reproduction.message('state', moving))
                done.set()
            thread = threading.Thread(target=callback)
            threads.append(thread)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(done.wait(0.1))
        client.on_publish = on_publish
        def before_ack():
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
        client.info = FakeInfo(on_wait=before_ack)
        result = ProgressPublisher(f.progress, OrderPublisher(client, enabled=True)).publish('T1', 2, 'boot-1')
        self.assertTrue(done.is_set())
        self.assertEqual(result['state'], 'acked')
        self.assertEqual(f.progress.get_order('T1', 2)['state'], 'acked')
        self.assertNotIn('physical-1', f.watcher._invalidated)

    def set_fake_watcher_clock(self):
        f = self.fixture
        now = [100.0]
        f.watcher._clock = lambda: now[0]
        with f.watcher._lock:
            item = f.watcher._latest['physical-1']
            f.watcher._latest['physical-1'] = (item[0], now[0], item[2], item[3])
            f.watcher._proven['physical-1'] = (('T1', 1), now[0])
        return now

    def test_stop_expiring_before_progress_commit_rolls_back(self):
        f = self.fixture
        now = self.set_fake_watcher_clock()
        before = f.store.list_occupancy()
        original = f.progress._job
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            now[0] += 3.0
            return result
        with patch.object(f.progress, '_job', side_effect=delayed):
            with self.assertRaises(RecoveryError):
                f.progress.advance('physical-1', f.event)
        self.assertEqual(f.store.list_occupancy(), before)
        self.assertIsNone(f.progress.get_order('T1', 2))

    def test_stop_expiring_before_queue_does_not_publish(self):
        f = self.fixture
        now = self.set_fake_watcher_clock()
        f.progress.advance('physical-1', f.event)
        client = FakeClient()
        publisher = OrderPublisher(client, enabled=True)
        original, calls = publisher.validate, []
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                now[0] += 3.0
            return result
        publisher.validate = delayed
        with self.assertRaises(RecoveryError):
            ProgressPublisher(f.progress, publisher).publish('T1', 2, 'boot-1')
        self.assertEqual(len(calls), 2)
        self.assertEqual(client.publications, [])
        self.assertEqual(f.progress.get_order('T1', 2)['state'], 'uncertain')
        self.assertEqual(set(f.store.list_occupancy()), set(f.next.resources))

    def test_next_segment_timeout_latches_and_clears_motion_permit(self):
        f = self.fixture
        f.progress.advance('physical-1', f.event)
        client = FakeClient()
        client.info = FakeInfo(acknowledged=False)
        with self.assertRaises(TimeoutError):
            ProgressPublisher(f.progress, OrderPublisher(client, enabled=True)).publish('T1', 2, 'boot-1')
        self.assertEqual(len(client.publications), 1)
        self.assertEqual(f.progress.get_order('T1', 2)['state'], 'uncertain')
        self.assertIn('physical-1', f.watcher._invalidated)
        self.assertIn('T1', f.gate._blocked)
        self.assertNotIn('physical-1', f.watcher._motion_permits)
        stopped = dict(f.state, status='IDLE', seq=2, last_node='N5', released_until='N5')
        f.watcher._on_message(None, None, reproduction.message('state', stopped))
        f.watcher._maybe_prove('physical-1', stopped)
        self.assertNotIn('physical-1', f.watcher._proven)
        self.assertEqual(set(f.store.list_occupancy()), set(f.next.resources))

    def test_next_segment_ack_database_failure_latches(self):
        f = self.fixture
        f.progress.advance('physical-1', f.event)
        client = FakeClient()
        with patch.object(f.progress, '_mark_physical_broker_acked',
                          side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaises(sqlite3.OperationalError):
                ProgressPublisher(f.progress, OrderPublisher(client, enabled=True)).publish('T1', 2, 'boot-1')
        self.assertEqual(len(client.publications), 1)
        self.assertEqual(f.progress.get_order('T1', 2)['state'], 'uncertain')
        self.assertIn('physical-1', f.watcher._invalidated)
        self.assertIn('T1', f.gate._blocked)
        self.assertNotIn('physical-1', f.watcher._motion_permits)
        self.assertEqual(set(f.store.list_occupancy()), set(f.next.resources))
