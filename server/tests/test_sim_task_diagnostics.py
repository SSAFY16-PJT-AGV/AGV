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
        started = threading.Event()
        failures = []

        def writer():
            try:
                with closing(sqlite3.connect(self.path, timeout=10)) as db:
                    started.set()
                    for i in range(60):
                        db.execute('BEGIN IMMEDIATE')
                        status, state = ('ASSIGNED', 'uncertain') if i % 2 else ('PENDING', 'pending')
                        db.execute('UPDATE tasks SET status=? WHERE task_id=?', (status, 'T1'))
                        db.execute('UPDATE allocation_outbox SET state=? WHERE task_id=?', (state, 'T1'))
                        db.commit()
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(started.wait(5))
        try:
            for _ in range(60):
                snapshot = self.runtime.task_diagnostics('T1')['database_snapshot']
                self.assertIn((snapshot['status'], snapshot['orders'][0]['state']),
                              {('ASSIGNED', 'uncertain'), ('PENDING', 'pending')})
        finally:
            thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])

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
