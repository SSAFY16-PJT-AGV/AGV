"""Initial physical publish uncertainty must not enable automatic recovery."""
import sqlite3
import unittest
from unittest.mock import patch

from server.reproductions import physical_safety_race as reproduction
from server.tests import test_physical_field_handoff_fence as fixture
from server.tests.test_progress_publisher import FakeInfo


class PhysicalInitialUncertaintyTests(unittest.TestCase):
    def make_fixture(self):
        f = fixture.PhysicalFieldHandoffFenceTests('test_source_completed_handoff_target_moving_ack')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.runtime.create_task('source')
        f.runtime.prepare_field('source', 'D_C',
            [{'robot_id': 'physical-1', 'node': 'B', 'incoming_edge': 'E1', 'available': True}],
            {'type': 'none'})
        return f

    def assert_uncertain_and_latched(self, f):
        self.assertEqual(f.store.get_order('source')['state'], 'uncertain')
        before = f.store.list_occupancy()
        self.assertTrue(before)
        self.assertIn('physical-1', f.watcher._invalidated)
        self.assertIn('source', f.gate._blocked)
        self.assertNotIn('physical-1', f.watcher._motion_permits)
        stopped = dict(f.state, status='IDLE', order_id='source', seq=1,
                       last_node='D_C', released_until='D_C')
        f.watcher._on_message(None, None, reproduction.message('state', stopped))
        f.watcher._maybe_prove('physical-1', stopped)
        self.assertNotIn('physical-1', f.watcher._proven)
        self.assertEqual(f.store.list_occupancy(), before)

    def test_initial_puback_timeout_latches_until_reconciliation(self):
        f = self.make_fixture()
        f.publishers[-1].info = FakeInfo(acknowledged=False)
        with self.assertRaises(TimeoutError):
            f.runtime.publish('source')
        self.assertEqual(len(f.publishers[-1].published), 1)
        self.assert_uncertain_and_latched(f)

    def test_initial_ack_database_failure_latches_until_reconciliation(self):
        f = self.make_fixture()
        with patch.object(f.store, 'mark_broker_acked', side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaises(sqlite3.OperationalError):
                f.runtime.publish('source')
        self.assertEqual(len(f.publishers[-1].published), 1)
        self.assert_uncertain_and_latched(f)
