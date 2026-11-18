"""Isolated handoff keeps historical orders and transfers the stop atomically."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
import paho.mqtt.client as mqtt

from server.allocation_store import AllocationConflict, AllocationStore
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress
from server.segments import plan_segment
from server.tests.test_segment_progress import map_data
from sim.event_inbox import SimEventInbox
from sim.event_receiver import SimEventReceiver
from server.sim_handoff_store import SimHandoffStore, SimHandoffCoordinatorStore, SimResourceBusy
from server.coordinator import Coordinator
from server.dispatch import RobotCandidate
from server.tests.test_coordinator import FakePublisher
from server.sim_runtime import SimRuntime
from unittest.mock import Mock, patch


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._setup_source('load')

    def _setup_source(self, source_action, idle_positions=None):
        suffix = '-fleet' if idle_positions is not None else ''
        self.path = Path(self.tmp.name) / ('isolated-' + source_action + suffix + '.sqlite3')
        self.store = SimHandoffStore.create_new(self.path, simulation_enabled=True, idle_positions=idle_positions)
        self.map = map_data()
        self.fingerprint = hashlib.sha256(json.dumps(self.map, sort_keys=True, ensure_ascii=True,
                                       allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.gate = RecoveryGate(self.path)
        self.gate.initialize()
        self.progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=self.gate)
        self.progress.initialize()
        self.inbox = SimEventInbox(self.path, robot_tasks={'sim-r1': ('pick', 'delivery')}, simulation_enabled=True)
        self.inbox.initialize()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='simulation-test',
                             clean_session=False, reconnect_on_failure=False)
        self.receiver = SimEventReceiver(client, self.progress, inbox=self.inbox, recovery_gate=self.gate,
                    robot_tasks={'sim-r1': ('pick', 'delivery')},
                    robot_sessions={'sim-r1': {'pick': 'boot-1', 'delivery': 'boot-1'}}, simulation_enabled=True)
        self.receiver._ready = True
        self.store.create_task('pick')
        path = ['N1', 'N2', 'N3']
        segment = plan_segment(self.map, path)
        allocation = self.store.allocate('pick', 'sim-r1', 'layout@v4', path, segment.released_until,
                                         segment.resources, {'type': source_action}, map_fingerprint=self.fingerprint)
        order = self.store.get_order('pick')
        self.store.mark_attempted('pick', order['payload'], allocation, segment.resources)
        self.store.mark_broker_acked('pick', order['payload'], allocation, segment.resources)
        digest = hashlib.sha256(json.dumps(json.loads(order['payload']), sort_keys=True, ensure_ascii=True,
                                allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        token = self.gate.begin('pick', 'boot-1')
        self.gate.complete('pick', 'boot-1', token, robot_id='sim-r1', order_id='pick', seq=1,
                           map_id='layout@v4', stopped_at='N3', order_digest=digest, stopped=True)
        self.event = {'type': 'rear_clear', 'event_id': 'final-1', 'robot_session': 'boot-1',
                      'order_id': 'pick', 'seq': 1, 'map_id': 'layout@v4', 'stopped_at': 'N3',
                      'cleared_resources': [list(item) for item in sorted(set(segment.resources)-{('node','N3')})]}
        self.inbox.store('sim-r1', self.event)
        self.assertEqual(self.inbox.retry('sim-r1', 'final-1', self.progress)['status'], 'finished')
        self.store.create_task('delivery')
        self.next_nodes = ['N3', 'N4', 'N5']
        self.next_segment = plan_segment(self.map, self.next_nodes)
        self.ack = {'order_id': 'pick', 'action_id': 'grab-1', 'agv_id': 'AGV_01',
                    'action': 'place' if source_action == 'unload' else 'grab', 'applied': True}

    def transfer(self, **overrides):
        args = {'task_id': 'delivery', 'robot_id': 'sim-r1', 'map_id': 'layout@v4',
                'nodes': self.next_nodes, 'released_until': self.next_segment.released_until,
                'resources': self.next_segment.resources, 'action_at_end': {'type': 'unload'},
                'map_fingerprint': self.fingerprint, 'source_task_id': 'pick',
                'robot_session': 'boot-1', 'seq': 1, 'stopped_at': 'N3', 'event_id': 'final-1',
                'action_ack': self.ack, 'expected_twin_id': 'AGV_01',
                'gate': self.gate, 'progress': self.progress, 'receiver': self.receiver, 'simulation_enabled': True}
        args.update(overrides)
        return self.store.handoff_allocate(**args)

    def test_transfer_preserves_old_history_and_moves_all_reservations(self):
        result = self.transfer()
        self.assertEqual(result['robot_id'], 'sim-r1')
        self.assertEqual(result['nodes'], self.next_nodes)
        self.assertEqual(set(self.store.list_occupancy()), set(self.next_segment.resources))
        self.assertEqual(self.store.get_order('pick')['state'], 'acked')
        self.assertEqual(self.store.get_order('delivery')['state'], 'pending')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT robot_id,task_id FROM sim_active_robots').fetchall(), [('sim-r1', 'delivery')])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM allocation_jobs').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT result FROM progress_events WHERE event_id='final-1'").fetchone(), ('finished',))
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE task_id='pick'").fetchone(), ('DONE',))
            self.assertEqual(db.execute("SELECT status FROM recovery_challenges WHERE task_id='pick'").fetchone(), ('revoked',))
        with self.assertRaises((ValueError, RuntimeError)):
            self.transfer()

    def test_invalid_identity_or_proof_rolls_back(self):
        scenarios = [
            {'simulation_enabled': False}, {'robot_id': 'sim-r2'}, {'robot_session': 'old'},
            {'seq': 2}, {'stopped_at': 'N2'}, {'event_id': 'missing'},
            {'map_fingerprint': '0' * 64}, {'nodes': ['N2', 'N3', 'N4', 'N5']},
            {'action_ack': dict(self.ack, applied=False)},
            {'action_ack': dict(self.ack, action_id='')},
            {'action_ack': dict(self.ack, action='place')},
            {'action_ack': dict(self.ack, order_id='wrong')},
            {'expected_twin_id': 'AGV_02'},
            {'resources': (('node', 'N3'),)},
        ]
        for invalid in scenarios:
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError, RuntimeError, sqlite3.Error)):
                    self.transfer(**invalid)
                self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})
                self.assertIsNone(self.store.get_allocation('delivery'))
                self.assertIsNone(self.store.get_order('delivery'))

    def test_completed_unload_requires_place_ack_and_allows_next_leg(self):
        self._setup_source('unload')
        with self.assertRaises((ValueError, RuntimeError)):
            self.transfer(action_ack=dict(self.ack, action='grab'))
        self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})
        self.assertIsNone(self.store.get_allocation('delivery'))
        self.assertEqual(self.transfer()['robot_id'], 'sim-r1')
        self.assertEqual(set(self.store.list_occupancy()), set(self.next_segment.resources))
        with self.assertRaises((ValueError, RuntimeError)):
            self.transfer()

    def test_unrecognized_source_action_cannot_handoff(self):
        self._setup_source('unsupported')
        with self.assertRaises((ValueError, RuntimeError)):
            self.transfer()
        self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})
        self.assertIsNone(self.store.get_allocation('delivery'))

    def test_occupied_next_resource_rolls_back_old_ownership(self):
        self.store.create_task('other')
        blocked_nodes = ['N4', 'N5']
        blocked = plan_segment(self.map, blocked_nodes)
        self.store.allocate('other', 'sim-r2', 'layout@v4', blocked_nodes, blocked.released_until,
                            blocked.resources, {'type': 'load'}, map_fingerprint=self.fingerprint)
        before = self.store.list_occupancy()
        with self.assertRaises(SimResourceBusy):
            self.transfer()
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertIsNone(self.store.get_allocation('delivery'))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT task_id FROM sim_active_robots WHERE robot_id='sim-r1'").fetchone(), ('pick',))

    def test_other_idle_robot_blocks_handoff_without_losing_old_ownership(self):
        self._setup_source('load', {'sim-r1': 'N1', 'sim-r2': 'N4'})
        before = self.store.list_occupancy()
        with self.assertRaises(SimResourceBusy):
            self.transfer()
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertEqual(before, {('node', 'N3'): 'sim-r1', ('node', 'N4'): 'sim-r2'})
        self.assertIsNone(self.store.get_allocation('delivery'))

    def test_direct_second_allocation_cannot_reuse_active_robot(self):
        with self.assertRaises(AllocationConflict):
            self.store.allocate('delivery', 'sim-r1', 'layout@v4', self.next_nodes,
                                self.next_segment.released_until, self.next_segment.resources,
                                {'type': 'unload'}, map_fingerprint=self.fingerprint)
        self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})

    def test_physical_robot_id_is_rejected_by_store_and_database(self):
        self.store.create_task('physical')
        with self.assertRaises((ValueError, RuntimeError)):
            self.store.allocate('physical', 'jetank-1', 'layout@v4', self.next_nodes,
                                self.next_segment.released_until, self.next_segment.resources,
                                {'type': 'unload'}, map_fingerprint=self.fingerprint)
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO allocation_jobs VALUES (?,?,?,?,?)',
                           ('physical', 'jetank-1', 'layout@v4', '["N3","N4","N5"]', 'N5'))
        self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})

    def test_disconnected_receiver_or_revoked_proof_cannot_handoff(self):
        self.receiver._ready = False
        with self.assertRaises((ValueError, RuntimeError)):
            self.transfer()
        self.receiver._ready = True
        self.gate.invalidate('pick')
        with self.assertRaises((ValueError, RuntimeError)):
            self.transfer()
        self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})
        self.assertIsNone(self.store.get_allocation('delivery'))

    def test_existing_coordinator_uses_single_handoff_and_publishes(self):
        adapter = SimHandoffCoordinatorStore(self.store, target_task_id='delivery',
            source_task_id='pick', robot_id='sim-r1', robot_session='boot-1', seq=1,
            stopped_at='N3', event_id='final-1', action_ack=self.ack, expected_twin_id='AGV_01',
            gate=self.gate, progress=self.progress, receiver=self.receiver, simulation_enabled=True)
        publisher = FakePublisher()
        coordinator = Coordinator(adapter, self.map, publisher)
        result = coordinator.prepare('delivery', 'N5', [RobotCandidate('sim-r1', 'N3', True)], {'type': 'unload'})
        self.assertEqual(result['nodes'], ['N3', 'N4', 'N5'])
        self.assertEqual(coordinator.publish('delivery')['state'], 'acked')
        self.assertEqual(self.store.get_order('delivery')['state'], 'acked')
        self.assertEqual(publisher.published[0][0], 'sim-r1')
        with self.assertRaises((ValueError, RuntimeError)):
            adapter.allocate('delivery', 'sim-r1', 'layout@v4', self.next_nodes,
                             self.next_segment.released_until, self.next_segment.resources,
                             {'type': 'unload'}, map_fingerprint=self.fingerprint)

    def test_handoff_adapter_cannot_be_reused_after_failed_attempt(self):
        adapter = SimHandoffCoordinatorStore(self.store, target_task_id='delivery',
            source_task_id='pick', robot_id='sim-r1', robot_session='boot-1', seq=1,
            stopped_at='N3', event_id='final-1', action_ack=self.ack, expected_twin_id='AGV_01',
            gate=self.gate, progress=self.progress, receiver=self.receiver, simulation_enabled=True)
        self.receiver._ready = False
        with self.assertRaises((ValueError, RuntimeError)):
            adapter.allocate('delivery', 'sim-r1', 'layout@v4', self.next_nodes,
                             self.next_segment.released_until, self.next_segment.resources,
                             {'type': 'unload'}, map_fingerprint=self.fingerprint)
        self.receiver._ready = True
        with self.assertRaises((ValueError, RuntimeError)):
            adapter.allocate('delivery', 'sim-r1', 'layout@v4', self.next_nodes,
                             self.next_segment.released_until, self.next_segment.resources,
                             {'type': 'unload'}, map_fingerprint=self.fingerprint)
        self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})

    def test_handoff_adapter_retries_only_rolled_back_resource_conflict(self):
        adapter = SimHandoffCoordinatorStore(self.store, target_task_id='delivery',
            source_task_id='pick', robot_id='sim-r1', robot_session='boot-1', seq=1,
            stopped_at='N3', event_id='final-1', action_ack=self.ack, expected_twin_id='AGV_01',
            gate=self.gate, progress=self.progress, receiver=self.receiver, simulation_enabled=True)
        self.store.create_task('other')
        blocked_nodes = ['N4', 'N5']
        blocked = plan_segment(self.map, blocked_nodes)
        self.store.allocate('other', 'sim-r2', 'layout@v4', blocked_nodes, blocked.released_until,
                            blocked.resources, {'type': 'load'}, map_fingerprint=self.fingerprint)
        args = ('delivery', 'sim-r1', 'layout@v4', self.next_nodes,
                self.next_segment.released_until, self.next_segment.resources, {'type': 'unload'})
        before = self.store.list_occupancy()
        with self.assertRaises(SimResourceBusy):
            adapter.allocate(*args, map_fingerprint=self.fingerprint)
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertIsNone(self.store.get_allocation('delivery'))
        with self.store._write() as db:
            db.execute("DELETE FROM allocation_occupancy WHERE task_id='other'")
        self.assertEqual(adapter.allocate(*args, map_fingerprint=self.fingerprint)['robot_id'], 'sim-r1')
        with self.assertRaises(AllocationConflict):
            adapter.allocate(*args, map_fingerprint=self.fingerprint)

    def test_handoff_busy_then_revoked_proof_permanently_consumes_adapter(self):
        from server.recovery import RecoveryError
        adapter = SimHandoffCoordinatorStore(self.store, target_task_id='delivery',
            source_task_id='pick', robot_id='sim-r1', robot_session='boot-1', seq=1,
            stopped_at='N3', event_id='final-1', action_ack=self.ack, expected_twin_id='AGV_01',
            gate=self.gate, progress=self.progress, receiver=self.receiver, simulation_enabled=True)
        self.store.create_task('other')
        blocked_nodes = ['N4', 'N5']
        blocked = plan_segment(self.map, blocked_nodes)
        self.store.allocate('other', 'sim-r2', 'layout@v4', blocked_nodes, blocked.released_until,
                            blocked.resources, {'type': 'load'}, map_fingerprint=self.fingerprint)
        args = ('delivery', 'sim-r1', 'layout@v4', self.next_nodes,
                self.next_segment.released_until, self.next_segment.resources, {'type': 'unload'})
        with self.assertRaises(SimResourceBusy):
            adapter.allocate(*args, map_fingerprint=self.fingerprint)
        self.gate.invalidate('pick')
        with self.assertRaises(RecoveryError):
            adapter.allocate(*args, map_fingerprint=self.fingerprint)
        with self.store._write() as db:
            db.execute("DELETE FROM allocation_occupancy WHERE task_id='other'")
        with self.assertRaises(AllocationConflict):
            adapter.allocate(*args, map_fingerprint=self.fingerprint)
        self.assertIsNone(self.store.get_allocation('delivery'))

    def test_runtime_injected_handoff_prepares_and_publishes(self):
        sessions = {'sim-r1': {'pick': 'boot-1', 'delivery': 'boot-1'}}
        runtime = SimRuntime(self.path, self.map, sessions, port=18888, client_id='injection',
                             simulation_store=self.store)
        runtime.store, runtime.gate, runtime.progress = self.store, self.gate, self.progress
        runtime.receiver = self.receiver
        runtime._started = True
        adapter = SimHandoffCoordinatorStore(self.store, target_task_id='delivery',
            source_task_id='pick', robot_id='sim-r1', robot_session='boot-1', seq=1,
            stopped_at='N3', event_id='final-1', action_ack=self.ack, expected_twin_id='AGV_01',
            gate=self.gate, progress=self.progress, receiver=self.receiver, simulation_enabled=True)
        publisher, client = FakePublisher(), Mock()
        with patch.object(runtime, '_connect_publisher', return_value=(client, publisher)), \
             patch.object(runtime, '_close_client'):
            result = runtime.prepare('delivery', 'N5',
                [{'robot_id': 'sim-r1', 'node': 'N3', 'available': True}],
                {'type': 'unload'}, handoff_store=adapter)
            self.assertEqual(result['nodes'], ['N3', 'N4', 'N5'])
            self.assertEqual(runtime.publish('delivery')['state'], 'acked')

    def test_runtime_injection_rejects_wrong_store_or_task_before_publish(self):
        sessions = {'sim-r1': {'pick': 'boot-1', 'delivery': 'boot-1'}}
        with self.assertRaises(ValueError):
            SimRuntime(self.path, self.map, sessions, port=18888, client_id='injection',
                       simulation_store=AllocationStore(self.path))
        with self.assertRaises(ValueError):
            SimRuntime(Path(self.tmp.name) / 'another.sqlite3', self.map, sessions,
                       port=18888, client_id='injection', simulation_store=self.store)
        runtime = SimRuntime(self.path, self.map, sessions, port=18888, client_id='injection',
                             simulation_store=self.store)
        runtime.store, runtime.gate, runtime.progress = self.store, self.gate, self.progress
        runtime.receiver = self.receiver
        runtime._started = True
        adapter = SimHandoffCoordinatorStore(self.store, target_task_id='pick',
            source_task_id='delivery', robot_id='sim-r1', robot_session='boot-1', seq=1,
            stopped_at='N3', event_id='final-1', action_ack=self.ack, expected_twin_id='AGV_01',
            gate=self.gate, progress=self.progress, receiver=self.receiver, simulation_enabled=True)
        with patch.object(runtime, '_connect_publisher') as connect:
            with self.assertRaises(ValueError):
                runtime.prepare('delivery', 'N5',
                    [{'robot_id': 'sim-r1', 'node': 'N3', 'available': True}],
                    {'type': 'unload'}, handoff_store=adapter)
            connect.assert_not_called()
        self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})

    def test_runtime_start_reuses_fresh_injected_store(self):
        separate = SimHandoffStore.create_new(Path(self.tmp.name) / 'startup.sqlite3', simulation_enabled=True)
        runtime = SimRuntime(separate.path, self.map, {'sim-r1': {'T1': 'boot-1'}},
                             port=18888, client_id='startup-test', simulation_store=separate)
        client = Mock()
        # This client is a stub: loop_start does not create a network thread.
        # An unspecified Mock attribute is itself truthy, unlike Paho's None.
        client._thread = None
        client.connect.return_value = 0
        client.loop_start.return_value = 0
        with patch.object(runtime, '_new_client', return_value=client), \
             patch('server.sim_runtime.SimEventReceiver') as receiver, \
             patch('server.sim_runtime.SimProgressWorkflow'):
            receiver.return_value.ready = True
            runtime.start()
            try:
                self.assertIs(runtime.store, separate)
                self.assertTrue(runtime.status()['ready'])
            finally:
                runtime.stop()
        client.disconnect.assert_called_once_with()
        client.loop_stop.assert_called_once_with()
        self.assertIsNone(runtime._owner.file)

    def test_client_cleanup_rejects_a_real_running_thread(self):
        running, release = threading.Event(), threading.Event()
        def network_loop():
            running.set()
            release.wait(30)
        thread = threading.Thread(target=network_loop)
        client = Mock()
        client._thread = thread
        thread.start()
        try:
            self.assertTrue(running.wait(2))
            # A faulty loop_stop that returns without joining must not be
            # treated as successful cleanup merely because it returned.
            with self.assertRaisesRegex(RuntimeError, 'network thread is still running'):
                SimRuntime._close_client(client)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        SimRuntime._close_client(client)

    def test_runtime_rejects_corrupted_simulation_store_identity(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE sim_store_identity SET value='wrong'")
            db.commit()
        with self.assertRaises(ValueError):
            SimRuntime(self.path, self.map, {'sim-r1': {'pick': 'boot-1'}},
                       port=18888, client_id='invalid-test', simulation_store=self.store)
        with self.assertRaises(ValueError):
            self.transfer()
        self.assertEqual(self.store.list_occupancy(), {('node', 'N3'): 'sim-r1'})

    def test_existing_database_and_non_simulation_are_rejected_without_mutation(self):
        old = Path(self.tmp.name) / 'old.sqlite3'
        old_store = AllocationStore(old)
        old_store.initialize()
        before = old.read_bytes()
        with self.assertRaises((ValueError, FileExistsError)):
            SimHandoffStore.create_new(old, simulation_enabled=True)
        self.assertEqual(old.read_bytes(), before)
        with self.assertRaises(ValueError):
            SimHandoffStore.create_new(Path(self.tmp.name) / 'no.sqlite3', simulation_enabled=False)


class IdleFleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.map = map_data()
        self.fingerprint = hashlib.sha256(json.dumps(self.map, sort_keys=True, ensure_ascii=True,
                                      allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.store = SimHandoffStore.create_new(Path(self.tmp.name) / 'fleet.sqlite3',
                    simulation_enabled=True, idle_positions={'sim-r1': 'N1', 'sim-r2': 'N5'})
        for task in ('one', 'two'):
            self.store.create_task(task)

    def allocate(self, task, robot, path):
        segment = plan_segment(self.map, path)
        return self.store.allocate(task, robot, 'layout@v4', path, segment.released_until,
                                   segment.resources, {'type': 'load'}, map_fingerprint=self.fingerprint)

    def test_idle_positions_are_protected_and_transfer_atomically(self):
        self.assertEqual(self.store.list_occupancy(), {('node', 'N1'): 'sim-r1', ('node', 'N5'): 'sim-r2'})
        self.allocate('one', 'sim-r1', ['N1', 'N2', 'N3'])
        self.assertEqual(self.store.list_occupancy()[('node', 'N5')], 'sim-r2')
        self.assertEqual(self.store.list_occupancy()[('node', 'N1')], 'sim-r1')
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT robot_id,node FROM sim_idle_robots').fetchall(), [('sim-r2', 'N5')])
            self.assertEqual(db.execute("SELECT task_id FROM allocation_occupancy WHERE kind='node' AND resource_id='N1'").fetchone(), ('one',))

    def test_other_robot_idle_node_blocks_allocation_and_rolls_back(self):
        store = SimHandoffStore.create_new(Path(self.tmp.name) / 'block.sqlite3',
                    simulation_enabled=True, idle_positions={'sim-r1': 'N1', 'sim-r2': 'N3'})
        store.create_task('blocked')
        segment = plan_segment(self.map, ['N1', 'N2', 'N3'])
        with self.assertRaises(SimResourceBusy):
            store.allocate('blocked', 'sim-r1', 'layout@v4', ['N1', 'N2', 'N3'],
                           segment.released_until, segment.resources, {'type': 'load'},
                           map_fingerprint=self.fingerprint)
        self.assertEqual(store.list_occupancy(), {('node', 'N1'): 'sim-r1', ('node', 'N3'): 'sim-r2'})
        self.assertIsNone(store.get_allocation('blocked'))
        self.assertIsNone(store.get_order('blocked'))

    def test_wrong_origin_and_unknown_robot_cannot_consume_idle_reservations(self):
        for robot, path in [('sim-r1', ['N2', 'N3']), ('sim-r3', ['N1', 'N2', 'N3'])]:
            with self.subTest(robot=robot), self.assertRaises((ValueError, RuntimeError)):
                self.allocate('one', robot, path)
        self.assertEqual(self.store.list_occupancy(), {('node', 'N1'): 'sim-r1', ('node', 'N5'): 'sim-r2'})

    def test_two_robots_competing_for_same_segment_have_exactly_one_winner(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def run(task, robot, path):
            barrier.wait()
            try:
                self.allocate(task, robot, path)
                outcomes.append((robot, 'won'))
            except AllocationConflict:
                outcomes.append((robot, 'blocked'))
        threads = [threading.Thread(target=run, args=('one', 'sim-r1', ['N1','N2','N3'])),
                   threading.Thread(target=run, args=('two', 'sim-r2', ['N5','N4','N3']))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertEqual(sorted(outcome for _, outcome in outcomes), ['blocked', 'won'])
        loser = next(robot for robot, outcome in outcomes if outcome == 'blocked')
        self.assertEqual(self.store.list_occupancy()[('node', 'N1' if loser == 'sim-r1' else 'N5')], loser)

    def test_invalid_or_duplicate_initial_positions_are_rejected(self):
        for positions in ({}, {'jetank': 'N1'}, {'sim-r1': ''},
                          {'sim-r1': 'N1', 'sim-r2': 'N1'}, {'sim-r1': 1}):
            with self.subTest(positions=positions), self.assertRaises((ValueError, RuntimeError)):
                SimHandoffStore.create_new(Path(self.tmp.name) / 'invalid.sqlite3',
                        simulation_enabled=True, idle_positions=positions)
            self.assertFalse((Path(self.tmp.name) / 'invalid.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
