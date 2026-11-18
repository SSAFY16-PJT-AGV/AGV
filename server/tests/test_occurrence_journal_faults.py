"""Independent SQLite failure/race checks for the experimental visit journal."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server.occurrence_journal import JournalConflict, OccurrenceJournal


def _order(seq, stop):
    return {'order_id': 'T1', 'seq': seq, 'map_id': 'map@v1',
            'nodes': ['A', 'B', 'A', 'C', 'B', 'D'],
            'released_until': stop, 'action_at_end': {'type': 'none'}}


class OccurrenceJournalFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.sqlite3'
        self.first = OccurrenceJournal(self.path)
        self.first.initialize()
        self.fp = 'a' * 64
        self.first.start('sim-r1', 'session-1', self.fp, _order(1, 'B'))

    def test_conflicting_separate_instances_allow_only_one_advance(self):
        other = OccurrenceJournal(self.path)
        gate = threading.Barrier(2)

        def run(item):
            journal, stop = item
            gate.wait(timeout=5)
            try:
                return journal.append('T1', 1, 1, 'session-1', self.fp,
                                      _order(2, stop), robot_id='sim-r1')['last_index']
            except JournalConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, [(self.first, 'B'), (other, 'D')]))
        accepted = [item for item in outcomes if item is not None]
        self.assertEqual(len(accepted), 1)
        # Use a fresh instance to verify the committed result, not in-memory state.
        snapshot = OccurrenceJournal(self.path).read('T1', 'session-1', self.fp, robot_id='sim-r1')
        self.assertEqual(snapshot['indices'], (1, accepted[0]))

    def test_database_abort_does_not_leave_partial_history_or_changed_index(self):
        original_read = OccurrenceJournal._read
        calls = []

        def fail_after_writes(*args):
            calls.append(1)
            if len(calls) == 2:
                # append has already inserted the order and updated the index.
                raise RuntimeError('forced failure after both writes')
            return original_read(*args)

        with patch.object(OccurrenceJournal, '_read', side_effect=fail_after_writes):
            with self.assertRaisesRegex(RuntimeError, 'forced failure'):
                self.first.append('T1', 1, 1, 'session-1', self.fp, _order(2, 'B'), robot_id='sim-r1')
        self.assertEqual(len(calls), 2)
        snapshot = OccurrenceJournal(self.path).read('T1', 'session-1', self.fp, robot_id='sim-r1')
        self.assertEqual(snapshot['indices'], (1,))
        self.assertEqual(snapshot['last_index'], 1)

    def test_task_identifier_cannot_be_reused_for_another_robot_or_session(self):
        with self.assertRaises((ValueError, JournalConflict)):
            self.first.start('sim-r2', 'session-2', self.fp, _order(1, 'B'))
        snapshot = OccurrenceJournal(self.path).read('T1', 'session-1', self.fp, robot_id='sim-r1')
        self.assertEqual(snapshot['indices'], (1,))


if __name__ == '__main__':
    unittest.main()
