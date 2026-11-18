"""Durable visit indexes are experimental records, not motion permits."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from server.occurrence_journal import JournalConflict, OccurrenceJournal


def order(seq, released):
    return {'order_id': 'T1', 'seq': seq, 'map_id': 'map@v1',
            'nodes': ['A', 'B', 'A', 'C', 'B', 'D'],
            'released_until': released, 'action_at_end': {'type': 'none'}}


class OccurrenceJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'visits.sqlite3'
        self.journal = OccurrenceJournal(self.path)
        self.journal.initialize()
        self.fp = 'a' * 64

    def start(self):
        return self.journal.start('sim-r1', 'session-1', self.fp, order(1, 'B'))

    def read(self):
        return self.journal.read('T1', 'session-1', self.fp, robot_id='sim-r1')

    def test_repeated_visit_survives_restart_and_is_reconstructed(self):
        self.assertEqual(self.start()['last_index'], 1)
        self.assertEqual(self.journal.append('T1', 1, 1, 'session-1', self.fp,
                                             order(2, 'B'), robot_id='sim-r1')['last_index'], 4)
        restarted = OccurrenceJournal(self.path)
        self.assertEqual(restarted.read('T1', 'session-1', self.fp, robot_id='sim-r1')['indices'], (1, 4))
        self.assertEqual(restarted.append('T1', 2, 4, 'session-1', self.fp,
                                           order(3, 'D'), robot_id='sim-r1')['last_index'], 5)

    def test_stale_session_map_or_transition_never_changes_the_record(self):
        self.start()
        for args in [(1, 1, 'session-other', self.fp, order(2, 'B')),
                     (1, 1, 'session-1', 'b' * 64, order(2, 'B')),
                     (1, 4, 'session-1', self.fp, order(2, 'B')),
                     (2, 1, 'session-1', self.fp, order(2, 'B')),
                     (1, 1, 'session-1', self.fp, order(3, 'D'))]:
            with self.subTest(args=args), self.assertRaises((ValueError, JournalConflict)):
                self.journal.append('T1', *args, robot_id='sim-r1')
        self.assertEqual(self.read()['indices'], (1,))
        with self.assertRaises(JournalConflict):
            self.journal.read('T1', 'session-other', self.fp, robot_id='sim-r1')
        with self.assertRaises(JournalConflict):
            self.journal.read('T1', 'session-1', self.fp, robot_id='sim-r2')
        with self.assertRaises(JournalConflict):
            self.journal.append('T1', 1, 1, 'session-1', self.fp, order(2, 'B'), robot_id='sim-r2')

    def test_tampered_index_or_missing_history_is_never_accepted(self):
        self.start()
        self.journal.append('T1', 1, 1, 'session-1', self.fp, order(2, 'B'), robot_id='sim-r1')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE occurrence_orders SET visit_index=2 WHERE task_id=? AND seq=2', ('T1',))
        with self.assertRaises(JournalConflict):
            self.read()
        with self.assertRaises(JournalConflict):
            self.journal.append('T1', 2, 4, 'session-1', self.fp, order(3, 'D'), robot_id='sim-r1')

    def test_missing_or_modified_record_is_not_reconstructed_as_proof(self):
        self.start()
        self.journal.append('T1', 1, 1, 'session-1', self.fp, order(2, 'B'), robot_id='sim-r1')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM occurrence_orders WHERE task_id=? AND seq=1', ('T1',))
        with self.assertRaises(JournalConflict):
            self.read()
        with self.assertRaises(JournalConflict):
            self.journal.append('T1', 2, 4, 'session-1', self.fp, order(3, 'D'), robot_id='sim-r1')

    def test_mismatched_order_digest_and_mismatched_map_block_loading(self):
        self.start()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE occurrence_orders SET digest=? WHERE task_id=?', ('b' * 64, 'T1'))
        with self.assertRaises(JournalConflict):
            self.read()
        with self.assertRaises(JournalConflict):
            self.journal.read('T1', 'session-1', 'c' * 64, robot_id='sim-r1')

    def test_competing_updates_only_one_can_advance(self):
        self.start()
        gate = threading.Barrier(2)
        def submit(release):
            gate.wait(timeout=5)
            try:
                return self.journal.append('T1', 1, 1, 'session-1', self.fp,
                                           order(2, release), robot_id='sim-r1')['last_index']
            except JournalConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ['B', 'D']))
        self.assertEqual(len([x for x in results if x is not None]), 1)
        self.assertEqual(self.read()['last_index'], next(x for x in results if x is not None))

    def test_initialization_does_not_modify_unrelated_database(self):
        other = Path(self.tmp.name) / 'other.sqlite3'
        with closing(sqlite3.connect(other)) as db, db:
            db.execute('CREATE TABLE unrelated (value TEXT)')
            db.execute('INSERT INTO unrelated VALUES (\'keep\')')
        with self.assertRaises(JournalConflict):
            OccurrenceJournal(other).initialize()
        with closing(sqlite3.connect(other)) as db:
            self.assertEqual(db.execute('SELECT value FROM unrelated').fetchone(), ('keep',))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'occurrence_%'").fetchone()[0], 0)

    def test_extra_trigger_or_altered_table_ddl_is_rejected(self):
        self.start()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TRIGGER rogue AFTER INSERT ON occurrence_orders BEGIN SELECT 1; END')
        with self.assertRaises(JournalConflict):
            self.read()
        with self.assertRaises(JournalConflict):
            self.journal.append('T1', 1, 1, 'session-1', self.fp, order(2, 'B'), robot_id='sim-r1')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TRIGGER rogue')
            db.execute('ALTER TABLE occurrence_orders ADD COLUMN extra TEXT')
        with self.assertRaises(JournalConflict):
            self.read()


if __name__ == '__main__':
    unittest.main()
