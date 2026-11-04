import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server.allocation_store import AllocationConflict
from sim.event_inbox import SimEventInbox
import server.tests.test_segment_progress as fixture_module


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.ProgressTests('test_advance_records_event_and_reserves_next_segment_atomically')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.store.path
        self.inbox = SimEventInbox(self.path, robot_tasks={'sim-r1': ['T1']}, simulation_enabled=True)
        self.inbox.initialize()
        self.event = self.fixture.event
        self.progress = self.fixture.progress

    def test_store_durable_duplicate_and_conflicting_content(self):
        self.assertEqual(self.inbox.store('sim-r1', self.event)['status'], 'stored')
        reopened = SimEventInbox(self.path, robot_tasks={'sim-r1': ['T1']}, simulation_enabled=True)
        reopened.initialize()
        payload = json.dumps(self.event, indent=2).encode()
        self.assertEqual(reopened.store('sim-r1', payload)['status'], 'duplicate')
        with self.assertRaises(AllocationConflict):
            reopened.store('sim-r1', dict(self.event, seq=2))
        self.assertEqual(reopened.get('sim-r1', 'event-1')['state'], 'pending')

    def test_validation_and_binding(self):
        with self.assertRaises(ValueError):
            SimEventInbox(self.path, robot_tasks={'sim-r1': ['T1']})
        bad = [b'{"event_id":"a","event_id":"b"}', b'{"x":NaN}', b'\xff', b' ' * (256*1024+1),
               dict(self.event, order_id='wrong'), dict(self.event, seq=True),
               dict(self.event, cleared_resources=[['node','N1'], ['node','N1']])]
        for payload in bad:
            with self.subTest(payload=str(payload)[:30]), self.assertRaises(ValueError):
                self.inbox.store('sim-r1', payload)
        with self.assertRaises(ValueError):
            self.inbox.store('sim-r2', self.event)

    def test_retry_applies_only_once(self):
        self.inbox.store('sim-r1', self.event)
        result = self.inbox.retry('sim-r1', 'event-1', self.progress)
        self.assertEqual(result['status'], 'advanced')
        self.assertEqual(self.inbox.get('sim-r1', 'event-1')['state'], 'applied')
        with patch.object(self.progress, 'advance', wraps=self.progress.advance) as advance:
            self.assertEqual(self.inbox.retry('sim-r1', 'event-1', self.progress), result)
            advance.assert_not_called()

    def test_failed_proof_keeps_pending(self):
        self.inbox.store('sim-r1', self.event)
        self.fixture.gate.expected = ('T1', 'different', 1, 'N3')
        with self.assertRaises(ValueError):
            self.inbox.retry('sim-r1', 'event-1', self.progress)
        self.assertEqual(self.inbox.get('sim-r1', 'event-1')['state'], 'pending')

    def test_commit_gap_retries_as_progress_duplicate(self):
        self.inbox.store('sim-r1', self.event)
        with patch.object(self.inbox, '_mark_applied', side_effect=sqlite3.OperationalError('crash')):
            with self.assertRaises(sqlite3.OperationalError):
                self.inbox.retry('sim-r1', 'event-1', self.progress)
        self.assertEqual(self.inbox.get('sim-r1', 'event-1')['state'], 'pending')
        reopened = SimEventInbox(self.path, robot_tasks={'sim-r1': ['T1']}, simulation_enabled=True)
        self.assertEqual(reopened.retry('sim-r1', 'event-1', self.progress)['status'], 'duplicate')
        self.assertEqual(reopened.get('sim-r1', 'event-1')['state'], 'applied')

    def test_restart_after_progress_commit_recovers_without_old_proof(self):
        self.inbox.store('sim-r1', self.event)
        with patch.object(self.inbox, '_mark_applied', side_effect=sqlite3.OperationalError('crash')):
            with self.assertRaises(sqlite3.OperationalError):
                self.inbox.retry('sim-r1', 'event-1', self.progress)
        self.fixture.gate.expected = ('T1', 'new-boot', 2, 'N5')
        reopened = SimEventInbox(self.path, robot_tasks={'sim-r1': ['T1']}, simulation_enabled=True)
        with patch.object(self.progress, 'advance', wraps=self.progress.advance) as advance:
            self.assertEqual(reopened.retry('sim-r1', 'event-1', self.progress)['status'], 'duplicate')
            advance.assert_not_called()
        self.assertEqual(reopened.list_pending(), [])

    def test_corrupt_progress_journal_never_marks_applied(self):
        self.inbox.store('sim-r1', self.event)
        with patch.object(self.inbox, '_mark_applied', side_effect=sqlite3.OperationalError('crash')):
            with self.assertRaises(sqlite3.OperationalError):
                self.inbox.retry('sim-r1', 'event-1', self.progress)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE progress_events SET digest='bad'")
        with self.assertRaises(AllocationConflict):
            self.inbox.retry('sim-r1', 'event-1', self.progress)
        self.assertEqual(self.inbox.get('sim-r1', 'event-1')['state'], 'pending')

    def test_missing_or_changed_next_order_does_not_mark_applied(self):
        self.inbox.store('sim-r1', self.event)
        with patch.object(self.inbox, '_mark_applied', side_effect=sqlite3.OperationalError('crash')):
            with self.assertRaises(sqlite3.OperationalError):
                self.inbox.retry('sim-r1', 'event-1', self.progress)
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM progress_orders WHERE seq=2")
        with self.assertRaises(AllocationConflict):
            self.inbox.retry('sim-r1', 'event-1', self.progress)
        self.assertEqual(self.inbox.get('sim-r1', 'event-1')['state'], 'pending')

    def test_recovery_rejects_modified_next_action_or_route(self):
        self.inbox.store('sim-r1', self.event)
        with patch.object(self.inbox, '_mark_applied', side_effect=sqlite3.OperationalError('crash')):
            with self.assertRaises(sqlite3.OperationalError):
                self.inbox.retry('sim-r1', 'event-1', self.progress)
        with sqlite3.connect(self.path) as db:
            original = db.execute("SELECT payload FROM progress_orders WHERE task_id='T1' AND seq=2").fetchone()[0]
        for field in ('action_at_end', 'nodes'):
            altered = json.loads(original)
            altered[field] = {'type': 'unload'} if field == 'action_at_end' else ['N1', 'N2', 'N4', 'N5']
            with self.subTest(field=field):
                with sqlite3.connect(self.path) as db:
                    db.execute("UPDATE progress_orders SET payload=? WHERE task_id='T1' AND seq=2", (json.dumps(altered),))
                with self.assertRaises((ValueError, AllocationConflict)):
                    self.inbox.retry('sim-r1', 'event-1', self.progress)
                self.assertEqual(self.inbox.get('sim-r1', 'event-1')['state'], 'pending')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE progress_orders SET payload=? WHERE task_id='T1' AND seq=2", (original,))
        self.assertEqual(self.inbox.retry('sim-r1', 'event-1', self.progress)['status'], 'duplicate')

    def test_restart_recovers_finished_event_without_releasing_again(self):
        next_order = self.progress.advance('sim-r1', self.event)
        self.progress.mark_attempted('T1', 2, 'boot-1', next_order['payload'], self.fixture.new)
        self.progress.mark_broker_acked('T1', 2, 'boot-1', next_order['payload'], self.fixture.new)
        self.fixture.gate.expected = ('T1', 'boot-1', 2, 'N5')
        final = dict(self.event, event_id='final', seq=2, stopped_at='N5',
                     cleared_resources=[list(item) for item in sorted(self.fixture.new - {('node', 'N5')})])
        self.inbox.store('sim-r1', final)
        with patch.object(self.inbox, '_mark_applied', side_effect=sqlite3.OperationalError('crash')):
            with self.assertRaises(sqlite3.OperationalError):
                self.inbox.retry('sim-r1', 'final', self.progress)
        self.assertEqual(self.fixture.store.list_occupancy(), {('node', 'N5'): 'sim-r1'})
        self.fixture.gate.expected = ('T1', 'new-boot', 2, 'N5')
        reopened = SimEventInbox(self.path, robot_tasks={'sim-r1': ['T1']}, simulation_enabled=True)
        with patch.object(self.progress, 'advance', wraps=self.progress.advance) as advance:
            result = reopened.retry('sim-r1', 'final', self.progress)
            advance.assert_not_called()
        self.assertEqual(result, {'status': 'duplicate', 'task_id': 'T1', 'next_seq': None, 'payload': None})
        self.assertEqual(reopened.get('sim-r1', 'final')['state'], 'applied')
        self.assertEqual(self.fixture.store.list_occupancy(), {('node', 'N5'): 'sim-r1'})

    def test_concurrent_retries_call_progress_once(self):
        self.inbox.store('sim-r1', self.event)
        other = SimEventInbox(self.path, robot_tasks={'sim-r1': ['T1']}, simulation_enabled=True)
        barrier = threading.Barrier(2)
        results, errors = [], []
        def run(inbox):
            try:
                barrier.wait()
                results.append(inbox.retry('sim-r1', 'event-1', self.progress))
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.progress, 'advance', wraps=self.progress.advance) as advance:
            threads = [threading.Thread(target=run, args=(x,)) for x in (self.inbox, other)]
            for t in threads: t.start()
            for t in threads: t.join(10)
            self.assertFalse(errors)
            self.assertEqual(len(results), 2)
            self.assertEqual(advance.call_count, 1)

    def test_database_trigger_failures_leave_durable_state(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER reject_insert BEFORE INSERT ON sim_event_inbox BEGIN SELECT RAISE(ABORT,'reject'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.inbox.store('sim-r1', self.event)
        self.assertEqual(self.inbox.list_pending(), [])
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TRIGGER reject_insert')
        self.inbox.store('sim-r1', self.event)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER reject_apply BEFORE UPDATE ON sim_event_inbox BEGIN SELECT RAISE(ABORT,'reject'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.inbox.retry('sim-r1', 'event-1', self.progress)
        self.assertEqual(self.inbox.list_pending()[0]['event_id'], 'event-1')
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TRIGGER reject_apply')
        self.assertEqual(self.inbox.retry('sim-r1', 'event-1', self.progress)['status'], 'duplicate')
        self.assertEqual(self.inbox.list_pending(), [])

    def test_inconsistent_stored_metadata_fails_closed(self):
        self.inbox.store('sim-r1', self.event)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE sim_event_inbox SET robot_session='tampered'")
        with self.assertRaises(AllocationConflict):
            self.inbox.retry('sim-r1', 'event-1', self.progress)

    def test_database_failure_and_other_database(self):
        self.inbox.store('sim-r1', self.event)
        with patch.object(self.inbox, '_connect', side_effect=sqlite3.OperationalError('db down')):
            with self.assertRaises(sqlite3.OperationalError):
                self.inbox.store('sim-r1', dict(self.event, event_id='e2'))
        self.assertIsNone(self.inbox.get('sim-r1', 'e2'))
        with tempfile.TemporaryDirectory() as tmp:
            from server.allocation_store import AllocationStore
            from server.segment_progress import SegmentProgress
            other = SegmentProgress(AllocationStore(Path(tmp) / 'else.db'), self.fixture.map, simulation_enabled=True, recovery_gate=self.fixture.gate)
            with self.assertRaises(ValueError):
                self.inbox.retry('sim-r1', 'event-1', other)
        self.assertEqual(self.inbox.get('sim-r1', 'event-1')['state'], 'pending')

if __name__ == '__main__':
    unittest.main()
