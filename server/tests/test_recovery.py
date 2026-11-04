import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from server.allocation_store import AllocationStore
from server.recovery import RecoveryError, RecoveryGate


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fleet.sqlite3'
        self.store = AllocationStore(self.path)
        self.store.initialize()
        self.store.create_task('T1')
        self.resources = [('node', 'N1'), ('node', 'N2'), ('edge', '["N1","N2"]')]
        self.store.allocate('T1', 'sim-r1', 'layout@v1', ['N1', 'N2', 'N3'], 'N2',
                            self.resources, {'type': 'load'}, map_fingerprint='a' * 64)
        self.now = [100.0]
        self.gate = RecoveryGate(self.path, clock=lambda: self.now[0], ttl=10)
        self.gate.initialize()

    def _attempt(self):
        allocation = self.store.get_allocation('T1')
        record = self.store.get_order('T1')
        self.store.mark_attempted('T1', record['payload'], allocation, self.resources)
        return record['payload']

    def _response(self, token, payload, **changes):
        response = dict(robot_id='sim-r1', order_id='T1', seq=1, map_id='layout@v1',
                        stopped_at='N2', stopped=True,
                        order_digest=hashlib.sha256(json.dumps(
                            json.loads(payload), sort_keys=True, ensure_ascii=True,
                            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest())
        response.update(changes)
        return self.gate.complete('T1', 'boot-1', token, **response)

    def _required(self, gate=None, session='boot-1', seq=1, node='N2'):
        target = gate or self.gate
        with target.transaction() as db:
            return target.require_reconciled(
                db, 'T1', session, expected_seq=seq, expected_stopped_at=node)

    def _two_proofs(self):
        payload = self._attempt()
        self._response(self.gate.begin('T1', 'boot-1'), payload)
        self.store.create_task('T2')
        resources = [('node', 'N4'), ('node', 'N5'), ('edge', '["N4","N5"]')]
        self.store.allocate('T2', 'sim-r2', 'layout@v1', ['N4', 'N5'], 'N5',
                            resources, {'type': 'load'}, map_fingerprint='a' * 64)
        allocation = self.store.get_allocation('T2')
        payload = self.store.get_order('T2')['payload']
        self.store.mark_attempted('T2', payload, allocation, resources)
        digest = hashlib.sha256(json.dumps(json.loads(payload), sort_keys=True,
            ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        token = self.gate.begin('T2', 'boot-2')
        self.gate.complete('T2', 'boot-2', token, robot_id='sim-r2', order_id='T2',
                           seq=1, map_id='layout@v1', stopped_at='N5', stopped=True,
                           order_digest=digest)

    def _required_second(self):
        with self.gate.transaction() as db:
            self.gate.require_reconciled(db, 'T2', 'boot-2', expected_seq=1,
                                         expected_stopped_at='N5')

    def test_invalidate_many_revokes_all_proofs_together_without_freeing_resources(self):
        self._two_proofs()
        self._required()
        self._required_second()
        self.gate.invalidate_many(['T1', 'T2'])
        for require in (self._required, self._required_second):
            with self.assertRaises(RecoveryError):
                require()
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT task_id,status FROM recovery_challenges ORDER BY task_id').fetchall()
        self.assertEqual(rows, [('T1', 'revoked'), ('T2', 'revoked')])
        self.assertEqual(len(self.store.list_occupancy()), 6)

    def test_invalidate_many_validates_entire_collection_before_revoking(self):
        self._two_proofs()
        for ids in ([], ['T1', 'T1'], ['T1', ''], 'T1', ['T1', None]):
            with self.subTest(ids=ids):
                with self.assertRaises((RecoveryError, ValueError)):
                    self.gate.invalidate_many(ids)
                self._required()
                self._required_second()

    def test_invalidate_many_blocks_every_task_before_database_attempt_even_on_failure(self):
        self._two_proofs()
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute('BEGIN IMMEDIATE')
        entered = threading.Event()
        errors = []

        def connect():
            entered.set()
            return sqlite3.connect(self.path, timeout=0.1, isolation_level=None)

        self.gate._connect = connect
        def revoke():
            try:
                self.gate.invalidate_many(['T1', 'T2'])
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=revoke)
        thread.start()
        self.assertTrue(entered.wait(3))
        with self.gate._state_lock:
            self.assertTrue({'T1', 'T2'}.issubset(self.gate._blocked))
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], sqlite3.OperationalError)
        blocker.execute('ROLLBACK')
        for require in (self._required, self._required_second):
            with self.assertRaises(RecoveryError):
                require()
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT task_id,status FROM recovery_challenges ORDER BY task_id').fetchall()
        self.assertEqual(rows, [('T1', 'verified'), ('T2', 'verified')])

    def test_invalidate_many_inside_transaction_blocks_all_without_deadlock(self):
        self._two_proofs()
        with self.gate.transaction():
            with self.assertRaises(RecoveryError):
                self.gate.invalidate_many(['T1', 'T2'])
        for require in (self._required, self._required_second):
            with self.assertRaises(RecoveryError):
                require()

    def test_invalidate_many_rolls_back_all_rows_if_later_update_fails(self):
        self._two_proofs()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER reject_second_revocation BEFORE UPDATE ON recovery_challenges "
                       "WHEN OLD.task_id = 'T2' BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.gate.invalidate_many(['T1', 'T2'])
        for require in (self._required, self._required_second):
            with self.assertRaises(RecoveryError):
                require()
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT task_id,status FROM recovery_challenges ORDER BY task_id').fetchall()
        self.assertEqual(rows, [('T1', 'verified'), ('T2', 'verified')])

    def test_invalidate_many_inside_serialized_is_reentrant(self):
        self._two_proofs()
        with self.gate.serialized():
            self.gate.invalidate_many(['T1', 'T2'])
        for require in (self._required, self._required_second):
            with self.assertRaises(RecoveryError):
                require()

    def test_pending_order_is_not_treated_as_applied(self):
        with self.assertRaises(RecoveryError):
            self.gate.begin('T1', 'boot-1')
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')
        self.assertEqual(len(self.store.list_occupancy()), 3)

    def test_verified_response_is_one_use_and_bound_to_session_sequence_and_node(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        self._required()
        for session, seq, node in [('boot-2', 1, 'N2'), ('boot-1', 2, 'N2'),
                                   ('boot-1', 1, 'N1')]:
            with self.subTest(session=session, seq=seq, node=node):
                with self.assertRaises(RecoveryError):
                    self._required(session=session, seq=seq, node=node)
        with self.assertRaises(RecoveryError):
            self._response(token, payload)

    def test_invalid_reply_consumes_challenge_and_never_unlocks(self):
        payload = self._attempt()
        for change in ({'robot_id': 'sim-r2'}, {'seq': 2}, {'map_id': 'layout@v2'},
                       {'stopped_at': 'N1'}, {'stopped': False}, {'order_digest': '0' * 64}):
            token = self.gate.begin('T1', 'boot-1')
            with self.assertRaises(RecoveryError):
                self._response(token, payload, **change)
            with self.assertRaises(RecoveryError):
                self._response(token, payload)
            with self.assertRaises(RecoveryError):
                self._required()
        self.assertEqual(len(self.store.list_occupancy()), 3)

    def test_new_epoch_and_new_challenge_invalidate_prior_proof(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        self.gate.begin('T1', 'boot-2')
        with self.assertRaises(RecoveryError):
            self._required()
        next_gate = RecoveryGate(self.path, clock=lambda: self.now[0], ttl=10)
        next_gate.initialize()
        with self.assertRaises(RecoveryError):
            self._required(gate=next_gate)
        with self.assertRaises(RecoveryError):
            self.gate.begin('T1', 'boot-1')
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertEqual(len(self.store.list_occupancy()), 3)

    def test_expired_challenge_and_reconciliation_fail_closed(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self.now[0] += 11
        with self.assertRaises(RecoveryError):
            self._response(token, payload)
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        self.now[0] += 11
        with self.assertRaises(RecoveryError):
            self._required()

    def test_db_change_between_challenge_and_reply_is_rejected(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM allocation_occupancy WHERE kind='edge'")
            db.commit()
        with self.assertRaises(RecoveryError):
            self._response(token, payload)
        with self.assertRaises(RecoveryError):
            self._required()

    def test_only_one_of_two_concurrent_completions_succeeds(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        barrier = threading.Barrier(2)
        results = []
        def complete():
            barrier.wait()
            try:
                self._response(token, payload)
                results.append(True)
            except RecoveryError:
                results.append(False)
        threads = [threading.Thread(target=complete) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(results), [False, True])

    def test_pending_next_segment_recovers_only_previous_attempt_without_sending_it(self):
        payload = self._attempt()
        previous = json.loads(payload)
        next_order = dict(previous, seq=2, released_until='N3')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE progress_orders(task_id TEXT, seq INTEGER, payload TEXT, '
                       'state TEXT, PRIMARY KEY(task_id,seq))')
            db.execute("INSERT INTO progress_orders VALUES (?,1,?,'uncertain')", ('T1', payload))
            db.execute("INSERT INTO progress_orders VALUES (?,2,?,'pending')", ('T1', json.dumps(next_order)))
            db.execute("UPDATE allocation_jobs SET released_until='N3' WHERE task_id='T1'")
            db.execute("INSERT INTO allocation_occupancy VALUES ('node','N3','T1')")
            db.commit()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        self._required(seq=1, node='N2')
        with self.assertRaises(RecoveryError):
            self._required(seq=2, node='N3')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state FROM progress_orders WHERE seq=2").fetchone()[0], 'pending')
        self.assertEqual(len(self.store.list_occupancy()), 4)

    def test_disconnect_invalidation_revokes_previous_proof(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        self._required()
        self.gate.invalidate('T1')
        with self.assertRaises(RecoveryError):
            self._required()

    def test_failed_new_challenge_revokes_previous_verified_session(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        self._required()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE allocation_outbox SET state='pending' WHERE task_id='T1'")
            db.commit()
        with self.assertRaises(RecoveryError):
            self.gate.begin('T1', 'boot-2')
        with self.assertRaises(RecoveryError):
            self._required()

    def test_proven_order_payload_cannot_change_before_progress(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        self._required()
        altered = json.loads(payload)
        altered['action_at_end'] = {'type': 'unload'}
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('UPDATE allocation_outbox SET payload=? WHERE task_id=?',
                       (json.dumps(altered), 'T1'))
            db.commit()
        with self.assertRaises(RecoveryError):
            self._required()

    def test_proven_map_fingerprint_cannot_change_before_progress(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE allocation_meta SET value=? WHERE key='map_fingerprint'", ('b' * 64,))
            db.commit()
        with self.assertRaises(RecoveryError):
            self._required()

    def test_stopped_node_must_remain_owned_by_task(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM allocation_occupancy WHERE kind='node' AND resource_id='N2'")
            db.commit()
        with self.assertRaises(RecoveryError):
            self._required()

    def test_gate_serializes_transaction_commit_and_invalidation(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        started = threading.Event()
        invalidated = threading.Event()
        done = []

        def invalidate():
            started.set()
            self.gate.invalidate('T1')
            done.append('invalidated')
            invalidated.set()

        with self.gate.transaction() as db:
            self.gate.require_reconciled(db, 'T1', 'boot-1', expected_seq=1, expected_stopped_at='N2')
            thread = threading.Thread(target=invalidate)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(invalidated.wait(0.1))
            done.append('transaction')
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(done, ['transaction', 'invalidated'])
        with self.assertRaises(RecoveryError):
            self._required()

    def test_gate_transaction_rolls_back_before_releasing_lock(self):
        with self.assertRaises(RuntimeError):
            with self.gate.transaction() as db:
                db.execute("UPDATE tasks SET status='FAILED' WHERE task_id='T1'")
                raise RuntimeError('abort')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone()[0], 'ASSIGNED')

    def test_reentrant_invalidation_does_not_deadlock_and_blocks_proof(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        with self.gate.transaction() as db:
            with self.assertRaises(RecoveryError):
                self.gate.invalidate('T1')
            with self.assertRaises(RecoveryError):
                self.gate.require_reconciled(db, 'T1', 'boot-1', expected_seq=1, expected_stopped_at='N2')
        with self.assertRaises(RecoveryError):
            self._required()

    def test_serialized_allows_nested_gate_transaction_but_blocks_invalidation_until_exit(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        started, finished = threading.Event(), threading.Event()
        def invalidate():
            started.set()
            self.gate.invalidate('T1')
            finished.set()
        with self.gate.serialized():
            with self.gate.transaction() as db:
                self.gate.require_reconciled(db, 'T1', 'boot-1', expected_seq=1, expected_stopped_at='N2')
            thread = threading.Thread(target=invalidate)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.1))
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())
        with self.assertRaises(RecoveryError):
            self._required()

    def test_serialized_inside_database_transaction_is_rejected(self):
        with self.gate.transaction():
            with self.assertRaises(RecoveryError):
                with self.gate.serialized():
                    pass

    def test_transaction_and_database_are_required_for_proof(self):
        payload = self._attempt()
        token = self.gate.begin('T1', 'boot-1')
        self._response(token, payload)
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(RecoveryError):
                self.gate.require_reconciled(db, 'T1', 'boot-1', expected_seq=1, expected_stopped_at='N2')


if __name__ == '__main__':
    unittest.main()
