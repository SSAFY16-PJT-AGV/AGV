"""Tests for proposed offline command records, not a robot or broker."""
import concurrent.futures
from contextlib import closing
import copy
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.order_publisher import _payload


def sample_order(seq=1, release='B'):
    return {'order_id': 'task-1', 'seq': seq, 'map_id': 'mat@v1',
            'nodes': ['A', 'B', 'C'], 'released_until': release,
            'action_at_end': {'type': 'load'}}


class ProposedCommandJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.sqlite'

    def make(self):
        if importlib.util.find_spec('server.proposed_command_journal') is None:
            return None
        from server.proposed_command_journal import ProposedCommandJournal
        return ProposedCommandJournal.create(self.path, simulation=True)

    def test_persists_attempt_as_uncertain_before_any_transport(self):
        journal = self.make()
        actual = None
        if journal is not None:
            journal.stage('sim-r1', 'boot-1', sample_order())
            journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
            actual = journal.get('sim-r1', 'task-1', 1)['state']
        self.assertEqual(actual, 'uncertain')

    def test_new_journal_exposes_independent_creation_identity(self):
        journal = self.make()
        creation_id = getattr(journal, 'creation_id', None)
        self.assertIsInstance(creation_id, str)
        self.assertRegex(creation_id, r'^[0-9a-f]{64}$')

    def test_existing_journal_requires_expected_creation_identity(self):
        from server.proposed_command_journal import ProposedCommandJournal
        journal = self.make()
        creation_id = getattr(journal, 'creation_id', '0' * 64)
        with self.assertRaises(ValueError):
            ProposedCommandJournal.open_existing(self.path, simulation=True)
        for invalid in ('', 'A' * 64, '1' * 63, None, 7):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ProposedCommandJournal.open_existing(self.path, simulation=True,
                                                     expected_creation_id=invalid)
        with self.assertRaises(ValueError):
            ProposedCommandJournal.open_existing(self.path, simulation=True, expected_creation_id='0' * 64)
        reopened = ProposedCommandJournal.open_existing(
            self.path, simulation=True, expected_creation_id=creation_id)
        self.assertEqual(reopened.creation_id, creation_id)

    def test_independently_created_valid_journal_does_not_match_identity(self):
        from server.proposed_command_journal import ProposedCommandJournal
        journal = self.make()
        other_path = Path(self.tmp.name) / 'other.sqlite'
        other = ProposedCommandJournal.create(other_path, simulation=True)
        with self.assertRaises(ValueError):
            ProposedCommandJournal.open_existing(other_path, simulation=True,
                                                 expected_creation_id=journal.creation_id)
        self.assertNotEqual(journal.creation_id, other.creation_id)
        expected_creation_id = journal.creation_id
        os.replace(other_path, self.path)
        with self.assertRaises(ValueError):
            ProposedCommandJournal.open_existing(self.path, simulation=True,
                                                 expected_creation_id=expected_creation_id)

    def test_file_identity_is_checked_again_after_sqlite_open(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        real_connect = sqlite3.connect
        opened = []
        def replaced_during_open(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            journal._file_identity = (-1, -1)
            return connection
        with patch('server.proposed_command_journal.sqlite3.connect', side_effect=replaced_during_open):
            with self.assertRaises(JournalConflict):
                journal.get('sim-r1', 'task-1', 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')

    def test_reopen_fences_old_owner_blocks_pending_and_preserves_uncertain(self):
        from server.proposed_command_journal import ProposedCommandJournal, JournalConflict
        journal = self.make()
        journal.stage('sim-r1', 'boot-1', sample_order())
        journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
        second = dict(sample_order(), order_id='task-2')
        journal.stage('sim-r2', 'boot-2', second)
        reopened = ProposedCommandJournal.open_existing(self.path, simulation=True,
                                                        expected_creation_id=journal.creation_id)
        self.assertEqual(reopened.get('sim-r1', 'task-1', 1)['state'], 'uncertain')
        self.assertEqual(reopened.get('sim-r2', 'task-2', 1)['state'], 'blocked')
        with self.assertRaises(JournalConflict):
            reopened.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
        with self.assertRaises(JournalConflict):
            reopened.mark_attempted('sim-r2', 'task-2', 1, expected_payload=_payload(second))
        with self.assertRaises(JournalConflict):
            journal.stage('sim-r3', 'boot-3', dict(sample_order(), order_id='task-3'))

    def test_stage_validates_exact_wire_identity_and_is_idempotent(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        original = sample_order()
        first = journal.stage('sim-r1', 'boot-1', original)
        self.assertEqual(journal.stage('sim-r1', 'boot-1', copy.deepcopy(original)), first)
        with self.assertRaises(JournalConflict):
            journal.stage('sim-r1', 'boot-1', dict(original, released_until='C'))
        with self.assertRaises(JournalConflict):
            journal.stage('sim-r1', 'boot-2', original)

    def test_attempt_rejects_different_exact_payload_without_state_change(self):
        from server.order_publisher import _payload
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        journal.stage('sim-r1', 'boot-1', sample_order())
        different = _payload(dict(sample_order(), released_until='C'))
        try:
            journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=different)
        except JournalConflict:
            pass
        except TypeError:
            self.fail('mark_attempted must validate the expected wire bytes')
        else:
            self.fail('mismatched payload was accepted')
        self.assertEqual(journal.get('sim-r1', 'task-1', 1)['state'], 'pending')

    def test_attempt_requires_immutable_bytes_and_returns_stored_wire_bytes(self):
        journal = self.make()
        staged = journal.stage('sim-r1', 'boot-1', sample_order())
        for invalid in (None, bytearray(staged['payload']), staged['payload'].decode('utf-8')):
            with self.subTest(invalid=type(invalid)), self.assertRaises(ValueError):
                journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=invalid)
        self.assertEqual(journal.get('sim-r1', 'task-1', 1)['state'], 'pending')
        self.assertEqual(journal.mark_attempted('sim-r1', 'task-1', 1,
                                               expected_payload=staged['payload']), staged['payload'])

    def test_same_robot_cannot_stage_a_different_task_in_any_state(self):
        from server.proposed_command_journal import JournalConflict
        for state in ('pending', 'uncertain', 'accepted', 'rejected', 'blocked'):
            with self.subTest(state=state):
                path = Path(self.tmp.name) / (state + '.sqlite')
                from server.proposed_command_journal import ProposedCommandJournal
                journal = ProposedCommandJournal.create(path, simulation=True)
                journal.stage('sim-r1', 'boot-1', sample_order())
                if state in ('uncertain', 'accepted', 'rejected'):
                    journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
                if state in ('accepted', 'rejected'):
                    journal.record_response('sim-r1', self.response(journal, outcome=state))
                if state == 'blocked':
                    journal = ProposedCommandJournal.open_existing(path, simulation=True,
                                                                  expected_creation_id=journal.creation_id)
                other = dict(sample_order(), order_id='task-other')
                with self.assertRaises(JournalConflict):
                    journal.stage('sim-r1', 'boot-1', other)

    def test_simultaneous_different_tasks_for_one_robot_have_one_winner(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        def stage(task_id):
            try:
                journal.stage('sim-r1', 'boot-1', dict(sample_order(), order_id=task_id))
                return 'won'
            except JournalConflict:
                return 'blocked'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(stage, ('task-1', 'task-2')))
        self.assertEqual(sorted(results), ['blocked', 'won'])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM commands').fetchone(), (1,))

    def test_first_sequence_and_next_sequence_must_be_contiguous(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        with self.assertRaises(JournalConflict):
            journal.stage('sim-r1', 'boot-1', sample_order(2, 'C'))
        journal.stage('sim-r1', 'boot-1', sample_order())
        with self.assertRaises(JournalConflict):
            journal.stage('sim-r1', 'boot-1', sample_order(3, 'C'))
        with self.assertRaises(JournalConflict):
            journal.stage('sim-r1', 'boot-1', sample_order(2, 'C'))

    def response(self, journal, event_id='event-1', outcome='accepted'):
        order = journal.get('sim-r1', 'task-1', 1)
        return {'event_id': event_id, 'robot_session': 'boot-1', 'order_id': 'task-1',
                'seq': 1, 'map_id': 'mat@v1', 'order_digest': order['digest'], 'outcome': outcome}

    def test_matching_proposed_response_is_idempotent_but_conflicts_are_rejected(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        journal.stage('sim-r1', 'boot-1', sample_order())
        journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
        event = self.response(journal)
        self.assertEqual(journal.record_response('sim-r1', event), 'accepted')
        self.assertEqual(journal.record_response('sim-r1', event), 'accepted')
        with self.assertRaises(JournalConflict):
            journal.record_response('sim-r1', dict(event, outcome='rejected'))
        with self.assertRaises(JournalConflict):
            journal.record_response('sim-r1', dict(event, event_id='event-2'))
        self.assertEqual(journal.get('sim-r1', 'task-1', 1)['state'], 'accepted')
        journal.stage('sim-r1', 'boot-1', sample_order(2, 'C'))

    def test_response_cannot_change_wrong_session_map_digest_or_sequence(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        journal.stage('sim-r1', 'boot-1', sample_order())
        journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
        event = self.response(journal)
        for field, value in [('robot_session', 'other'), ('map_id', 'other@v1'),
                             ('order_digest', '0' * 64), ('seq', 2)]:
            with self.subTest(field=field), self.assertRaises(JournalConflict):
                journal.record_response('sim-r1', dict(event, **{field: value}))
        self.assertEqual(journal.get('sim-r1', 'task-1', 1)['state'], 'uncertain')

    def test_rejected_or_retained_response_cannot_advance_sequence(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        journal.stage('sim-r1', 'boot-1', sample_order())
        journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
        event = self.response(journal, outcome='rejected')
        with self.assertRaises(JournalConflict):
            journal.record_response('sim-r1', event, retained=True)
        self.assertEqual(journal.get('sim-r1', 'task-1', 1)['state'], 'uncertain')
        self.assertEqual(journal.record_response('sim-r1', event), 'rejected')
        with self.assertRaises(JournalConflict):
            journal.stage('sim-r1', 'boot-1', sample_order(2, 'C'))

    def test_response_write_failure_rolls_back_event_and_state(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        journal.stage('sim-r1', 'boot-1', sample_order())
        journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
        event = self.response(journal)
        original = journal._validate_all
        counter = [0]
        def fail_after_write(db, **kwargs):
            counter[0] += 1
            if counter[0] == 2:
                raise JournalConflict('injected integrity failure')
            return original(db, **kwargs)
        with patch.object(journal, '_validate_all', side_effect=fail_after_write):
            with self.assertRaises(JournalConflict):
                journal.record_response('sim-r1', event)
        self.assertEqual(journal.get('sim-r1', 'task-1', 1)['state'], 'uncertain')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM responses').fetchone(), (0,))

    def test_attempt_transition_is_single_winner_across_threads(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        journal.stage('sim-r1', 'boot-1', sample_order())
        def attempt(_):
            try:
                journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
                return 'won'
            except JournalConflict:
                return 'blocked'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sorted(results), ['blocked', 'won'])

    def test_corrupted_payload_or_event_does_not_allow_more_mutations(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        journal.stage('sim-r1', 'boot-1', sample_order())
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("UPDATE commands SET payload = '{}' WHERE order_id = 'task-1'")
        with self.assertRaises(JournalConflict):
            journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))

    def test_corrupt_event_identity_is_detected_before_further_work(self):
        from server.proposed_command_journal import JournalConflict
        journal = self.make()
        journal.stage('sim-r1', 'boot-1', sample_order())
        journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
        journal.record_response('sim-r1', self.response(journal))
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("UPDATE responses SET event_id='other'")
        with self.assertRaises(JournalConflict):
            journal.stage('sim-r1', 'boot-1', sample_order(2, 'C'))

    def test_prior_generation_acceptance_does_not_authorize_next_command(self):
        from server.proposed_command_journal import ProposedCommandJournal, JournalConflict
        journal = self.make()
        journal.stage('sim-r1', 'boot-1', sample_order())
        journal.mark_attempted('sim-r1', 'task-1', 1, expected_payload=_payload(sample_order()))
        journal.record_response('sim-r1', self.response(journal))
        reopened = ProposedCommandJournal.open_existing(self.path, simulation=True,
                                                        expected_creation_id=journal.creation_id)
        with self.assertRaises(JournalConflict):
            reopened.stage('sim-r1', 'boot-1', sample_order(2, 'C'))

    def test_simulation_must_be_explicit_and_cannot_open_an_unrelated_db(self):
        from server.proposed_command_journal import ProposedCommandJournal
        with self.assertRaises(ValueError):
            ProposedCommandJournal.create(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute('CREATE TABLE unrelated (x INTEGER)')
        with self.assertRaises(ValueError):
            ProposedCommandJournal.open_existing(self.path, simulation=True, expected_creation_id='1' * 64)


if __name__ == '__main__':
    unittest.main()
