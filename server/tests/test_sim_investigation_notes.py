"""Investigation annotations are simulation-only records, never control actions."""
from contextlib import closing
import sqlite3
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.allocation_store import AllocationStore
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress
from server.tests.test_segment_recovery_mqtt import two_segment_map


class InvestigationNotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sim-investigation-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'simulation.sqlite3'
        self.store = AllocationStore(self.path)
        self.store.initialize()
        self.store.create_task('T1')
        self.store.create_task('T2')
        nodes = ['N1', 'N2']
        self.resources = [('node', 'N1'), ('node', 'N2'), ('edge', '["N1","N2"]')]
        self.allocation = self.store.allocate('T1', 'sim-r1', 'integration@v1', nodes, 'N2',
                                              self.resources, {'type': 'load'}, map_fingerprint='a' * 64)
        order = self.store.get_order('T1')
        self.store.mark_attempted('T1', order['payload'], self.allocation, self.resources)
        self.gate = RecoveryGate(self.path)
        self.gate.initialize()
        self.gate.invalidate('T1')
        self.progress = SegmentProgress(self.store, two_segment_map(), simulation_enabled=True,
                                        recovery_gate=self.gate)
        self.progress.initialize()

    def notes(self, progress=None):
        from server.sim_investigation_notes import SimInvestigationNotes
        notes = SimInvestigationNotes(progress or self.progress,
                                      {'sim-r1': ['T1'], 'sim-r2': ['T2']}, simulation=True)
        notes.initialize()
        return notes

    def test_record_captures_durable_task_order_and_occupancy_without_control(self):
        notes = self.notes()
        before = self.store.list_occupancy()
        order = self.store.get_order('T1')
        self.assertIn('T1', self.gate._blocked)
        note = notes.record('T1', '1' * 32, 'connection')
        self.assertEqual((note['task_id'], note['request_id'], note['category']), ('T1', '1' * 32, 'connection'))
        self.assertEqual(note['snapshot']['status'], 'ASSIGNED')
        self.assertEqual(note['snapshot']['orders'], [{'seq': 1, 'state': 'uncertain'}])
        self.assertEqual(note['snapshot']['occupancy'], [['edge', '["N1","N2"]'], ['node', 'N1'], ['node', 'N2']])
        self.assertEqual(note['snapshot']['integrity'], 'not_checked')
        self.assertTrue(note['created_utc'].endswith('Z'))
        self.assertEqual(notes.list('T1')['notes'], [note])
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertEqual(self.store.get_order('T1'), order)
        self.assertIn('T1', self.gate._blocked)

    def test_identical_request_is_idempotent_and_content_or_task_conflict_fails(self):
        notes = self.notes()
        first = notes.record('T1', 'a' * 32, 'position')
        self.assertEqual(notes.record('T1', 'a' * 32, 'position'), first)
        for task, category in (('T1', 'action'), ('T2', 'position')):
            with self.subTest(task=task), self.assertRaises(ValueError):
                notes.record(task, 'a' * 32, category)
        self.assertEqual(len(notes.list('T1')['notes']), 1)

    def test_request_and_category_are_strict_and_other_data_is_not_accepted(self):
        notes = self.notes()
        for request_id in ('', 'a' * 31, 'A' * 32, 'z' * 32, 'a' * 33, None):
            with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                notes.record('T1', request_id, 'other')
        for category in ('operator-secret', '', None, 'other\n', []):
            with self.subTest(category=category), self.assertRaises(ValueError):
                notes.record('T1', 'b' * 32, category)
        with self.assertRaises(TypeError):
            notes.record('T1', 'b' * 32, 'other', text='private')
        with self.assertRaises(ValueError):
            notes.record('unmapped', 'b' * 32, 'other')
        self.assertEqual(notes.list('T1')['notes'], [])

    def test_new_epoch_can_read_prior_notes_but_old_gate_cannot_write(self):
        notes = self.notes()
        first = notes.record('T1', 'c' * 32, 'connection')
        new_gate = RecoveryGate(self.path)
        new_gate.initialize()
        new_progress = SegmentProgress(self.store, two_segment_map(), simulation_enabled=True,
                                       recovery_gate=new_gate)
        fresh = self.notes(new_progress)
        self.assertEqual(fresh.list('T1')['notes'], [first])
        with self.assertRaises(ValueError):
            notes.record('T1', 'd' * 32, 'action')
        self.assertEqual(len(fresh.list('T1')['notes']), 1)

    def test_concurrent_identical_request_commits_once(self):
        notes = self.notes()
        barrier = threading.Barrier(2)
        results, errors = [], []
        def record():
            try:
                barrier.wait(timeout=5)
                results.append(notes.record('T1', 'e' * 32, 'position'))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=record) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(notes.list('T1')['notes']), 1)

    def test_failed_insert_preserves_order_occupancy_and_existing_records(self):
        notes = self.notes()
        first = notes.record('T1', 'f' * 32, 'action')
        before, order = self.store.list_occupancy(), self.store.get_order('T1')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_note BEFORE INSERT ON sim_investigation_notes "
                       "BEGIN SELECT RAISE(ABORT, 'injected-secret'); END")
        with self.assertRaises(ValueError) as error:
            notes.record('T1', '0' * 32, 'other')
        self.assertNotIn('injected-secret', str(error.exception))
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertEqual(self.store.get_order('T1'), order)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sim_investigation_notes').fetchone(), (1,))
        self.assertEqual(first['category'], 'action')

    def test_nested_gate_transaction_is_rejected_without_waiting(self):
        notes = self.notes()
        with self.gate.transaction():
            with self.assertRaises(ValueError):
                notes.record('T1', '9' * 32, 'other')
        with self.gate.serialized():
            with self.assertRaises(ValueError):
                notes.list('T1')

    def test_epoch_change_while_write_waits_rejects_stale_record(self):
        notes = self.notes()
        entered = threading.Event()
        errors = []
        original_connect = sqlite3.connect
        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE':
                    entered.set()
                return super().execute(sql, *args)
        def connect(*args, **kwargs):
            if threading.current_thread().name == 'notes-writer':
                kwargs['factory'] = ObservedConnection
            return original_connect(*args, **kwargs)
        def record():
            try:
                notes.record('T1', '8' * 32, 'other')
            except BaseException as exc:
                errors.append(exc)
        with closing(original_connect(self.path, isolation_level=None)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE recovery_runtime SET epoch=? WHERE singleton=1', ('new-epoch',))
            with patch('server.sim_investigation_notes.sqlite3.connect', side_effect=connect):
                writer = threading.Thread(target=record, name='notes-writer')
                writer.start()
                try:
                    self.assertTrue(entered.wait(3))
                finally:
                    db.execute('COMMIT')
                    writer.join(10)
        self.assertFalse(writer.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        with closing(original_connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sim_investigation_notes').fetchone()[0], 0)

    def test_waiting_writer_does_not_delay_other_task_local_invalidation(self):
        notes = self.notes()
        waiting, blocked = threading.Event(), threading.Event()
        errors = []
        original_connect = sqlite3.connect
        original_block = self.gate._block_many
        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE':
                    waiting.set()
                return super().execute(sql, *args)
        def connect(*args, **kwargs):
            if threading.current_thread().name == 'notes-writer':
                kwargs['factory'] = ObservedConnection
            return original_connect(*args, **kwargs)
        def block(tasks):
            result = original_block(tasks)
            blocked.set()
            return result
        def record():
            try:
                notes.record('T1', '7' * 32, 'connection')
            except BaseException as exc:
                errors.append(exc)
        def invalidate():
            try:
                self.gate.invalidate('T2')
            except BaseException as exc:
                errors.append(exc)
        with closing(original_connect(self.path, isolation_level=None)) as db:
            db.execute('BEGIN IMMEDIATE')
            with patch('server.sim_investigation_notes.sqlite3.connect', side_effect=connect), \
                 patch.object(self.gate, '_block_many', side_effect=block):
                writer = threading.Thread(target=record, name='notes-writer')
                invalidator = threading.Thread(target=invalidate)
                writer.start()
                try:
                    self.assertTrue(waiting.wait(3))
                    invalidator.start()
                    self.assertTrue(blocked.wait(1), 'local invalidation waited for notes database I/O')
                    self.assertIn('T2', self.gate._blocked)
                finally:
                    db.execute('ROLLBACK')
                    for thread in (writer, invalidator):
                        if thread.ident is not None:
                            thread.join(10)
        self.assertFalse(writer.is_alive() or invalidator.is_alive())
        self.assertEqual(errors, [])

    def test_failure_after_insert_rolls_back_transaction(self):
        from server.sim_investigation_notes import InvestigationConflict
        notes = self.notes()
        before, order = self.store.list_occupancy(), self.store.get_order('T1')
        with patch.object(notes, '_note', side_effect=InvestigationConflict('injected failure')):
            with self.assertRaises(InvestigationConflict):
                notes.record('T1', '8' * 32, 'action')
        self.assertEqual(notes.list('T1')['notes'], [])
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertEqual(self.store.get_order('T1'), order)

    def test_pagination_is_bounded_and_never_returns_other_task(self):
        notes = self.notes()
        one = notes.record('T1', '1' * 32, 'other')
        notes.record('T2', '2' * 32, 'position')
        two = notes.record('T1', '3' * 32, 'connection')
        first = notes.list('T1', limit=1)
        self.assertEqual(first, {'notes': [one], 'next_after': one['note_id']})
        self.assertEqual(notes.list('T1', after_id=first['next_after'], limit=1),
                         {'notes': [two], 'next_after': None})
        for kwargs in ({'limit': 0}, {'limit': 21}, {'limit': 101}, {'limit': True},
                       {'after_id': -1}, {'after_id': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                notes.list('T1', **kwargs)

    def test_corrupt_snapshot_never_returns_extra_user_data(self):
        notes = self.notes()
        notes.record('T1', '7' * 32, 'other')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE sim_investigation_notes SET snapshot_json=?',
                       ('{"integrity":"not_checked","secret":"do-not-return"}',))
        with self.assertRaises(ValueError) as error:
            notes.list('T1')
        self.assertNotIn('do-not-return', str(error.exception))

    def test_historical_reference_cannot_be_read_under_changed_robot_mapping(self):
        from server.sim_investigation_notes import SimInvestigationNotes
        notes = self.notes()
        notes.record('T1', '7' * 32, 'other')
        notes.record('T2', '8' * 32, 'position')
        changed = SimInvestigationNotes(self.progress, {'sim-r2': ['T1'], 'sim-r1': ['T2']}, simulation=True)
        changed.initialize()
        with self.assertRaises(ValueError):
            changed.list('T1')
        with self.assertRaises(ValueError):
            changed.record('T1', '7' * 32, 'other')
        with self.assertRaises(ValueError):
            changed.list('T2')

    def test_current_allocation_robot_must_match_recorded_mapping_on_read_and_retry(self):
        notes = self.notes()
        notes.record('T1', '9' * 32, 'connection')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE allocation_jobs SET robot_id='sim-r2' WHERE task_id='T1'")
        with self.assertRaises(ValueError):
            notes.list('T1')
        with self.assertRaises(ValueError):
            notes.record('T1', '9' * 32, 'connection')

    def test_assigned_task_missing_allocation_and_outbox_cannot_be_read_or_retried(self):
        notes = self.notes()
        notes.record('T1', '9' * 32, 'connection')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute("DELETE FROM allocation_occupancy WHERE task_id='T1'")
            db.execute("DELETE FROM allocation_outbox WHERE task_id='T1'")
            db.execute("DELETE FROM allocation_jobs WHERE task_id='T1'")
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone(), ('ASSIGNED',))
        with self.assertRaises(ValueError):
            notes.list('T1')
        with self.assertRaises(ValueError):
            notes.record('T1', '9' * 32, 'connection')
        # A configured but unallocated PENDING task remains a valid reference target.
        self.assertEqual(notes.record('T2', '8' * 32, 'position')['snapshot']['status'], 'PENDING')

    def test_allocation_without_initial_outbox_is_rejected(self):
        notes = self.notes()
        notes.record('T1', '9' * 32, 'connection')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM allocation_outbox WHERE task_id='T1'")
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        with self.assertRaises(ValueError):
            notes.list('T1')
        with self.assertRaises(ValueError):
            notes.record('T1', '9' * 32, 'connection')

    def test_orphaned_note_cannot_be_read_or_retried(self):
        notes = self.notes()
        notes.record('T1', '9' * 32, 'connection')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute("DELETE FROM tasks WHERE task_id='T1'")
        with self.assertRaises(ValueError):
            notes.list('T1')
        with self.assertRaises(ValueError):
            notes.record('T1', '9' * 32, 'connection')

    def test_corrupt_utc_text_is_never_returned(self):
        notes = self.notes()
        notes.record('T1', '9' * 32, 'connection')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE sim_investigation_notes SET created_utc='private-secretZ'")
        for operation in (lambda: notes.list('T1'), lambda: notes.record('T1', '9' * 32, 'connection')):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError) as error:
                    operation()
                self.assertNotIn('private-secret', str(error.exception))

    def test_task_capacity_rejects_new_reference_but_allows_retry(self):
        notes = self.notes()
        with patch('server.sim_investigation_notes._MAX_TASK_NOTES', 1):
            first = notes.record('T1', '6' * 32, 'other')
            self.assertEqual(notes.record('T1', '6' * 32, 'other'), first)
            with self.assertRaises(ValueError):
                notes.record('T1', '5' * 32, 'other')
        self.assertEqual(len(notes.list('T1')['notes']), 1)

    def test_same_transaction_keeps_snapshot_columns_consistent(self):
        notes = self.notes()
        first_read, attempting, committed = threading.Event(), threading.Event(), threading.Event()
        errors, snapshots, blocked_during_read = [], [], []
        real_connect = sqlite3.connect

        class ObservedCursor:
            def __init__(self, cursor):
                self.cursor = cursor
            def fetchone(self):
                row = self.cursor.fetchone()
                self.cursor.close()
                first_read.set()
                if not attempting.wait(5):
                    raise AssertionError('writer did not try to begin')
                blocked_during_read.append(not committed.wait(1))
                return row

        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                if (threading.current_thread().name == 'note-reader'
                        and sql.startswith('SELECT CASE WHEN typeof(status)')
                        and 'FROM tasks WHERE task_id=?' in sql):
                    return ObservedCursor(cursor)
                return cursor

        def monitored_connect(*args, **kwargs):
            if threading.current_thread().name == 'note-reader':
                kwargs['factory'] = ObservedConnection
            return real_connect(*args, **kwargs)

        def writer():
            try:
                if not first_read.wait(5):
                    raise AssertionError('reader never sampled task status')
                with closing(real_connect(self.path, timeout=10)) as db:
                    attempting.set()
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE tasks SET status='PENDING' WHERE task_id='T1'")
                    db.execute("UPDATE allocation_outbox SET state='pending' WHERE task_id='T1'")
                    db.commit()
                    committed.set()
            except BaseException as exc:
                errors.append(exc)

        def reader():
            try:
                snapshots.append(notes.record('T1', '0' * 32, 'other')['snapshot'])
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=writer)
        reader_thread = threading.Thread(target=reader, name='note-reader')
        with patch('server.sim_investigation_notes.sqlite3.connect', side_effect=monitored_connect):
            worker.start()
            reader_thread.start()
            reader_thread.join(15)
            worker.join(15)
        self.assertFalse(reader_thread.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(blocked_during_read, [True])
        self.assertTrue(committed.is_set())
        self.assertEqual((snapshots[0]['status'], snapshots[0]['orders'][0]['state']),
                         ('ASSIGNED', 'uncertain'))

    def test_handoff_reopen_preserves_notes_and_file_replacement_is_rejected(self):
        from server.sim_handoff_store import SimHandoffStore
        from server.sim_investigation_notes import SimInvestigationNotes
        path = Path(self.tmp.name) / 'handoff.sqlite3'
        store = SimHandoffStore.create_new(path, simulation_enabled=True)
        store.create_task('T1')
        gate = RecoveryGate(path)
        gate.initialize()
        progress = SegmentProgress(store, two_segment_map(), simulation_enabled=True, recovery_gate=gate)
        progress.initialize()
        notes = SimInvestigationNotes(progress, {'sim-r1': ['T1']}, simulation=True)
        notes.initialize()
        first = notes.record('T1', '4' * 32, 'position')
        reopened = SimHandoffStore.open_existing(path, expected_creation_id=store.creation_id,
                                                  expected_idle_enabled=False, simulation_enabled=True)
        next_gate = RecoveryGate(path)
        next_gate.initialize()
        next_progress = SegmentProgress(reopened, two_segment_map(), simulation_enabled=True,
                                        recovery_gate=next_gate)
        fresh = SimInvestigationNotes(next_progress, {'sim-r1': ['T1']}, simulation=True)
        fresh.initialize()
        self.assertEqual(fresh.list('T1')['notes'], [first])
        original, copy = path.with_suffix('.original'), path.with_suffix('.copy')
        shutil.copyfile(path, copy)
        path.replace(original)
        copy.replace(path)
        with self.assertRaises(ValueError):
            fresh.record('T1', '5' * 32, 'other')


if __name__ == '__main__':
    unittest.main()
