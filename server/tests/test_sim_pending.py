import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.sim_runtime import SimRuntime
from server.tests.test_segment_recovery_mqtt import two_segment_map


class PendingEventsTest(unittest.TestCase):
    def test_status_reports_last_fixed_workflow_error_as_historical_information(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = SimRuntime(Path(folder) / 'sim.sqlite3', two_segment_map(),
                                 {'sim-r1': {'T1': 'boot-1'}}, port=18883, client_id='test')
            runtime._started = True
            runtime.receiver = SimpleNamespace(ready=True)
            runtime.workflow = SimpleNamespace(last_error='recovery proof rejected')
            self.assertEqual(runtime.status()['last_workflow_error'], 'recovery proof rejected')

    def test_task_status_exposes_order_state_and_reserved_resources(self):
        from server.allocation_store import AllocationStore
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'simulation.sqlite3'
            store = AllocationStore(path)
            store.initialize()
            store.create_task('T1')
            store.allocate('T1', 'sim-r1', 'integration@v1', ['N1', 'N2'], 'N2',
                           [('node', 'N1'), ('node', 'N2'), ('edge', '["N1","N2"]')],
                           {'type': 'load'}, map_fingerprint='a' * 64)
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE progress_orders (task_id TEXT, seq INTEGER, state TEXT)')
                db.commit()
            runtime = SimRuntime(path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                                 port=18883, client_id='test-runtime')
            runtime._started = True
            status = runtime.task_status('T1')
            self.assertEqual(status['status'], 'ASSIGNED')
            self.assertEqual(status['orders'], [{'seq': 1, 'state': 'pending'}])
            self.assertEqual(status['robot_id'], 'sim-r1')
            self.assertEqual(len(status['occupancy']), 3)
            with self.assertRaises(ValueError):
                runtime.task_status('other-task')

    def test_lists_only_mapped_pending_event_metadata_without_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'simulation.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE sim_event_inbox (robot_id TEXT,event_id TEXT,task_id TEXT,seq INTEGER,state TEXT,payload TEXT)')
                db.executemany('INSERT INTO sim_event_inbox VALUES (?,?,?,?,?,?)', [
                    ('sim-r1', 'e1', 'T1', 1, 'pending', 'do-not-expose'),
                    ('sim-r1', 'e2', 'T1', 1, 'applied', 'do-not-expose'),
                    ('sim-r1', 'e3', 'other-task', 1, 'pending', 'do-not-expose'),
                ])
            runtime = SimRuntime(path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                                 port=18883, client_id='test-runtime')
            runtime._started = True
            runtime.receiver = SimpleNamespace(ready=False)
            result = runtime.pending_events()
            self.assertEqual(result, {'events': [{'robot_id': 'sim-r1', 'event_id': 'e1',
                                                  'task_id': 'T1', 'seq': 1}], 'truncated': False})


if __name__ == '__main__':
    unittest.main()
