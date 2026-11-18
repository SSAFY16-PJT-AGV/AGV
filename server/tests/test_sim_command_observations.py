"""Proposed responses are simulated claims tied to the live allocation DB."""
from contextlib import closing
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server.allocation_store import AllocationStore
from server.coordinator import Coordinator
from server.field_dispatch import FieldCandidate
from server.recovery import RecoveryGate, _digest_json
from server.route_occurrence import occurrence_clearance
from server.segment_progress import SegmentProgress
from server.tests.test_coordinator import FakePublisher
from server.tests.test_occurrence_allocation_lab import laboratory_map


class SimulationCommandObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sim-command-observation-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'simulation.sqlite3'
        self.data = laboratory_map()
        self.store = AllocationStore(self.path, field_required=True)
        self.store.initialize()
        self.store.create_task('T1')
        self.gate = RecoveryGate(self.path, field_required=True)
        self.gate.initialize()
        self.progress = SegmentProgress(self.store, self.data, simulation_enabled=True,
                                        field_simulation=True, recovery_gate=self.gate)
        self.progress.initialize()
        self.journal()
        self.publisher = FakePublisher()
        self.coordinator = Coordinator(self.store, self.data, self.publisher, field_simulation=True)
        self.allocation = self.coordinator.prepare_field('T1', 'D',
            [FieldCandidate('sim-r1', 'A', 'E0', True)], {'type': 'none'})

    def journal(self):
        from server.sim_command_observations import SimCommandObservations
        result = SimCommandObservations(self.progress, {'sim-r1': {'T1': 'boot-1'}}, simulation=True)
        result.initialize()
        return result

    def response(self, *, seq=1, event_id='ack-1', outcome='accepted'):
        if seq == 1:
            record = self.store.get_order('T1')
        else:
            record = self.progress.get_order('T1', seq)
        return {'event_id': event_id, 'robot_session': 'boot-1', 'order_id': 'T1',
                'seq': seq, 'map_id': 'lab@v1', 'outcome': outcome,
                'order_digest': hashlib.sha256(record['payload'].encode('utf-8')).hexdigest()}

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT robot_id,event_id,task_id,seq,outcome '
                              'FROM sim_command_observations ORDER BY event_id').fetchall()

    def test_simulation_opt_in_and_published_claim_does_not_move_or_release(self):
        from server.sim_command_observations import SimCommandObservations
        with self.assertRaises(ValueError):
            SimCommandObservations(self.progress, {'sim-r1': {'T1': 'boot-1'}})
        journal = self.journal()
        self.coordinator.publish('T1')
        before, first = self.store.list_occupancy(), self.store.get_order('T1')
        result = journal.record('sim-r1', self.response())
        self.assertEqual(result, {'status': 'recorded', 'outcome': 'accepted'})
        self.assertEqual(self.rows(), [('sim-r1', 'ack-1', 'T1', 1, 'accepted')])
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertEqual(self.store.get_order('T1'), first)
        self.assertIsNone(self.progress.get_order('T1', 2))

    def test_response_before_broker_puback_is_recorded_as_untrusted_claim(self):
        journal = self.journal()
        initial = self.store.get_order('T1')
        occupied = self.store.list_occupancy()
        resources = [resource for resource, robot in occupied.items() if robot == 'sim-r1']
        self.store.mark_attempted('T1', initial['payload'], self.allocation, resources)
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertEqual(journal.record('sim-r1', self.response()),
                         {'status': 'recorded', 'outcome': 'accepted'})
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertEqual(self.store.list_occupancy(), occupied)

    def test_before_attempt_and_wrong_claims_do_not_change_anything(self):
        journal = self.journal()
        before = self.store.list_occupancy()
        with self.assertRaises(ValueError):
            journal.record('sim-r1', self.response())
        self.coordinator.publish('T1')
        good = self.response()
        for override in ({'robot_session': 'old'}, {'map_id': 'other@v1'},
                         {'order_digest': 'f' * 64}, {'seq': 2}, {'order_id': 'T2'}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                journal.record('sim-r1', dict(good, **override))
        for rid, retained in (('sim-r2', False), ('physical-r1', False), ('sim-r1', True)):
            with self.subTest(robot=rid, retained=retained), self.assertRaises(ValueError):
                journal.record(rid, good, retained=retained)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.list_occupancy(), before)

    def test_duplicate_is_idempotent_but_changed_or_second_response_conflicts(self):
        journal = self.journal()
        self.coordinator.publish('T1')
        first = self.response()
        journal.record('sim-r1', first)
        self.assertEqual(journal.record('sim-r1', first), {'status': 'duplicate', 'outcome': 'accepted'})
        for other in (dict(first, outcome='rejected'), self.response(event_id='ack-2', outcome='rejected')):
            with self.subTest(other=other), self.assertRaises(ValueError):
                journal.record('sim-r1', other)
        self.assertEqual(self.rows(), [('sim-r1', 'ack-1', 'T1', 1, 'accepted')])

    def test_next_sequence_requires_real_stored_current_attempt(self):
        journal = self.journal()
        self.coordinator.publish('T1')
        journal.record('sim-r1', self.response())
        initial = json.loads(self.store.get_order('T1')['payload'])
        challenge = self.gate.begin('T1', 'boot-1')
        self.gate.complete('T1', 'boot-1', challenge, robot_id='sim-r1', order_id='T1', seq=1,
                           map_id='lab@v1', stopped_at='B', order_digest=_digest_json(initial), stopped=True)
        stop = {'type': 'rear_clear', 'event_id': 'stop-1', 'robot_session': 'boot-1',
                'order_id': 'T1', 'seq': 1, 'map_id': 'lab@v1', 'stopped_at': 'B',
                'cleared_resources': [list(x) for x in occurrence_clearance(self.data, self.allocation['nodes'], 0)]}
        self.assertEqual(self.progress.advance('sim-r1', stop)['next_seq'], 2)
        response = self.response(seq=2, event_id='ack-2', outcome='rejected')
        with self.assertRaises(ValueError):
            journal.record('sim-r1', response)
        current = self.progress.get_order('T1', 2)
        resources = [(kind, key) for (kind, key), robot in self.store.list_occupancy().items()
                     if robot == 'sim-r1']
        self.progress.mark_attempted('T1', 2, 'boot-1', current['payload'], resources)
        self.assertEqual(journal.record('sim-r1', response), {'status': 'recorded', 'outcome': 'rejected'})
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')
        with self.assertRaises(ValueError):
            journal.record('sim-r1', self.response(event_id='late-old'))

    def test_failed_insert_rolls_back_without_touching_order_or_occupancy(self):
        from server.sim_command_observations import _verify_field_global
        journal = self.journal()
        self.coordinator.publish('T1')
        before, order = self.store.list_occupancy(), self.store.get_order('T1')
        calls = []
        def fail_after_insert(db, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise sqlite3.OperationalError('injected post-insert database error')
            return _verify_field_global(db, **kwargs)
        with patch('server.sim_command_observations._verify_field_global', side_effect=fail_after_insert):
            with self.assertRaises(sqlite3.OperationalError):
                journal.record('sim-r1', self.response())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertEqual(self.store.get_order('T1'), order)

    def test_changed_occupancy_or_recovery_generation_fails_closed(self):
        journal = self.journal()
        self.coordinator.publish('T1')
        before = self.store.get_order('T1')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM allocation_occupancy WHERE kind='node' AND resource_id='B'")
        with self.assertRaises(ValueError):
            journal.record('sim-r1', self.response())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.get_order('T1'), before)

    def test_stale_gate_epoch_cannot_record_response(self):
        journal = self.journal()
        self.coordinator.publish('T1')
        other_gate = RecoveryGate(self.path, field_required=True)
        other_gate.initialize()
        with self.assertRaises(ValueError):
            journal.record('sim-r1', self.response())
        self.assertEqual(self.rows(), [])

    def test_blocked_gate_cannot_record_late_response(self):
        journal = self.journal()
        self.coordinator.publish('T1')
        before = self.store.list_occupancy()
        self.gate.invalidate('T1')
        with self.assertRaises(ValueError):
            journal.record('sim-r1', self.response())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.list_occupancy(), before)

    def test_competing_observations_commit_at_most_one_claim(self):
        journal = self.journal()
        self.coordinator.publish('T1')
        before = self.store.list_occupancy()
        barrier = threading.Barrier(2)
        results, errors = [], []

        def record(eid):
            barrier.wait(timeout=5)
            try:
                results.append(journal.record('sim-r1', self.response(event_id=eid)))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record, args=(eid,)) for eid in ('ack-a', 'ack-b')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results, [{'status': 'recorded', 'outcome': 'accepted'}])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.store.list_occupancy(), before)

    def test_changed_response_table_schema_is_rejected(self):
        journal = self.journal()
        self.coordinator.publish('T1')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('ALTER TABLE sim_command_observations ADD COLUMN tampered TEXT')
        with self.assertRaises(ValueError):
            journal.record('sim-r1', self.response())

    def test_response_table_trigger_cannot_change_command_state(self):
        journal = self.journal()
        initial = self.store.get_order('T1')
        before = self.store.list_occupancy()
        resources = [item for item, robot in before.items() if robot == 'sim-r1']
        self.store.mark_attempted('T1', initial['payload'], self.allocation, resources)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER injected_mutation AFTER INSERT ON sim_command_observations "
                       "BEGIN UPDATE allocation_outbox SET state='acked' WHERE task_id='T1'; END")
        with self.assertRaises(ValueError):
            journal.record('sim-r1', self.response())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertEqual(self.store.list_occupancy(), before)

    def test_uppercase_response_table_trigger_cannot_change_command_state(self):
        journal = self.journal()
        initial = self.store.get_order('T1')
        before = self.store.list_occupancy()
        resources = [item for item, robot in before.items() if robot == 'sim-r1']
        self.store.mark_attempted('T1', initial['payload'], self.allocation, resources)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TRIGGER uppercase_mutation AFTER INSERT ON SIM_COMMAND_OBSERVATIONS '
                       "BEGIN UPDATE allocation_outbox SET state='acked' WHERE task_id='T1'; END")
        with self.assertRaises(ValueError):
            journal.record('sim-r1', self.response())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertEqual(self.store.list_occupancy(), before)

    def test_mixed_case_epoch_table_trigger_is_rejected(self):
        journal = self.journal()
        self.coordinator.publish('T1')
        before = self.store.list_occupancy()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TRIGGER mixed_epoch AFTER UPDATE ON SiM_CoMmAnD_ObSeRvAtIoN_MeTa '
                       "BEGIN UPDATE allocation_outbox SET state='uncertain' WHERE task_id='T1'; END")
        with self.assertRaises(ValueError):
            journal.record('sim-r1', self.response())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.get_order('T1')['state'], 'acked')
        self.assertEqual(self.store.list_occupancy(), before)

    def test_new_gate_cannot_bind_to_existing_attempted_allocation(self):
        from server.sim_command_observations import SimCommandObservations
        self.journal()
        self.coordinator.publish('T1')
        before = self.store.list_occupancy()
        new_gate = RecoveryGate(self.path, field_required=True)
        new_gate.initialize()
        new_progress = SegmentProgress(self.store, self.data, simulation_enabled=True,
                                       field_simulation=True, recovery_gate=new_gate)
        new_progress.initialize()
        fresh = SimCommandObservations(new_progress, {'sim-r1': {'T1': 'boot-1'}}, simulation=True)
        with self.assertRaises(ValueError):
            fresh.initialize()
            fresh.record('sim-r1', self.response())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.list_occupancy(), before)

    def test_first_initialization_rejects_already_allocated_db(self):
        from server.sim_command_observations import SimCommandObservations
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE sim_command_observations')
            db.execute('DROP TABLE sim_command_observation_meta')
        self.coordinator.publish('T1')
        observer = SimCommandObservations(self.progress, {'sim-r1': {'T1': 'boot-1'}}, simulation=True)
        with self.assertRaises(ValueError):
            observer.initialize()

    def test_handoff_store_file_replacement_is_rejected(self):
        from server.route_occurrence import plan_occurrence_segment
        from server.sim_command_observations import SimCommandObservations
        from server.sim_handoff_store import SimHandoffStore
        path = Path(self.tmp.name) / 'handoff.sqlite3'
        starts = {'sim-r1': 'A', 'sim-r2': 'X'}
        store = SimHandoffStore.create_new(path, simulation_enabled=True, idle_positions=starts)
        store._field_required = True
        gate = RecoveryGate(path, field_required=True)
        gate.initialize()
        progress = SegmentProgress(store, self.data, simulation_enabled=True,
                                   field_simulation=True, recovery_gate=gate)
        progress.initialize()
        observer = SimCommandObservations(progress, {'sim-r1': {'T1': 'boot-1'}}, simulation=True)
        observer.initialize()
        store.create_task('T1')
        nodes = ['A', 'B', 'C', 'B', 'D']
        segment = plan_occurrence_segment(self.data, nodes, 0)
        fingerprint = hashlib.sha256(json.dumps(self.data, ensure_ascii=True, sort_keys=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        allocation = store.allocate('T1', 'sim-r1', 'lab@v1', nodes, segment.released_until,
            segment.resources, {'type': 'none'}, map_fingerprint=fingerprint, release_index=segment.end_index,
            field_context={'map_data': self.data, 'incoming_edge': 'E0', 'goal': 'D'})
        record = store.get_order('T1')
        store.mark_attempted('T1', record['payload'], allocation, segment.resources)
        response = {'event_id': 'handoff-ack', 'robot_session': 'boot-1', 'order_id': 'T1',
                    'seq': 1, 'map_id': 'lab@v1', 'outcome': 'accepted',
                    'order_digest': hashlib.sha256(record['payload'].encode('utf-8')).hexdigest()}
        self.assertEqual(observer.record('sim-r1', response), {'status': 'recorded', 'outcome': 'accepted'})
        original, copy = path.with_suffix('.original'), path.with_suffix('.copy')
        shutil.copyfile(path, copy)
        path.replace(original)
        copy.replace(path)
        with self.assertRaisesRegex(ValueError, 'file was replaced'):
            observer.record('sim-r1', response)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_command_observations').fetchone(), (1,))

    def test_nested_recovery_transaction_fails_immediately(self):
        journal = self.journal()
        self.coordinator.publish('T1')
        response = self.response()
        with self.gate.transaction():
            started = time.monotonic()
            with self.assertRaises(ValueError):
                journal.record('sim-r1', response)
            self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.rows(), [])

@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class RuntimeObservationRestartTests(unittest.TestCase):
    def test_handoff_runtime_restart_cannot_record_lost_old_response(self):
        from server.sim_command_observations import SimCommandObservations
        from server.sim_handoff_store import SimHandoffStore
        from server.sim_runtime import SimRuntime
        from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
        from server.tools.twin_multi_test import isolated_environment

        port, data = free_port(), laboratory_map()
        env = isolated_environment(os.environ)
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='sim-observation-restart-') as folder, ExitStack() as cleanup, \
                open(os.devnull, 'w') as log:
            ready = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                                       BROKER, str(port), ready], env=env, stdout=log, stderr=log,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready)
            path = Path(folder) / 'simulation.sqlite3'
            starts = {'sim-r1': 'A'}
            sessions = {'sim-r1': {'T1': 'boot-1'}}
            store = SimHandoffStore.create_new(path, simulation_enabled=True, idle_positions=starts)
            runtime = SimRuntime(path, data, sessions, port=port, client_id='obs-before-restart',
                                 simulation_store=store, field_simulation=True, field_initial_positions=starts)
            with patch.dict(os.environ, isolated_environment(os.environ), clear=True):
                runtime.start()
                cleanup.callback(runtime.stop)
                observer = SimCommandObservations(runtime.progress, sessions, simulation=True)
                observer.initialize()
                runtime.create_task('T1')
                runtime.prepare_field('T1', 'D', [{'robot_id': 'sim-r1', 'node': 'A',
                    'incoming_edge': 'E0', 'available': True}], {'type': 'none'})
                runtime.publish('T1')
                order = store.get_order('T1')
                before = store.list_occupancy()
                response = {'event_id': 'late-after-restart', 'robot_session': 'boot-1',
                            'order_id': 'T1', 'seq': 1, 'map_id': 'lab@v1', 'outcome': 'accepted',
                            'order_digest': hashlib.sha256(order['payload'].encode('utf-8')).hexdigest()}
                runtime.stop()
                reopened = SimHandoffStore.open_existing(path, expected_creation_id=store.creation_id,
                                                        expected_idle_enabled=True, simulation_enabled=True)
                restarted = SimRuntime(path, data, sessions, port=port, client_id='obs-after-restart',
                                       simulation_store=reopened, field_simulation=True,
                                       field_initial_positions=starts)
                restarted.start()
                cleanup.callback(restarted.stop)
                self.assertIn('T1', restarted.gate._blocked)
                new_observer = SimCommandObservations(restarted.progress, sessions, simulation=True)
                with self.assertRaisesRegex(ValueError, 'epoch differs'):
                    new_observer.initialize()
                with self.assertRaisesRegex(ValueError, 'epoch differs'):
                    new_observer.record('sim-r1', response)
                self.assertEqual(reopened.list_occupancy(), before)
                self.assertEqual(reopened.get_order('T1'), order)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_command_observations').fetchone(), (0,))


if __name__ == '__main__':
    unittest.main()
