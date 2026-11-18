"""Independent failure and restart tests for the isolated allocation lab."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from server.occurrence_allocation_lab import LabConflict, OccurrenceAllocationLab
from server.route_occurrence import occurrence_clearance
from server.tests.test_occurrence_allocation_lab import laboratory_map


class AllocationLabFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'lab.sqlite3'
        self.map = laboratory_map()
        self.lab = OccurrenceAllocationLab(self.path, self.map, simulation=True)
        self.lab.initialize()
        self.job = self.lab.start('task', 'sim-a', 'session-a', 'E0', 'D')

    def ack(self, job):
        seq = job['orders'][-1]['seq']
        self.lab.mark_attempted('task', seq, 'session-a')
        self.lab.mark_broker_acked('task', seq, 'session-a')

    def event(self, job, event_id):
        order = job['orders'][-1]
        return {'type': 'rear_clear', 'event_id': event_id,
                'robot_session': 'session-a', 'order_id': 'task',
                'seq': order['seq'], 'map_id': order['map_id'],
                'stopped_at': order['released_until'],
                'cleared_resources': [list(x) for x in occurrence_clearance(
                    self.map, order['nodes'], job['start_index'])]}

    def advance(self, event):
        return self.lab.advance_simulated('sim-a', event, generation=self.lab.generation)

    def test_failure_after_all_writes_rolls_back_outbox_event_index_and_occupancy(self):
        self.ack(self.job)
        before = self.lab.snapshot('task')
        original = OccurrenceAllocationLab._load
        observed_event = []

        def fail_after_writes(instance, db, task_id, **kwargs):
            # Detect the inserted event in the same transaction so added
            # preflight reads cannot move this fault before the actual writes.
            if db.execute("SELECT 1 FROM lab_events WHERE event_id='e1'").fetchone():
                observed_event.append(True)
                raise RuntimeError('forced error after writes')
            return original(instance, db, task_id, **kwargs)

        with patch.object(OccurrenceAllocationLab, '_load', new=fail_after_writes):
            with self.assertRaisesRegex(RuntimeError, 'forced error'):
                self.advance(self.event(self.job, 'e1'))
        self.assertEqual(observed_event, [True])
        self.assertEqual(self.lab.snapshot('task'), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lab_events').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lab_orders').fetchone()[0], 1)

    def test_restart_fences_old_start_and_send_transitions(self):
        self.lab.mark_attempted('task', 1, 'session-a')
        restarted = OccurrenceAllocationLab(self.path, self.map, simulation=True)
        restarted.initialize()
        before = restarted.snapshot('task')
        self.assertEqual(before['status'], 'blocked')
        for call in (lambda: self.lab.mark_attempted('task', 1, 'session-a'),
                     lambda: self.lab.mark_broker_acked('task', 1, 'session-a'),
                     lambda: self.lab.start('other', 'sim-b', 'session-b', 'E5', 'Y')):
            with self.subTest(call=call), self.assertRaises(LabConflict):
                call()
        self.assertEqual(restarted.snapshot('task'), before)

    def test_restart_and_advance_race_leave_one_coherent_blocked_snapshot(self):
        self.ack(self.job)
        event = self.event(self.job, 'race')
        before = self.lab.snapshot('task')
        restarted = OccurrenceAllocationLab(self.path, self.map, simulation=True)
        barrier = threading.Barrier(2)

        def restart():
            barrier.wait(timeout=5)
            restarted.initialize()

        def advance():
            barrier.wait(timeout=5)
            try:
                return self.advance(event)
            except LabConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            restarting = pool.submit(restart)
            moving = pool.submit(advance)
            restarting.result(timeout=10)
            advanced = moving.result(timeout=10)
        observed = restarted.snapshot('task')
        self.assertEqual(observed['status'], 'blocked')
        expected = before if advanced is None else advanced
        for key in ('orders', 'order_states', 'start_index', 'end_index', 'occupancy'):
            self.assertEqual(observed[key], expected[key], key)

    def test_missing_order_history_does_not_change_retained_resources(self):
        self.ack(self.job)
        second = self.advance(self.event(self.job, 'e1'))
        with closing(sqlite3.connect(self.path)) as db, db:
            before = tuple(db.execute('SELECT kind,resource_id,task_id FROM lab_occupancy ORDER BY 1,2,3'))
            db.execute('DELETE FROM lab_orders WHERE task_id=? AND seq=?', ('task', 1))
        with self.assertRaises(LabConflict):
            self.lab.mark_attempted('task', 2, 'session-a')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(tuple(db.execute('SELECT kind,resource_id,task_id FROM lab_occupancy ORDER BY 1,2,3')),
                             before)

    def test_final_duplicate_is_idempotent_without_releasing_destination(self):
        job = self.job
        for number in (1, 2, 3):
            self.ack(job)
            event = self.event(job, 'e%d' % number)
            job = self.advance(event)
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['occupancy'], (('node', 'D'),))
        self.assertEqual(self.advance(event), job)

    def test_corrupt_completed_occupancy_blocks_new_task(self):
        job = self.job
        for number in (1, 2, 3):
            self.ack(job)
            job = self.advance(self.event(job, 'e%d' % number))
        self.assertEqual(job['status'], 'complete')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM lab_occupancy WHERE task_id=?', ('task',))
        # An unrelated new task cannot treat a corrupt completed robot as clear.
        with self.assertRaises(LabConflict):
            self.lab.start('other', 'sim-b', 'session-b', 'E5', 'Y')

    def test_corrupt_other_active_occupancy_blocks_new_task(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM lab_occupancy WHERE task_id=?', ('task',))
        with self.assertRaises(LabConflict):
            self.lab.start('other', 'sim-b', 'session-b', 'E5', 'Y')

    def test_orphan_occupancy_blocks_new_task(self):
        # Simulate external corruption using an SQLite connection with FK off.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO lab_occupancy VALUES (?,?,?)', ('node', 'orphan', 'missing-task'))
        with self.assertRaises(LabConflict):
            self.lab.start('other', 'sim-b', 'session-b', 'E5', 'Y')

    def test_orphan_order_blocks_new_task(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO lab_orders VALUES (?,?,?,?,?)',
                       ('missing-task', 1, '{}', 'fake', 'pending'))
        with self.assertRaises(LabConflict):
            self.lab.start('other', 'sim-b', 'session-b', 'E5', 'Y')

    def test_orphan_event_blocks_new_task(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO lab_events VALUES (?,?,?,?,?,?,?)',
                       ('sim-b', 'orphan', 'missing-task', 1, '{}', 'fake', self.lab.generation))
        with self.assertRaises(LabConflict):
            self.lab.start('other', 'sim-b', 'session-b', 'E5', 'Y')

    def test_other_robot_corruption_blocks_advance_and_preserves_own_occupancy(self):
        self.lab.start('other', 'sim-b', 'session-b', 'E5', 'Y')
        self.ack(self.job)
        before = self.lab.snapshot('task')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM lab_occupancy WHERE task_id=?', ('other',))
        with self.assertRaises(LabConflict):
            self.advance(self.event(self.job, 'e1'))
        with closing(sqlite3.connect(self.path)) as db:
            actual = set(db.execute('SELECT kind,resource_id FROM lab_occupancy WHERE task_id=?', ('task',)))
        self.assertEqual(actual, set(before['occupancy']))

    def test_other_robot_corruption_blocks_order_attempt(self):
        self.lab.start('other', 'sim-b', 'session-b', 'E5', 'Y')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM lab_occupancy WHERE task_id=?', ('other',))
        with self.assertRaises(LabConflict):
            self.lab.mark_attempted('task', 1, 'session-a')

    def test_restart_rejects_corruption_and_old_instance_cannot_resume(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM lab_occupancy WHERE task_id=?', ('task',))
        restarted = OccurrenceAllocationLab(self.path, self.map, simulation=True)
        with self.assertRaises(LabConflict):
            restarted.initialize()
        with self.assertRaises(LabConflict):
            self.lab.mark_attempted('task', 1, 'session-a')

if __name__ == '__main__':
    unittest.main()
