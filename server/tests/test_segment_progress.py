import copy
import hashlib
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from server.allocation_store import AllocationStore, AllocationConflict
from server.segments import plan_segment


def map_data():
    return {'map_id': 'layout', 'version': 4, 'unit': 'mm',
            'nodes': [{'id': 'N'+str(i), 'pos': [10*i, 0]} for i in range(1, 6)],
            'edges': [{'from': 'N'+str(i), 'to': 'N'+str(i+1),
                       'shape': [[10*i, 0], [10*(i+1), 0]],
                       'bidirectional': True, 'lanes': 1} for i in range(1, 5)],
            'zones': [{'id': 'cross1', 'type': 'shared', 'nodes': ['N2']},
                      {'id': 'cross2', 'type': 'shared', 'nodes': ['N4']},
                      {'id': 'wait3', 'type': 'waiting', 'node': 'N3'},
                      {'id': 'wait5', 'type': 'waiting', 'node': 'N5'}]}


class FakeGate:
    def __init__(self, store):
        self.store = store
        self.expected = ('T1', 'boot-1', 1, 'N3')

    @contextmanager
    def transaction(self):
        with self.store._write() as db:
            yield db

    def require_reconciled(self, connection, task_id, robot_session, *, expected_seq, expected_stopped_at):
        if (task_id, robot_session, expected_seq, expected_stopped_at) != self.expected:
            raise ValueError('not reconciled')


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AllocationStore(Path(self.tmp.name) / 'progress.sqlite3')
        self.store.initialize()
        self.map = map_data()
        self.path = ['N1', 'N2', 'N3', 'N4', 'N5']
        self.segment = plan_segment(self.map, self.path)
        self.fingerprint = hashlib.sha256(json.dumps(
            self.map, sort_keys=True, ensure_ascii=True, allow_nan=False,
            separators=(',', ':')).encode('ascii')).hexdigest()
        self.store.create_task('T1')
        allocation = self.store.allocate('T1', 'sim-r1', 'layout@v4', self.path,
            self.segment.released_until, self.segment.resources, {'type': 'load'},
            map_fingerprint=self.fingerprint)
        order = self.store.get_order('T1')
        self.store.mark_attempted('T1', order['payload'], allocation, self.segment.resources)
        self.store.mark_broker_acked('T1', order['payload'], allocation, self.segment.resources)
        self.gate = FakeGate(self.store)
        from server.segment_progress import SegmentProgress
        self.progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=self.gate)
        self.progress.initialize()
        following = plan_segment(self.map, self.path[2:])
        self.old = set(self.segment.resources)
        self.new = set(following.resources)
        self.event = {'type': 'rear_clear', 'event_id': 'event-1', 'robot_session': 'boot-1',
                      'order_id': 'T1', 'seq': 1, 'map_id': 'layout@v4',
                      'stopped_at': 'N3',
                      'cleared_resources': [list(x) for x in sorted(self.old - self.new)]}

    def test_advance_records_event_and_reserves_next_segment_atomically(self):
        result = self.progress.advance('sim-r1', self.event)
        self.assertEqual((result['status'], result['next_seq']), ('advanced', 2))
        self.assertEqual(set(self.store.list_occupancy()), self.new)
        record = self.progress.get_order('T1', 2)
        self.assertEqual(record['state'], 'pending')
        self.assertEqual(json.loads(record['payload'])['released_until'], 'N5')
        self.assertEqual(json.loads(self.store.get_order('T1')['payload'])['seq'], 1)
        self.assertEqual(self.progress.advance('sim-r1', self.event)['status'], 'duplicate')

    def test_wrong_session_robot_retained_and_clearance_leave_occupancy(self):
        for robot, event, retained in [('sim-r2', self.event, False),
                ('sim-r1', dict(self.event, robot_session='old'), False),
                ('sim-r1', dict(self.event, cleared_resources=[]), False),
                ('sim-r1', self.event, True)]:
            with self.subTest(robot=robot, event=event, retained=retained):
                with self.assertRaises((ValueError, AllocationConflict)):
                    self.progress.advance(robot, event, retained=retained)
                self.assertEqual(set(self.store.list_occupancy()), self.old)

    def test_conflicting_duplicate_event_is_rejected(self):
        self.progress.advance('sim-r1', self.event)
        with self.assertRaises((ValueError, AllocationConflict)):
            self.progress.advance('sim-r1', dict(self.event, stopped_at='N4'))
        self.assertEqual(set(self.store.list_occupancy()), self.new)

    def test_blocked_next_segment_rolls_back_entire_event(self):
        self.store.create_task('T2')
        self.store.allocate('T2', 'sim-r2', 'layout@v4', ['N4', 'N5'], 'N5',
            [('node','N4'),('node','N5'),('edge','["N4","N5"]'),('zone','cross2')],
            {'type':'load'}, map_fingerprint=self.fingerprint)
        before = self.store.list_occupancy()
        with self.assertRaises((ValueError, AllocationConflict)):
            self.progress.advance('sim-r1', self.event)
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertIsNone(self.progress.get_order('T1', 2))

    def test_disabled_or_changed_map_fails_without_mutation(self):
        from server.segment_progress import SegmentProgress
        disabled = SegmentProgress(self.store, self.map, recovery_gate=self.gate)
        with self.assertRaises((ValueError, RuntimeError)):
            disabled.advance('sim-r1', self.event)
        altered = copy.deepcopy(self.map); altered['nodes'][0]['pos'] = [999, 0]
        with self.assertRaises((ValueError, AllocationConflict)):
            other = SegmentProgress(self.store, altered, simulation_enabled=True, recovery_gate=self.gate)
            other.initialize(); other.advance('sim-r1', self.event)
        self.assertEqual(set(self.store.list_occupancy()), self.old)

    def test_new_order_transition_and_final_clear_keep_waiting_node(self):
        result = self.progress.advance('sim-r1', self.event)
        payload = result['payload']
        with self.assertRaises((ValueError, AllocationConflict)):
            self.progress.mark_attempted('T1', 2, 'wrong', payload, self.new)
        with self.assertRaises((ValueError, AllocationConflict)):
            self.progress.mark_attempted('T1', 2, 'boot-1', payload, {('node', 'N3')})
        self.progress.mark_attempted('T1', 2, 'boot-1', payload, self.new)
        with self.assertRaises((ValueError, AllocationConflict)):
            self.progress.mark_attempted('T1', 2, 'boot-1', payload, self.new)
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')
        self.progress.mark_broker_acked('T1', 2, 'boot-1', payload, self.new)
        self.gate.expected = ('T1', 'boot-1', 2, 'N5')
        final = {'type': 'rear_clear', 'event_id': 'event-2', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 2, 'map_id': 'layout@v4', 'stopped_at': 'N5',
                 'cleared_resources': [list(x) for x in sorted(self.new - {('node', 'N5')})]}
        result = self.progress.advance('sim-r1', final)
        self.assertEqual((result['status'], result['next_seq']), ('finished', None))
        self.assertEqual(self.store.list_occupancy(), {('node', 'N5'): 'sim-r1'})
        self.assertEqual(self.progress.advance('sim-r1', final)['status'], 'duplicate')
        self.assertEqual(self.store.get_order('T1')['state'], 'acked')

    def test_pending_order_cannot_be_reported_as_rear_clear(self):
        self.progress.advance('sim-r1', self.event)
        event2 = dict(self.event, event_id='event-2', seq=2, stopped_at='N5',
                      cleared_resources=[list(x) for x in sorted(self.new - {('node','N5')})])
        self.gate.expected = ('T1', 'boot-1', 2, 'N5')
        with self.assertRaises((ValueError, AllocationConflict)):
            self.progress.advance('sim-r1', event2)
        self.assertEqual(set(self.store.list_occupancy()), self.new)

    def test_mark_acked_rechecks_database_and_keeps_uncertain_on_tamper(self):
        payload = self.progress.advance('sim-r1', self.event)['payload']
        self.progress.mark_attempted('T1', 2, 'boot-1', payload, self.new)
        with self.store._write() as db:
            db.execute("DELETE FROM allocation_occupancy WHERE task_id='T1' AND kind='zone' AND resource_id='cross2'")
        with self.assertRaises((ValueError, AllocationConflict)):
            self.progress.mark_broker_acked('T1', 2, 'boot-1', payload, self.new)
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')

    def test_duplicate_result_survives_new_progress_instance(self):
        from server.segment_progress import SegmentProgress
        self.progress.advance('sim-r1', self.event)
        reopened = SegmentProgress(AllocationStore(self.store.path), self.map,
                                   simulation_enabled=True, recovery_gate=self.gate)
        reopened.initialize()
        self.assertEqual(reopened.advance('sim-r1', self.event)['status'], 'duplicate')
        self.assertEqual(set(self.store.list_occupancy()), self.new)

    def test_old_duplicate_is_rejected_after_new_proof(self):
        self.progress.advance('sim-r1', self.event)
        self.gate.expected = ('T1', 'boot-1', 2, 'N5')
        with self.assertRaises(ValueError):
            self.progress.advance('sim-r1', self.event)
        self.assertEqual(set(self.store.list_occupancy()), self.new)

    def test_concurrent_duplicate_event_only_advances_once(self):
        barrier = threading.Barrier(2)
        results, errors = [], []
        def run():
            try:
                barrier.wait()
                results.append(self.progress.advance('sim-r1', self.event)['status'])
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ['advanced', 'duplicate'])
        self.assertEqual(set(self.store.list_occupancy()), self.new)

    def test_actual_recovery_gate_requires_new_proof_after_restart(self):
        from server.recovery import RecoveryGate, RecoveryError
        from server.segment_progress import SegmentProgress
        gate = RecoveryGate(self.store.path); gate.initialize()
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=gate)
        progress.initialize()
        with self.assertRaises(RecoveryError):
            progress.advance('sim-r1', self.event)
        challenge = gate.begin('T1', 'boot-1')
        order = json.loads(self.store.get_order('T1')['payload'])
        digest = hashlib.sha256(json.dumps(order, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        gate.complete('T1', 'boot-1', challenge, robot_id='sim-r1', order_id='T1',
                      seq=1, map_id='layout@v4', stopped_at='N3',
                      order_digest=digest, stopped=True)
        result = progress.advance('sim-r1', self.event)
        restarted_gate = RecoveryGate(self.store.path); restarted_gate.initialize()
        restarted = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=restarted_gate)
        with self.assertRaises(RecoveryError):
            restarted.mark_attempted('T1', 2, 'boot-1', result['payload'], self.new)
        self.assertEqual(progress.get_order('T1', 2)['state'], 'pending')
        self.assertEqual(set(self.store.list_occupancy()), self.new)

    def test_verified_event_allows_uncertain_order_without_claiming_broker_ack(self):
        with self.store._write() as db:
            db.execute("UPDATE allocation_outbox SET state='uncertain' WHERE task_id='T1'")
        result = self.progress.advance('sim-r1', self.event)
        self.assertEqual(result['status'], 'advanced')
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertEqual(self.progress.get_order('T1', 1)['state'], 'uncertain')
        self.progress.mark_attempted('T1', 2, 'boot-1', result['payload'], self.new)
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')

    def test_gate_and_store_must_use_the_same_database(self):
        from server.segment_progress import SegmentProgress
        other = AllocationStore(Path(self.tmp.name) / 'different.sqlite3')
        other.initialize()
        mismatched = SegmentProgress(other, self.map, simulation_enabled=True, recovery_gate=self.gate)
        mismatched.initialize()
        with self.assertRaises(AllocationConflict):
            mismatched.advance('sim-r1', self.event)
        self.assertEqual(set(self.store.list_occupancy()), self.old)

    def test_missing_progression_evidence_rejects_next_order_transition(self):
        payload = self.progress.advance('sim-r1', self.event)['payload']
        with self.store._write() as db:
            db.execute("DELETE FROM progress_events WHERE task_id='T1' AND seq=1")
        with self.assertRaises(AllocationConflict):
            self.progress.mark_attempted('T1', 2, 'boot-1', payload, self.new)
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'pending')
        self.assertEqual(set(self.store.list_occupancy()), self.new)

    def test_invalidation_is_serialized_with_progress_transaction(self):
        from server.recovery import RecoveryGate, RecoveryError
        from server.segment_progress import SegmentProgress
        gate = RecoveryGate(self.store.path); gate.initialize()
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=gate)
        progress.initialize()
        challenge = gate.begin('T1', 'boot-1')
        order = json.loads(self.store.get_order('T1')['payload'])
        digest = hashlib.sha256(json.dumps(order, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        gate.complete('T1', 'boot-1', challenge, robot_id='sim-r1', order_id='T1',
                      seq=1, map_id='layout@v4', stopped_at='N3',
                      order_digest=digest, stopped=True)
        checked, proceed, invalidated = threading.Event(), threading.Event(), threading.Event()
        errors, results = [], []
        original = progress._authorize
        def pause_after_check(*args):
            original(*args)
            checked.set()
            if not proceed.wait(5):
                raise TimeoutError('test did not release transaction')
        progress._authorize = pause_after_check
        def advance():
            try: results.append(progress.advance('sim-r1', self.event))
            except Exception as error: errors.append(error)
        def invalidate():
            try: gate.invalidate('T1'); invalidated.set()
            except Exception as error: errors.append(error)
        first = threading.Thread(target=advance); second = threading.Thread(target=invalidate)
        first.start()
        self.assertTrue(checked.wait(5))
        second.start()
        self.assertFalse(invalidated.wait(.1))
        proceed.set()
        first.join(5); second.join(5)
        self.assertFalse(first.is_alive()); self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]['status'], 'advanced')
        self.assertTrue(invalidated.is_set())
        with self.assertRaises(RecoveryError):
            progress.mark_attempted('T1', 2, 'boot-1', results[0]['payload'], self.new)
        self.assertEqual(progress.get_order('T1', 2)['state'], 'pending')


if __name__ == '__main__':
    unittest.main()
