import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from types import SimpleNamespace

from server.segments import plan_segment
from server.sim_handoff_store import SimHandoffStore
from server.sim_runtime import SimRuntime
from server.tests.test_segment_progress import map_data
from server.tests import test_sim_handoff_store
from sim.order_inbox import SimOrderInbox
from sim.order_executor import SimOrderExecutor


class RestartStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'simulation.sqlite3'
        self.store = SimHandoffStore.create_new(self.path, simulation_enabled=True,
                                                 idle_positions={'sim-r1': 'N1', 'sim-r2': 'N5'})
        self.map = map_data()
        fingerprint = hashlib.sha256(json.dumps(self.map, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.store.create_task('T1')
        route = ['N1', 'N2', 'N3']
        segment = plan_segment(self.map, route)
        self.store.allocate('T1', 'sim-r1', 'layout@v4', route, segment.released_until,
                            segment.resources, {'type': 'load'}, map_fingerprint=fingerprint)

    def reopen(self, **overrides):
        args = dict(expected_creation_id=self.store.creation_id,
                    expected_idle_enabled=True, simulation_enabled=True)
        args.update(overrides)
        return SimHandoffStore.open_existing(self.path, **args)

    def test_reopen_preserves_occupancy_and_requires_external_identity(self):
        reopened = self.reopen()
        self.assertTrue(reopened.reopened)
        self.assertEqual(reopened.creation_id, self.store.creation_id)
        self.assertEqual(reopened.list_occupancy(), self.store.list_occupancy())
        with self.assertRaises(ValueError):
            self.reopen(expected_creation_id='0' * 64)
        with self.assertRaises(ValueError):
            self.reopen(expected_idle_enabled=False)
        with self.assertRaises(ValueError):
            self.reopen(simulation_enabled=False)

    def test_missing_file_never_created(self):
        missing = self.path.parent / 'missing.sqlite3'
        with self.assertRaises(ValueError):
            SimHandoffStore.open_existing(missing, expected_creation_id=self.store.creation_id,
                                         expected_idle_enabled=True, simulation_enabled=True)
        self.assertFalse(missing.exists())

    def test_missing_or_replaced_trigger_rejected(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TRIGGER sim_preserve_active_delete')
            db.execute('CREATE TRIGGER sim_preserve_active_delete BEFORE DELETE ON sim_active_robots BEGIN SELECT 1; END')
            db.commit()
        with self.assertRaises(ValueError):
            self.reopen()

    def test_reopened_runtime_checks_map_and_task_mapping_before_connect(self):
        reopened = self.reopen()
        different_map = map_data()
        different_map['nodes'][0]['pos'][0] += 1
        runtime = SimRuntime(self.path, different_map, {'sim-r1': {'T1': 'session1'}},
                             port=1884, client_id='restart-test', simulation_store=reopened)
        with patch.object(runtime, '_new_client') as client:
            with self.assertRaises(ValueError):
                runtime.start()
            client.assert_not_called()

        self.assertEqual(reopened.list_occupancy(), self.store.list_occupancy())

    def test_existing_tasks_blocked_before_receiver_creation(self):
        reopened = self.reopen()
        runtime = SimRuntime(self.path, self.map, {'sim-r1': {'T1': 'session1'}},
                             port=1884, client_id='restart-test', simulation_store=reopened)
        def capture(*args, **kwargs):
            self.assertIn('T1', runtime.gate._blocked)
            raise RuntimeError('stop before connecting')
        with patch.object(runtime, '_new_client', side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, 'stop before connecting'):
                runtime.start()
        self.assertEqual(reopened.list_occupancy(), self.store.list_occupancy())

    def test_unmapped_existing_task_rejected_before_network(self):
        reopened = self.reopen()
        runtime = SimRuntime(self.path, self.map, {'sim-r2': {'T2': 'session2'}},
                             port=1884, client_id='restart-test', simulation_store=reopened)
        with patch.object(runtime, '_new_client') as client:
            with self.assertRaisesRegex(ValueError, 'fully mapped'):
                runtime.start()
            client.assert_not_called()

    def test_runtime_rechecks_schema_after_open_before_network(self):
        reopened = self.reopen()
        runtime = SimRuntime(self.path, self.map, {'sim-r1': {'T1': 'session1'}},
                             port=1884, client_id='restart-test', simulation_store=reopened)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TRIGGER sim_preserve_active_delete')
            db.commit()
        with patch.object(runtime, '_new_client') as client:
            with self.assertRaises(ValueError):
                runtime.start()
            client.assert_not_called()

    def test_exclusive_runtime_owner_prevents_second_start(self):
        reopened = self.reopen()
        first = SimRuntime(self.path, self.map, {'sim-r1': {'T1': 'session1'}},
                           port=1884, client_id='restart-one', simulation_store=reopened)
        second = SimRuntime(self.path, self.map, {'sim-r1': {'T1': 'session1'}},
                            port=1884, client_id='restart-two', simulation_store=reopened)
        first._owner.acquire()
        self.addCleanup(first._owner.release)
        with patch.object(second, '_new_client') as client:
            with self.assertRaisesRegex(RuntimeError, 'already in use'):
                second.start()
            client.assert_not_called()


class CompletedExecutorRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        test_sim_handoff_store.HandoffTests._setup_source(self, 'unload')
        payload = self.store.get_order('pick')['payload'].encode('utf-8')
        self.robot_inbox = SimOrderInbox(Path(self.tmp.name) / 'robot.sqlite3', 'sim-r1', 'layout@v4')
        self.robot_inbox.initialize()
        original = SimOrderExecutor(self.robot_inbox, self.map, 'N1')
        original.accept(payload)
        self.assertEqual(original.tick(1)['receipt']['state'], 'completed')
        self.robot_inbox.initialize()
        self.executor = SimOrderExecutor(self.robot_inbox, self.map, 'N1')
        reopened = SimHandoffStore.open_existing(self.path, expected_creation_id=self.store.creation_id,
                                                  expected_idle_enabled=False, simulation_enabled=True)
        self.runtime = SimRuntime(self.path, self.map, {'sim-r1': {'pick': 'boot-1', 'delivery': 'boot-1'}},
                                  port=1884, client_id='recover-test', simulation_store=reopened)
        self.runtime.store, self.runtime.progress = reopened, self.progress
        self.runtime.gate, self.runtime.receiver = self.gate, self.receiver
        self.runtime._started = True
        self.gate.invalidate_many(('pick', 'delivery'))

    def restore(self, **kw):
        args = dict(task_id='pick', session='boot-1', event_id='final-1', action_ack=self.ack,
                    expected_twin_id='AGV_01')
        args.update(kw)
        return self.runtime.restore_completed_executor(self.executor, **args)

    def test_completed_receipt_is_recovered_without_becoming_new(self):
        evidence = self.restore()
        state = self.executor.snapshot()
        self.assertTrue(state['recovered'])
        self.assertEqual((state['status'], state['current_node']), ('stopped', 'N3'))
        self.assertIs(state['receipt']['new'], False)
        self.assertEqual(evidence['event_id'], 'final-1')
        self.assertEqual(self.executor.tick(10), state)
        self.assertIn('pick', self.gate._blocked)

    def test_incomplete_inbox_and_bad_ack_never_recover(self):
        with closing(sqlite3.connect(self.robot_inbox.path)) as db:
            db.execute("UPDATE sim_inbox_orders SET state='accepted'")
            db.commit()
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(self.executor.snapshot()['status'], 'idle')
        with self.assertRaises(ValueError):
            self.restore(action_ack=dict(self.ack, applied=False))
        self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})

    def test_bad_ack_or_event_never_recover_or_release_occupancy(self):
        for options in ({'action_ack': dict(self.ack, applied=False)},
                        {'action_ack': dict(self.ack, action='grab')},
                        {'event_id': 'missing'}, {'session': 'different'}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.restore(**options)
                self.assertEqual(self.executor.snapshot()['status'], 'idle')
                self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})

    def test_only_matching_recovered_executor_is_accepted_by_product_and_pickup(self):
        from server.order_product_control import ProductControlEngine
        from server.order_pickup_workflow import PickupWorkflow
        from server.routing import MapGraph
        evidence = self.restore()
        robot = SimpleNamespace(executor=self.executor)
        engine = ProductControlEngine.__new__(ProductControlEngine)
        engine.robot, engine.SESSION, engine.ROBOT = robot, 'boot-1', 'sim-r1'
        engine.graph = MapGraph(self.map)
        engine.positions = {node['id']: tuple(node['pos']) for node in self.map['nodes']}
        self.assertEqual(engine._validate_finished(evidence, 'pick', 'N3'), evidence)
        with self.assertRaises(ValueError):
            engine._validate_finished(dict(evidence, event_id='wrong'), 'pick', 'N3')
        pickup = PickupWorkflow.__new__(PickupWorkflow)
        pickup.robot, pickup.accept_preceding = robot, ('pick', 'N3')
        state = self.executor.snapshot()
        self.assertTrue(pickup._is_preceding_stop(state))
        state['receipt']['digest'] = 'wrong'
        self.assertFalse(pickup._is_preceding_stop(state))



if __name__ == '__main__':
    unittest.main()
