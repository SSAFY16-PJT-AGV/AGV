"""Stop reports must not revive a proof after an unexpected transition."""
import unittest

from server.order_publisher import OrderPublisher
from server.progress_publisher import ProgressPublisher
from server.recovery import RecoveryError
from server.reproductions import physical_safety_race as reproduction
from server.tests.test_progress_publisher import FakeClient, FakeInfo


class PhysicalStopTransitionTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = reproduction.SafetyWindowTests('test_moving_state_must_block_automatic_receiver_progress')
        f.setUp()
        self.addCleanup(f.doCleanups)

    def required_first(self):
        f = self.fixture
        with f.gate.transaction() as db:
            return f.gate.require_reconciled(
                db, 'T1', 'boot-1', expected_seq=1, expected_stopped_at='N3')

    def test_unexpected_stop_cannot_revive_prior_proof(self):
        f = self.fixture
        self.assertTrue(self.required_first())
        unexpected = dict(f.state, last_node='N4')
        f.watcher._on_message(None, None, reproduction.message('state', unexpected))
        self.assertIn('physical-1', f.watcher._invalidated)
        f.watcher._on_message(None, None, reproduction.message('state', f.state))
        with self.assertRaises(RecoveryError):
            self.required_first()
        self.assertTrue(f.store.list_occupancy())

    def test_valid_next_stop_without_seen_moving_needs_new_proof(self):
        f = self.fixture
        self.assertEqual(f.progress.advance('physical-1', f.event)['next_seq'], 2)
        client = FakeClient()
        self.assertEqual(ProgressPublisher(f.progress, OrderPublisher(client, enabled=True))
                         .publish('T1', 2, 'boot-1')['state'], 'acked')
        next_stop = dict(f.state, status='IDLE', seq=2, last_node='N5', released_until='N5')
        f.watcher._on_message(None, None, reproduction.message('state', next_stop))
        self.assertNotIn('physical-1', f.watcher._invalidated)
        self.assertNotIn('physical-1', f.watcher._proven)
        f.watcher._maybe_prove('physical-1', next_stop)
        self.assertEqual(f.watcher._proven['physical-1'][0], ('T1', 2))
        with f.gate.transaction() as db:
            self.assertTrue(f.gate.require_reconciled(
                db, 'T1', 'boot-1', expected_seq=2, expected_stopped_at='N5'))

    def test_prior_stop_after_approved_next_stop_is_latched(self):
        f = self.fixture
        f.progress.advance('physical-1', f.event)
        client = FakeClient()
        ProgressPublisher(f.progress, OrderPublisher(client, enabled=True)).publish('T1', 2, 'boot-1')
        next_stop = dict(f.state, status='IDLE', seq=2, last_node='N5', released_until='N5')
        f.watcher._on_message(None, None, reproduction.message('state', next_stop))
        self.assertNotIn('physical-1', f.watcher._proven)
        f.watcher._on_message(None, None, reproduction.message('state', f.state))
        self.assertIn('physical-1', f.watcher._invalidated)
        with self.assertRaises(RecoveryError):
            self.required_first()

    def test_same_stop_with_new_telemetry_keeps_current_proof(self):
        f = self.fixture
        self.assertTrue(self.required_first())
        newer = dict(f.state, ts=123, battery=90)
        f.watcher._on_message(None, None, reproduction.message('state', newer))
        self.assertNotIn('physical-1', f.watcher._invalidated)
        self.assertTrue(self.required_first())

    def test_unexpected_order_stop_cannot_restore_original_proof(self):
        f = self.fixture
        self.assertTrue(self.required_first())
        unexpected = dict(f.state, order_id='different')
        f.watcher._on_message(None, None, reproduction.message('state', unexpected))
        self.assertIn('physical-1', f.watcher._invalidated)
        f.watcher._on_message(None, None, reproduction.message('state', f.state))
        with self.assertRaises(RecoveryError):
            self.required_first()
        self.assertTrue(f.store.list_occupancy())

    def test_active_order_disappearing_after_moving_is_latched(self):
        f = self.fixture
        f.progress.advance('physical-1', f.event)
        client = FakeClient()
        moving = dict(f.state, status='MOVING', seq=2, released_until='N5')
        client.info = FakeInfo(
            on_wait=lambda: f.watcher._on_message(None, None, reproduction.message('state', moving)))
        self.assertEqual(ProgressPublisher(f.progress, OrderPublisher(client, enabled=True))
                         .publish('T1', 2, 'boot-1')['state'], 'acked')
        self.assertNotIn('physical-1', f.watcher._proven)
        self.assertIn('physical-1', f.watcher._motion_permits)
        missing_order = dict(moving, status='IDLE', order_id=None, seq=None, released_until=None)
        f.watcher._on_message(None, None, reproduction.message('state', missing_order))
        self.assertIn('physical-1', f.watcher._invalidated)
        stopped = dict(f.state, status='IDLE', seq=2, last_node='N5', released_until='N5')
        f.watcher._on_message(None, None, reproduction.message('state', stopped))
        f.watcher._maybe_prove('physical-1', stopped)
        self.assertNotIn('physical-1', f.watcher._proven)
        with self.assertRaises(RecoveryError):
            with f.gate.transaction() as db:
                f.gate.require_reconciled(db, 'T1', 'boot-1', expected_seq=2, expected_stopped_at='N5')
