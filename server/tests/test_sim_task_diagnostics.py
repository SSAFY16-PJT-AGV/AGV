"""Bounded diagnostics use real simulation storage and do not grant motion."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from server.allocation_store import AllocationStore
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress
from server.sim_api import router
from server.sim_runtime import SimRuntime
from server.tests.test_segment_recovery_mqtt import two_segment_map
from server.tests.test_sim_api import request
from sim.event_inbox import SimEventInbox


class SimulationTaskDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='sim-diagnostics-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'simulation.sqlite3'
        self.store = AllocationStore(self.path)
        self.store.initialize()
        for task, robot, nodes in (('T1', 'sim-r1', ['N1', 'N2']),
                                   ('T2', 'sim-r2', ['N3', 'N4'])):
            self.store.create_task(task)
            self.store.allocate(task, robot, 'integration@v1', nodes, nodes[1],
                                [('node', nodes[0]), ('node', nodes[1]),
                                 ('edge', json.dumps(nodes, separators=(',', ':')))],
                                {'type': 'load', 'secret': 'private-payload'},
                                map_fingerprint='a' * 64)
        allocation = self.store.get_allocation('T1')
        payload = self.store.get_order('T1')['payload']
        self.store.mark_attempted('T1', payload, allocation,
                                  [('node', 'N1'), ('node', 'N2'), ('edge', '["N1","N2"]')])
        SegmentProgress(self.store, two_segment_map(), simulation_enabled=True).initialize()
        inbox = SimEventInbox(self.path, robot_tasks={'sim-r1': ['T1'], 'sim-r2': ['T2']},
                              simulation_enabled=True)
        inbox.initialize()
        for rid, task, session, eid in [('sim-r1', 'T1', 'boot-1', 'event-1'),
                                        ('sim-r2', 'T2', 'boot-2', 'other-event-secret')]:
            inbox.store(rid, {'type': 'rear_clear', 'event_id': eid, 'robot_session': session,
                              'order_id': task, 'seq': 1, 'map_id': 'integration@v1',
                              'stopped_at': 'N2' if task == 'T1' else 'N4', 'cleared_resources': []})
        self.gate = RecoveryGate(self.path)
        self.gate.initialize()
        self.gate.invalidate_many(('T1',))
        self.runtime = SimRuntime(self.path, two_segment_map(),
                                  {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2', 'T3': 'boot-3'}},
                                  port=18883, client_id='diagnostic-test')
        self.runtime._started = True
        self.runtime.receiver = SimpleNamespace(ready=False)
        self.runtime.gate = self.gate

    def test_disconnected_uncertain_task_reports_only_its_own_state(self):
        before_occupancy = self.store.list_occupancy()
        result = self.runtime.task_diagnostics('T1')
        self.assertEqual(result['scope'], 'isolated_simulation')
        snapshot = result['database_snapshot']
        self.assertEqual((snapshot['task_id'], snapshot['status'], snapshot['robot_id']),
                         ('T1', 'ASSIGNED', 'sim-r1'))
        self.assertEqual(snapshot['orders'], [{'seq': 1, 'state': 'uncertain'}])
        self.assertEqual(snapshot['occupancy'], [['edge', '["N1","N2"]'], ['node', 'N1'], ['node', 'N2']])
        self.assertEqual(snapshot['pending_events'], [{'event_id': 'event-1', 'seq': 1}])
        self.assertEqual(snapshot['pending_events_integrity'], 'not_checked')
        self.assertFalse(snapshot['pending_events_truncated'])
        self.assertEqual(result['recovery_blocked_observation'], True)
        self.assertEqual(result['mqtt_ready_observation'], False)
        self.assertNotIn('private-payload', repr(result))
        self.assertNotIn('other-event-secret', repr(result))
        self.assertNotIn('boot-1', repr(result))
        self.assertEqual(self.store.list_occupancy(), before_occupancy)
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')

    def test_uninstalled_command_observations_are_distinct_from_empty_installed_table(self):
        snapshot = self.runtime.task_diagnostics('T1')['database_snapshot']
        self.assertEqual(snapshot['command_response_observations'], {
            'availability': 'not_installed', 'records': [], 'truncated': False,
            'integrity': 'not_checked', 'kind': 'unverified_simulation_claim'})
        self.install_observations()
        snapshot = self.runtime.task_diagnostics('T1')['database_snapshot']
        self.assertEqual(snapshot['command_response_observations']['availability'], 'available')
        self.assertEqual(snapshot['command_response_observations']['records'], [])

    def test_enabled_observations_with_both_tables_missing_fail_closed(self):
        self.runtime._command_observations_enabled = True
        with self.assertRaises(RuntimeError):
            self.runtime.task_diagnostics('T1')

    def install_observations(self):
        from server.sim_command_observations import _CREATE, _CREATE_META
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(_CREATE)
            db.execute(_CREATE_META)
            db.execute('INSERT INTO sim_command_observation_meta VALUES (1,?)', (self.gate._epoch,))

    def insert_claim(self, *, robot='sim-r1', task='T1', event='claim-1', seq=1,
                     outcome='accepted', epoch=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO sim_command_observations VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (robot, event, task, seq, 'sensitive-session', 'integration@v1',
                        'sensitive-digest', outcome, self.gate._epoch if epoch is None else epoch,
                        'sensitive-response-body'))

    def test_claim_metadata_is_task_scoped_unverified_and_never_contains_payload(self):
        self.install_observations()
        self.insert_claim()
        self.insert_claim(robot='sim-r2', task='T2', event='other-claim-secret')
        self.insert_claim(robot='sim-r2', task='T1', event='wrong-robot-secret', seq=2)
        result = self.runtime.task_diagnostics('T1')['database_snapshot']['command_response_observations']
        self.assertEqual(result, {'availability': 'available', 'integrity': 'not_checked',
            'kind': 'unverified_simulation_claim', 'truncated': False,
            'records': [{'event_id': 'claim-1', 'seq': 1, 'claimed_outcome': 'accepted',
                         'epoch_relation': 'same_at_db_snapshot'}]})
        for secret in ('sensitive-session', 'sensitive-digest', 'sensitive-response-body',
                       'other-claim-secret', 'wrong-robot-secret', self.gate._epoch):
            self.assertNotIn(secret, repr(result))

    def test_persisted_claim_epoch_differs_after_new_recovery_epoch(self):
        self.install_observations()
        self.insert_claim()
        RecoveryGate(self.path).initialize()
        result = self.runtime.task_diagnostics('T1')['database_snapshot']['command_response_observations']
        self.assertEqual(result['records'][0]['epoch_relation'], 'different_at_db_snapshot')
        self.assertEqual(result['kind'], 'unverified_simulation_claim')

    def test_corrupt_claim_metadata_is_bounded_and_never_promoted_to_verified(self):
        self.install_observations()
        self.insert_claim(event='나' * 86, seq=1, outcome='private-invalid-outcome' * 50,
                          epoch='not-an-epoch')
        result = self.runtime.task_diagnostics('T1')['database_snapshot']['command_response_observations']
        self.assertEqual(result['integrity'], 'not_checked')
        self.assertEqual(result['records'], [{'event_id': None, 'event_id_omitted': True,
            'seq': 1, 'claimed_outcome': None, 'outcome_unrecognized': True,
            'epoch_relation': 'unknown'}])
        self.assertNotIn('private-invalid-outcome', repr(result))

    def test_partial_or_changed_observation_schema_fails_closed(self):
        from server.sim_command_observations import _CREATE
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(_CREATE)
        with self.assertRaises(RuntimeError):
            self.runtime.task_diagnostics('T1')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE sim_command_observations')
        self.install_observations()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('ALTER TABLE sim_command_observations ADD COLUMN wrong TEXT')
        with self.assertRaises(RuntimeError):
            self.runtime.task_diagnostics('T1')

    def test_missing_or_malformed_observation_epoch_fails_closed(self):
        self.install_observations()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM sim_command_observation_meta')
        with self.assertRaises(RuntimeError):
            self.runtime.task_diagnostics('T1')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO sim_command_observation_meta VALUES (1,'bad')")
        with self.assertRaises(RuntimeError):
            self.runtime.task_diagnostics('T1')

    def test_nul_in_observation_meta_epoch_fails_closed(self):
        self.install_observations()
        malformed = 'a' * 32 + '\x00' + 'Z' * 31
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE sim_command_observation_meta SET epoch=?', (malformed,))
        with self.assertRaises(RuntimeError):
            self.runtime.task_diagnostics('T1')

    def test_nul_in_claim_or_recovery_epoch_reports_unknown(self):
        self.install_observations()
        malformed = 'a' * 32 + '\x00' + 'Z' * 31
        self.insert_claim(epoch=malformed)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE recovery_runtime SET epoch=?', (malformed,))
        result = self.runtime.task_diagnostics('T1')['database_snapshot']['command_response_observations']
        self.assertEqual(result['records'][0]['epoch_relation'], 'unknown')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE sim_command_observations SET epoch=?', (self.gate._epoch,))
        result = self.runtime.task_diagnostics('T1')['database_snapshot']['command_response_observations']
        self.assertEqual(result['records'][0]['epoch_relation'], 'unknown')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE sim_command_observations SET epoch=?', (malformed,))
            db.execute('UPDATE recovery_runtime SET epoch=?', (self.gate._epoch,))
        result = self.runtime.task_diagnostics('T1')['database_snapshot']['command_response_observations']
        self.assertEqual(result['records'][0]['epoch_relation'], 'unknown')

    def test_claim_output_count_utf8_and_sequence_limits(self):
        self.install_observations()
        rows = []
        for seq in range(1, 103):
            event_id = ('가' * 85 if seq == 1 else '나' * 86 if seq == 2 else
                        'a' * 256 if seq == 3 else 'b' * 257 if seq == 4 else 'event-%03d' % seq)
            rows.append(('sim-r1', event_id, 'T1', seq, 'sensitive-session', 'integration@v1',
                         'sensitive-digest', 'accepted', self.gate._epoch, 'sensitive-response-body'))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany('INSERT INTO sim_command_observations VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
        result = self.runtime.task_diagnostics('T1')['database_snapshot']['command_response_observations']
        self.assertEqual(len(result['records']), 100)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['records'][0]['event_id'], '가' * 85)
        self.assertEqual(result['records'][1]['event_id'], None)
        self.assertTrue(result['records'][1]['event_id_omitted'])
        self.assertEqual(result['records'][2]['event_id'], 'a' * 256)
        self.assertIsNone(result['records'][3]['event_id'])
        self.assertTrue(result['records'][3]['event_id_omitted'])
        self.assertNotIn('나' * 86, repr(result))
        self.assertNotIn('b' * 257, repr(result))

    def test_malformed_claim_sequence_and_trigger_are_not_exposed_or_trusted(self):
        self.install_observations()
        self.insert_claim(seq='secret-sequence-' * 50)
        result = self.runtime.task_diagnostics('T1')['database_snapshot']['command_response_observations']
        self.assertIsNone(result['records'][0]['seq'])
        self.assertTrue(result['records'][0]['seq_omitted'])
        self.assertNotIn('secret-sequence', repr(result))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TRIGGER evil AFTER INSERT ON SIM_COMMAND_OBSERVATIONS BEGIN SELECT 1; END')
        with self.assertRaises(RuntimeError):
            self.runtime.task_diagnostics('T1')

    def test_authenticated_claim_api_fails_closed_on_partial_schema(self):
        self.install_observations()
        self.insert_claim()
        app = FastAPI()
        app.include_router(router)
        app.state.sim_runtime = self.runtime
        app.state.sim_api_token = 'secret-token'
        def get(token='Bearer secret-token'):
            return asyncio.run(request(app, 'GET', '/simulation/tasks/T1/diagnostics', token=token))
        self.assertEqual(get(token=None)[0], 401)
        code, result, headers = get()
        self.assertEqual(code, 200)
        self.assertEqual(headers[b'cache-control'], b'no-store')
        self.assertEqual(result['database_snapshot']['command_response_observations']['records'][0]
                         ['claimed_outcome'], 'accepted')
        self.assertNotIn('sensitive-session', repr(result))
        self.assertNotIn('sensitive-digest', repr(result))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE sim_command_observation_meta')
        code, result, headers = get()
        self.assertEqual((code, result), (503, {'detail': 'simulation unavailable'}))
        self.assertEqual(headers[b'cache-control'], b'no-store')

    def test_claims_and_order_are_read_from_the_same_snapshot(self):
        self.install_observations()
        self.insert_claim()
        original_connect = sqlite3.connect
        with closing(original_connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower(), 'wal')
        started, first_read, committed = threading.Event(), threading.Event(), threading.Event()
        failures = []
        def writer():
            try:
                with closing(original_connect(self.path, timeout=10)) as db:
                    started.set()
                    if not first_read.wait(5):
                        raise RuntimeError('diagnostic did not reach its first read')
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE allocation_outbox SET state='acked' WHERE task_id='T1'")
                    db.execute("UPDATE sim_command_observations SET outcome='rejected' WHERE task_id='T1'")
                    db.commit()
                    committed.set()
            except Exception as exc:
                failures.append(exc)
                committed.set()

        class PausingConnection(sqlite3.Connection):
            def execute(self, statement, parameters=()):
                cursor = super().execute(statement, parameters)
                if 'FROM tasks WHERE task_id=?' in statement:
                    first_read.set()
                    if not committed.wait(5):
                        raise RuntimeError('test writer did not commit during the read')
                    if failures:
                        raise RuntimeError('test writer failed') from failures[0]
                return cursor

        def diagnostic_connection(*args, **kwargs):
            kwargs['factory'] = PausingConnection
            return original_connect(*args, **kwargs)

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(started.wait(5))
        try:
            with patch('server.sim_runtime.sqlite3.connect', side_effect=diagnostic_connection):
                snapshot = self.runtime.task_diagnostics('T1')['database_snapshot']
        finally:
            thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(first_read.is_set() and committed.is_set())
        self.assertEqual(snapshot['orders'][0]['state'], 'uncertain')
        self.assertEqual(snapshot['command_response_observations']['records'][0]['claimed_outcome'], 'accepted')
        with closing(original_connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state FROM allocation_outbox WHERE task_id='T1'").fetchone(), ('acked',))
            self.assertEqual(db.execute("SELECT outcome FROM sim_command_observations WHERE task_id='T1'").fetchone(),
                             ('rejected',))

    def test_missing_task_and_stopped_runtime_fail_closed(self):
        for name in ('not-configured', 'T3'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.runtime.task_diagnostics(name)
        self.runtime._stopped = True
        with self.assertRaises(RuntimeError):
            self.runtime.task_diagnostics('T1')

    def test_unknown_gate_or_receiver_are_not_reported_ready(self):
        self.runtime.gate = None
        self.runtime.receiver = None
        result = self.runtime.task_diagnostics('T1')
        self.assertIsNone(result['recovery_blocked_observation'])
        self.assertIsNone(result['mqtt_ready_observation'])

    def test_pending_event_list_is_bounded_and_ignores_another_robot(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany('INSERT INTO sim_event_inbox '
                           '(robot_id,event_id,task_id,robot_session,seq,payload,digest,state) '
                           "VALUES(?,?,?,?,?,?,?,'pending')",
                           [('sim-r1', 'bulk-%03d' % n, 'T1', 'boot-1', 1, '{}', 'test')
                            for n in range(101)] +
                           [('sim-r2', 'wrong-robot-secret', 'T1', 'boot-2', 1, '{}', 'test')])
        snapshot = self.runtime.task_diagnostics('T1')['database_snapshot']
        self.assertEqual(len(snapshot['pending_events']), 100)
        self.assertTrue(snapshot['pending_events_truncated'])
        self.assertNotIn('wrong-robot-secret', repr(snapshot))

    def test_event_ids_are_bounded_by_utf8_bytes_without_modification(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany('INSERT INTO sim_event_inbox '
                           '(robot_id,event_id,task_id,robot_session,seq,payload,digest,state) '
                           "VALUES(?,?,?,?,?,?,?,'pending')", [
                               ('sim-r1', 'a' * 256, 'T1', 'boot-1', 2, '{}', 'bad'),
                               ('sim-r1', 'b' * 257, 'T1', 'boot-1', 3, '{}', 'bad'),
                               ('sim-r1', '가' * 85, 'T1', 'boot-1', 4, '{}', 'bad'),
                               ('sim-r1', '나' * 86, 'T1', 'boot-1', 5, '{}', 'bad'),
                               ('sim-r2', 'other-event-secret-2', 'T2', 'boot-2', 6, '{}', 'bad'),
                           ])
        snapshot = self.runtime.task_diagnostics('T1')['database_snapshot']
        by_sequence = {row['seq']: row for row in snapshot['pending_events']}
        self.assertEqual(by_sequence[2], {'event_id': 'a' * 256, 'seq': 2})
        self.assertEqual(by_sequence[3], {'event_id': None, 'event_id_omitted': True, 'seq': 3})
        self.assertEqual(by_sequence[4], {'event_id': '가' * 85, 'seq': 4})
        self.assertEqual(by_sequence[5], {'event_id': None, 'event_id_omitted': True, 'seq': 5})
        self.assertNotIn(6, by_sequence)
        self.assertNotIn('b' * 257, repr(snapshot))
        self.assertNotIn('나' * 86, repr(snapshot))

    def test_corrupt_event_metadata_is_explicitly_unverified(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE sim_event_inbox SET payload='{}', digest='invalid' "
                       "WHERE robot_id='sim-r1' AND event_id='event-1'")
        snapshot = self.runtime.task_diagnostics('T1')['database_snapshot']
        self.assertEqual(snapshot['pending_events'], [{'event_id': 'event-1', 'seq': 1}])
        self.assertEqual(snapshot['pending_events_integrity'], 'not_checked')

    def test_invalid_stored_sequence_is_omitted_not_echoed(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE sim_event_inbox SET seq=? WHERE robot_id='sim-r1' AND event_id='event-1'",
                       ('oversized-sequence-' * 200,))
        snapshot = self.runtime.task_diagnostics('T1')['database_snapshot']
        self.assertEqual(snapshot['pending_events'],
                         [{'event_id': 'event-1', 'seq': None, 'seq_omitted': True}])

    def test_occupancy_ids_preserve_256_utf8_bytes_and_fail_if_larger(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany('INSERT INTO allocation_occupancy(kind,resource_id,task_id) VALUES(?,?,?)',
                           [('zone', 'a' * 256, 'T1'), ('zone', '가' * 85, 'T1')])
        snapshot = self.runtime.task_diagnostics('T1')['database_snapshot']
        self.assertIn(['zone', 'a' * 256], snapshot['occupancy'])
        self.assertIn(['zone', '가' * 85], snapshot['occupancy'])
        for invalid in ('b' * 257, '나' * 86):
            with self.subTest(invalid_length=len(invalid)):
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("UPDATE allocation_occupancy SET resource_id=? "
                               "WHERE kind='zone' AND resource_id=?", (invalid, 'a' * 256))
                with self.assertRaises(RuntimeError):
                    self.runtime.task_diagnostics('T1')
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("UPDATE allocation_occupancy SET resource_id=? "
                               "WHERE kind='zone' AND resource_id=?", ('a' * 256, invalid))

    def test_oversized_database_fields_cannot_inflate_diagnostic_response(self):
        scenarios = [('tasks', 'status', 'ASSIGNED'), ('allocation_jobs', 'robot_id', 'sim-r1'),
                     ('allocation_jobs', 'map_id', 'integration@v1'),
                     ('allocation_jobs', 'released_until', 'N2'),
                     ('allocation_outbox', 'state', 'uncertain')]
        for table, column, original in scenarios:
            with self.subTest(table=table, column=column):
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE task_id=?',
                               ('x' * 257, 'T1'))
                with self.assertRaises(RuntimeError):
                    self.runtime.task_diagnostics('T1')
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE task_id=?',
                               (original, 'T1'))

    def test_oversized_later_order_metadata_and_resource_kind_fail_closed(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO progress_orders(task_id,seq,payload,state) VALUES('T1',2,'{}',?)",
                       ('y' * 257,))
        with self.assertRaises(RuntimeError):
            self.runtime.task_diagnostics('T1')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM progress_orders WHERE task_id='T1' AND seq=2")
            db.execute("INSERT INTO allocation_occupancy(kind,resource_id,task_id) VALUES(?, 'bad', 'T1')",
                       ('x' * 257,))
        with self.assertRaises(RuntimeError):
            self.runtime.task_diagnostics('T1')

    def test_oversized_configured_task_id_is_not_reflected(self):
        huge_task = 't' * 257
        self.runtime._tasks[huge_task] = ('sim-r1', 'boot-1')
        self.store.create_task(huge_task)
        with self.assertRaises(ValueError):
            self.runtime.task_diagnostics(huge_task)

    def test_database_reads_remain_one_snapshot_under_concurrent_commits(self):
        first_read, committed = threading.Event(), threading.Event()
        failures = []
        original_connect = sqlite3.connect
        with closing(original_connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower(), 'wal')

        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                cursor = super().execute(sql, *args)
                if 'FROM tasks WHERE task_id=' in sql:
                    first_read.set()
                    if not committed.wait(5):
                        raise RuntimeError('writer did not commit during diagnostic snapshot')
                return cursor

        def connect(*args, **kwargs):
            if threading.current_thread() is threading.main_thread():
                kwargs['factory'] = ObservedConnection
            return original_connect(*args, **kwargs)

        def writer():
            try:
                if not first_read.wait(5):
                    raise RuntimeError('diagnostic did not read task')
                with closing(original_connect(self.path, timeout=5)) as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE tasks SET status='PENDING' WHERE task_id='T1'")
                    db.execute("UPDATE allocation_outbox SET state='pending' WHERE task_id='T1'")
                    db.commit()
            except Exception as exc:
                failures.append(exc)
            finally:
                committed.set()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with patch('server.sim_runtime.sqlite3.connect', side_effect=connect):
                snapshot = self.runtime.task_diagnostics('T1')['database_snapshot']
        finally:
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual((snapshot['status'], snapshot['orders'][0]['state']), ('ASSIGNED', 'uncertain'))
        with closing(original_connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT status FROM tasks WHERE task_id=?', ('T1',)).fetchone(), ('PENDING',))
            self.assertEqual(db.execute('SELECT state FROM allocation_outbox WHERE task_id=?', ('T1',)).fetchone(),
                             ('pending',))

    def test_busy_database_diagnostic_returns_generic_503_and_releases_pin(self):
        app = FastAPI()
        app.include_router(router)
        app.state.sim_runtime = self.runtime
        app.state.sim_api_token = 'secret-token'
        original_connect = sqlite3.connect
        def no_wait(*args, **kwargs):
            kwargs['timeout'] = 0
            return original_connect(*args, **kwargs)
        with closing(original_connect(self.path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            try:
                with patch('server.sim_runtime.sqlite3.connect', side_effect=no_wait):
                    code, body, headers = asyncio.run(request(
                        app, 'GET', '/simulation/tasks/T1/diagnostics', token='Bearer secret-token'))
                self.assertEqual((code, body), (503, {'detail': 'simulation unavailable'}))
                self.assertEqual(headers[b'cache-control'], b'no-store')
                self.assertEqual(self.runtime._reference_operations, 0)
            finally:
                blocker.execute('ROLLBACK')

    def test_api_requires_loopback_and_token_and_hides_errors(self):
        app = FastAPI()
        app.include_router(router)
        app.state.sim_runtime = self.runtime
        app.state.sim_api_token = 'secret-token'

        def get(task, *, token='Bearer secret-token', host='127.0.0.1'):
            return asyncio.run(request(app, 'GET', '/simulation/tasks/' + task + '/diagnostics',
                                       token=token, host=host))

        self.assertEqual(get('T1', token=None)[0], 401)
        self.assertEqual(get('T1', host='192.0.2.5')[0], 403)
        code, body, headers = get('T1')
        self.assertEqual(code, 200)
        self.assertEqual(body['database_snapshot']['orders'][0]['state'], 'uncertain')
        self.assertEqual(headers[b'cache-control'], b'no-store')
        self.assertEqual(get('T3')[0], 409)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE sim_event_inbox')
        code, body, headers = get('T1')
        self.assertEqual((code, body), (503, {'detail': 'simulation unavailable'}))
        self.assertEqual(headers[b'cache-control'], b'no-store')


if __name__ == '__main__':
    unittest.main()
