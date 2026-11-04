"""A pre-allocation disconnect can be reset without clearing real occupancy."""
from contextlib import closing
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from server.allocation_store import AllocationStore
from server.recovery import RecoveryError, RecoveryGate
from server.tests import test_fleet_mqtt_integration as fixture
from server.tests.test_segment_recovery_mqtt import two_segment_map


class UnallocatedResetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'allocation.sqlite3'
        self.store = AllocationStore(self.path)
        self.store.initialize()
        self.store.create_task('T1')
        self.gate = RecoveryGate(self.path)
        self.gate.initialize()
        self.gate.invalidate('T1')

    def test_pending_task_with_no_order_or_allocation_can_be_explicitly_reset(self):
        self.assertIn('T1', self.gate._blocked)
        self.gate.reset_unallocated('T1')
        self.assertNotIn('T1', self.gate._blocked)
        self.assertIsNone(self.store.get_allocation('T1'))
        self.assertIsNone(self.store.get_order('T1'))
        self.assertEqual(self.store.list_occupancy(), {})

    def test_unknown_or_allocated_task_is_rejected_and_remains_blocked(self):
        with self.assertRaises(RecoveryError):
            self.gate.reset_unallocated('missing')
        resources = [('node', 'N1'), ('node', 'N2'), ('edge', '["N1","N2"]')]
        self.store.allocate('T1', 'sim-r1', 'layout@v1', ['N1', 'N2'], 'N2',
                            resources, {}, map_fingerprint='a' * 64)
        with self.assertRaises(RecoveryError):
            self.gate.reset_unallocated('T1')
        self.assertIn('T1', self.gate._blocked)
        self.assertEqual(len(self.store.list_occupancy()), 3)

    def test_unallocated_task_with_progress_evidence_is_not_reset(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE sim_event_inbox (task_id TEXT NOT NULL)')
            db.execute("INSERT INTO sim_event_inbox VALUES ('T1')")
            db.commit()
        with self.assertRaises(RecoveryError):
            self.gate.reset_unallocated('T1')
        self.assertIn('T1', self.gate._blocked)

    def test_reset_revokes_old_challenge_and_forgets_local_token(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT INTO recovery_challenges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('T1', self.gate._epoch, 'old-token', 'boot-1', 'sim-r1', 'T1', 1,
                        'layout@v1', 'N2', 'a' * 64, 'b' * 64, 100.0, 'pending', 'c' * 64))
            db.commit()
        self.gate._token_generations['old-token'] = ('T1', self.gate._generations['T1'])
        self.gate.reset_unallocated('T1')
        self.assertNotIn('old-token', self.gate._token_generations)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM recovery_challenges WHERE task_id='T1'").fetchone(),
                             ('revoked',))

    def test_corrupt_pending_status_cannot_hide_persisted_allocation(self):
        resources = [('node', 'N1'), ('node', 'N2'), ('edge', '["N1","N2"]')]
        self.store.allocate('T1', 'sim-r1', 'layout@v1', ['N1', 'N2'], 'N2',
                            resources, {}, map_fingerprint='a' * 64)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE tasks SET status='PENDING' WHERE task_id='T1'")
            db.commit()
        with self.assertRaises(RecoveryError):
            self.gate.reset_unallocated('T1')
        self.assertIn('T1', self.gate._blocked)
        self.assertEqual(len(self.store.list_occupancy()), 3)

    def test_database_failure_leaves_task_blocked(self):
        original = self.gate._connect
        def blocked_connection():
            return sqlite3.connect(self.path, timeout=0.05, isolation_level=None)
        self.gate._connect = blocked_connection
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError):
                self.gate.reset_unallocated('T1')
            db.execute('ROLLBACK')
        self.gate._connect = original
        self.assertIn('T1', self.gate._blocked)

    def test_old_gate_epoch_cannot_reset(self):
        newer = RecoveryGate(self.path)
        newer.initialize()
        with self.assertRaises(RecoveryError):
            self.gate.reset_unallocated('T1')
        self.assertIn('T1', self.gate._blocked)

    def test_reset_inside_an_existing_transaction_is_rejected(self):
        with self.gate.transaction():
            with self.assertRaises(RecoveryError):
                self.gate.reset_unallocated('T1')
        self.assertIn('T1', self.gate._blocked)


@unittest.skipUnless(os.environ.get('MOSQUITTO_EXE'), 'set MOSQUITTO_EXE for isolated broker')
class UnallocatedRuntimeResetTest(unittest.TestCase):
    setUp = fixture.FleetMqttIntegrationTest.setUp
    new_client = fixture.FleetMqttIntegrationTest.new_client
    connect = fixture.FleetMqttIntegrationTest.connect

    def test_explicit_reset_requires_mapped_session_and_no_allocation(self):
        from server.sim_runtime import SimRuntime
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reset-test')
        runtime.start()
        self.addCleanup(runtime.stop)
        runtime.create_task('T1')
        sender = self.new_client()
        self.connect(sender, None)
        sender.publish('agv/sim-r1/conn', b'offline', qos=1).wait_for_publish(timeout=3)
        deadline = time.monotonic() + 3
        while 'T1' not in runtime.gate._blocked and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn('T1', runtime.gate._blocked)
        candidate = [{'robot_id': 'sim-r1', 'node': 'N1', 'available': True}]
        with self.assertRaises(Exception):
            runtime.prepare('T1', 'N5', candidate, {'type': 'load'})
        with self.assertRaises(ValueError):
            runtime.reset_unallocated('T1', 'wrong-session')
        self.assertIn('T1', runtime.gate._blocked)
        self.assertEqual(runtime.reset_unallocated('T1', 'boot-1')['task_id'], 'T1')
        runtime.prepare('T1', 'N5', candidate, {'type': 'load'})
        before = runtime.store.list_occupancy()
        with self.assertRaises(RecoveryError):
            runtime.reset_unallocated('T1', 'boot-1')
        self.assertEqual(runtime.store.list_occupancy(), before)
        self.assertIn('T1', runtime.gate._blocked)


if __name__ == '__main__':
    unittest.main()
