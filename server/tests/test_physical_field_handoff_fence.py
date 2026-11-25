"""Full isolated physical source-to-target handoff; no hardware is started."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.physical_state_proof import PhysicalStateWatcher
from server.recovery import RecoveryGate
from server.route_occurrence import plan_occurrence_segment
from server.segment_progress import SegmentProgress
from server.sim_handoff_store import SimHandoffStore
from server.sim_runtime import SimRuntime
from server.tests.test_coordinator import FakePublisher
from server.tests.test_field_map_planner import sample_map
from server.tests.test_progress_publisher import FakeInfo
from server.reproductions.physical_safety_race import message
from sim.event_inbox import SimEventInbox
from sim.event_receiver import SimEventReceiver
from sim.tests.test_event_receiver import FakeClient


class PhysicalFieldHandoffFenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'handoff.sqlite3'
        self.data = sample_map()
        for edge in self.data['edges']:
            edge['lanes'] = 1
        self.data['zones'] = [{'id': 'end-spur', 'type': 'waiting', 'node': 'D_C'},
                              {'id': 'end-a', 'type': 'waiting', 'node': 'A'}]
        self.store = SimHandoffStore.create_new(path, simulation_enabled=True,
                                               idle_positions={'physical-1': 'B'}, physical_robots=True)
        self.store.bind_field_robots(('physical-1',), {'physical-1': 'B'})
        self.store._field_required = True
        robots = {'physical-1': {'source': 'boot', 'target': 'boot'}}
        self.runtime = SimRuntime(path, self.data, robots, port=1, client_id='physical-handoff-test',
                                  field_simulation=True, field_initial_positions={'physical-1': 'B'},
                                  physical_robots=True, simulation_store=self.store)
        self.gate = RecoveryGate(path, field_required=True)
        self.gate.initialize()
        self.progress = SegmentProgress(self.store, self.data, simulation_enabled=True,
                                        recovery_gate=self.gate, field_simulation=True)
        self.progress.initialize()
        self.inbox = SimEventInbox(path, robot_tasks={'physical-1': ('source', 'target')}, simulation_enabled=True)
        self.inbox.initialize()
        self.receiver = SimEventReceiver(FakeClient(), self.progress, inbox=self.inbox, recovery_gate=self.gate,
            robot_tasks={'physical-1': ('source', 'target')}, robot_sessions=robots, simulation_enabled=True)
        self.receiver._ready = True
        self.runtime.gate, self.runtime.store = self.gate, self.store
        self.runtime.receiver, self.runtime.progress, self.runtime.inbox = self.receiver, self.progress, self.inbox
        self.runtime._started = True
        self.publishers = []
        def connect():
            publisher = FakePublisher()
            self.publishers.append(publisher)
            return object(), publisher
        self.runtime._connect_publisher = connect
        self.runtime._close_client = lambda _client: None
        self.watcher = PhysicalStateWatcher(['physical-1'], client=SimpleNamespace(), host='127.0.0.1', port=1,
                                            log=lambda _message: None, clock=lambda: 0.0)
        self.watcher._runtime, self.watcher._sessions = self.runtime, {'physical-1': 'boot'}
        self.watcher._mqtt_started = True
        self.watcher._connected.set()
        self.watcher._subscription_done.set()
        self.watcher._online['physical-1'] = True
        self.runtime.physical_watcher = self.watcher
        self.gate.bind_physical_watcher(self.watcher)
        self.state = {'robot_session': 'boot', 'map_id': 'field-test@v1', 'last_node': 'B',
                      'status': 'IDLE', 'error': None, 'rejected': None,
                      'order_id': None, 'seq': None, 'released_until': None}
        self.watcher._on_message(None, None, message('state', self.state))

    def test_source_completed_handoff_target_moving_ack(self):
        self.runtime.create_task('source')
        self.runtime.prepare_field('source', 'D_C',
            [{'robot_id': 'physical-1', 'node': 'B', 'incoming_edge': 'E1', 'available': True}],
            {'type': 'none'})
        self.assertEqual(self.runtime.publish('source')['state'], 'acked')
        stopped = dict(self.state, status='IDLE', order_id='source', seq=1,
                       last_node='D_C', released_until='D_C')
        self.watcher._on_message(None, None, message('state', stopped))
        self.watcher._maybe_prove('physical-1', stopped)
        self.assertEqual(self.watcher._proven['physical-1'][0], ('source', 1))
        segment = plan_occurrence_segment(self.data, ['B', 'C', 'D_C'], 0)
        event = {'type': 'rear_clear', 'event_id': 'source-finished', 'robot_session': 'boot',
                 'order_id': 'source', 'seq': 1, 'map_id': 'field-test@v1', 'stopped_at': 'D_C',
                 'cleared_resources': [list(x) for x in sorted(set(segment.resources) - {('node', 'D_C')})]}
        self.inbox.store('physical-1', event)
        self.assertEqual(self.inbox.retry('physical-1', 'source-finished', self.progress)['status'], 'finished')
        self.runtime.create_task('target')
        allocation = self.runtime.prepare_field_next('target', 'source', 'A')
        self.assertEqual(allocation['nodes'], ['D_C', 'C', 'B', 'A'])
        self.assertEqual(self.store.get_order('target')['state'], 'pending')
        moving = dict(stopped, status='MOVING', order_id='target', seq=1, released_until='A')
        self.publishers[-1].info = FakeInfo(
            on_wait=lambda: self.watcher._on_message(None, None, message('state', moving)))
        self.assertEqual(self.runtime.publish('target')['state'], 'acked')
        self.assertEqual(self.store.get_order('target')['state'], 'acked')
        self.assertNotIn('physical-1', self.watcher._invalidated)
        self.assertEqual(self.watcher._motion_permits['physical-1'], ('target', 'boot', 1, 'field-test@v1', 'A'))
