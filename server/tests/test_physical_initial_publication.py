"""Initial/handoff physical publication fences with no real broker or robot."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.allocation_store import AllocationStore
from server.coordinator import Coordinator
from server.dispatch import RobotCandidate
from server.physical_state_proof import PhysicalStateWatcher
from server.recovery import RecoveryGate
from server.sim_runtime import SimRuntime
from server.sim_handoff_store import SimHandoffStore
from server.tests.test_coordinator import FakePublisher, map_data
from server.tests.test_progress_publisher import FakeInfo
from server.tests.test_runtime_initial_publish_fence import Receiver
from server.reproductions.physical_safety_race import message
from server.tests.test_field_map_planner import sample_map


class PhysicalInitialPublicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.temp = temp
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'db.sqlite3'
        self.store = SimHandoffStore.create_new(self.path, simulation_enabled=True,
            idle_positions={'physical-1': 'N1'}, physical_robots=True)
        self.store.create_task('T1')
        self.publisher = FakePublisher()
        self.coordinator = Coordinator(self.store, map_data(), self.publisher)
        self.coordinator.prepare('T1', 'N4', [RobotCandidate('physical-1', 'N1', True)], {'type': 'none'})
        self.runtime = SimRuntime(self.path, map_data(), {'physical-1': {'T1': 'boot'}}, port=1,
                                  client_id='physical-start-test', field_simulation=True, physical_robots=True,
                                  simulation_store=self.store, field_initial_positions={'physical-1': 'N1'})
        self.gate = RecoveryGate(self.path)
        self.gate.initialize()
        self.receiver = Receiver(self.gate)
        self.runtime.gate, self.runtime.receiver = self.gate, self.receiver
        self.runtime._started = True
        self.closed = []
        self.client = object()
        self.runtime._close_client = self.closed.append
        self.now = [0.0]
        self.watcher = PhysicalStateWatcher(['physical-1'], client=SimpleNamespace(),
            host='127.0.0.1', port=1, clock=lambda: self.now[0], log=lambda _message: None)
        self.watcher._runtime = self.runtime
        self.watcher._sessions = {'physical-1': 'boot'}
        self.watcher._mqtt_started = True
        self.watcher._connected.set()
        self.watcher._subscription_done.set()
        self.watcher._online['physical-1'] = True
        self.runtime.physical_watcher = self.watcher
        self.gate.bind_physical_watcher(self.watcher)
        self.idle = {'robot_session': 'boot', 'map_id': 'layout@v4', 'last_node': 'N1',
                     'status': 'IDLE', 'error': None, 'rejected': None,
                     'order_id': None, 'seq': None, 'released_until': None}
        self.watcher._on_message(None, None, message('state', self.idle))
        with self.watcher._lock:
            self.evidence = self.watcher._start_evidence_locked('physical-1', 'boot', 'N1', 'layout@v4')
        self.runtime._prepared['T1'] = (self.coordinator, self.client, 0)
        self.runtime._physical_prepared['T1'] = self.evidence

    def test_initial_enqueue_permits_matching_first_moving_report(self):
        moving = dict(self.idle, status='MOVING', order_id='T1', seq=1, released_until='N3')
        self.publisher.info = FakeInfo(on_wait=lambda: self.watcher._on_message(None, None, message('state', moving)))
        self.assertEqual(self.runtime.publish('T1')['state'], 'acked')
        self.assertEqual(self.store.get_order('T1')['state'], 'acked')
        self.assertNotIn('physical-1', self.watcher._invalidated)
        self.assertEqual(self.watcher._motion_permits['physical-1'], ('T1', 'boot', 1, 'layout@v4', 'N3'))
        self.assertEqual(len(self.publisher.published), 1)

    def test_stale_initial_state_blocks_before_enqueue(self):
        self.now[0] = 3.0
        with self.assertRaises(ValueError):
            self.runtime.publish('T1')
        self.assertEqual(self.publisher.published, [])
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')
        self.assertTrue(self.store.list_occupancy())

    def test_initial_evidence_expiring_during_attempt_record_blocks_enqueue(self):
        original = self.store.mark_attempted
        def slow_mark(*args, **kwargs):
            result = original(*args, **kwargs)
            self.now[0] = 3.0
            return result
        self.store.mark_attempted = slow_mark
        with self.assertRaises(ValueError):
            self.runtime.publish('T1')
        self.assertEqual(self.publisher.published, [])
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertTrue(self.store.list_occupancy())

    def test_handoff_source_stop_change_blocks_target_enqueue(self):
        source = dict(self.idle, status='WAITING', order_id='previous', seq=2, released_until='N1')
        self.watcher._on_message(None, None, message('state', source))
        self.watcher._proven['physical-1'] = (('previous', 2), self.now[0])
        with self.watcher._lock:
            evidence = self.watcher._start_evidence_locked(
                'physical-1', 'boot', 'N1', 'layout@v4', source_task='previous', source_seq=2)
        self.runtime._physical_prepared['T1'] = evidence
        self.watcher._on_message(None, None, message('state', dict(source, last_node='N2')))
        with self.assertRaises((ValueError, RuntimeError)):
            self.runtime.publish('T1')
        self.assertEqual(self.publisher.published, [])
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')
        self.assertTrue(self.store.list_occupancy())

    def test_initial_prepare_expiry_rolls_back_before_commit(self):
        path = Path(self.temp.name) / 'field.sqlite3'
        store = SimHandoffStore.create_new(path, simulation_enabled=True,
            idle_positions={'physical-1': 'B'}, physical_robots=True)
        store.bind_field_robots(('physical-1',), {'physical-1': 'B'})
        store._field_required = True
        store.create_task('T1')
        data = sample_map()
        for edge in data['edges']:
            edge['lanes'] = 1
        data['zones'] = [{'id': 'wait-end', 'type': 'waiting', 'node': 'D_C'}]
        runtime = SimRuntime(path, data, {'physical-1': {'T1': 'boot'}}, port=1,
                             client_id='physical-prepare-test', field_simulation=True, physical_robots=True,
                             simulation_store=store, field_initial_positions={'physical-1': 'B'})
        gate = RecoveryGate(path, field_required=True)
        gate.initialize()
        runtime.gate, runtime.receiver, runtime.store = gate, Receiver(gate), store
        runtime._started = True
        runtime._connect_publisher = lambda: (object(), FakePublisher())
        runtime._close_client = lambda _client: None
        watcher = PhysicalStateWatcher(['physical-1'], client=SimpleNamespace(),
            host='127.0.0.1', port=1, clock=lambda: self.now[0], log=lambda _message: None)
        watcher._runtime, watcher._sessions = runtime, {'physical-1': 'boot'}
        watcher._mqtt_started = True
        watcher._connected.set()
        watcher._subscription_done.set()
        watcher._online['physical-1'] = True
        runtime.physical_watcher = watcher
        gate.bind_physical_watcher(watcher)
        report = dict(self.idle, map_id='field-test@v1', last_node='B')
        watcher._on_message(None, None, message('state', report))
        original, calls = watcher._start_evidence_locked, []
        def expire_before_commit(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                self.now[0] = 3.0
            return original(*args, **kwargs)
        with patch.object(watcher, '_start_evidence_locked', side_effect=expire_before_commit):
            with self.assertRaises(ValueError) as failure:
                runtime.prepare_field('T1', 'D_C',
                    [{'robot_id': 'physical-1', 'node': 'B', 'incoming_edge': 'E1', 'available': True}],
                    {'type': 'none'})
        self.assertEqual(len(calls), 2, str(failure.exception))
        self.assertIsNone(store.get_allocation('T1'))
        self.assertIsNone(store.get_order('T1'))
        self.assertEqual(store.list_occupancy(), {('node', 'B'): 'physical-1'})
