"""Two physical identities, test-owned SQLite and no broker or hardware."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.allocation_store import AllocationStore
from server.order_publisher import OrderPublisher
from server.physical_state_proof import PhysicalStateWatcher
from server.progress_publisher import ProgressPublisher
from server.recovery import RecoveryError, RecoveryGate
from server.segment_progress import SegmentOccupied, SegmentProgress
from server.segments import plan_segment
from server.sim_runtime import SimRuntime
from server.tests.test_progress_publisher import FakeClient


def message(robot, kind, payload):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode('utf-8')
    return SimpleNamespace(topic='agv/%s/%s' % (robot, kind), retain=False, qos=1,
                           payload=payload)


def _map_data():
    ids = list('ABCDE') + list('PQRSTUV')
    nodes = [{'id': name, 'pos': [i * 10, 0]} for i, name in enumerate(ids)]
    positions = {node['id']: node['pos'] for node in nodes}
    pairs = [('A', 'B'), ('B', 'C'), ('C', 'D'), ('D', 'E'), ('P', 'Q'),
             ('Q', 'R'), ('R', 'S'), ('S', 'T'), ('R', 'U'), ('U', 'V')]
    edges = [{'from': a, 'to': b, 'shape': [positions[a], positions[b]],
              'bidirectional': True, 'lanes': 1} for a, b in pairs]
    zones = [{'id': 'wait-' + node, 'type': 'waiting', 'node': node} for node in 'CERTV']
    zones.append({'id': 'common', 'type': 'shared', 'nodes': ['B', 'S']})
    return {'map_id': 'two', 'version': 1, 'unit': 'mm', 'nodes': nodes, 'edges': edges, 'zones': zones}


class TwoPhysicalRobotsFenceTests(unittest.TestCase):
    def configure(self, route_two):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'fleet.sqlite3'
        graph = _map_data()
        fingerprint = hashlib.sha256(json.dumps(graph, sort_keys=True, ensure_ascii=True,
                                      allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.store = AllocationStore(path)
        self.store.initialize()
        routes = {'physical-1': ['A', 'B', 'C', 'D', 'E'], 'physical-2': route_two}
        sessions = {'physical-1': 'boot-1', 'physical-2': 'boot-2'}
        tasks = {'physical-1': 'T1', 'physical-2': 'T2'}
        self.events = {}
        for robot, route in routes.items():
            task = tasks[robot]
            first = plan_segment(graph, route)
            next_segment = plan_segment(graph, route[route.index(first.released_until):])
            self.store.create_task(task)
            alloc = self.store.allocate(task, robot, 'two@v1', route, first.released_until,
                                        first.resources, {'type': 'none'}, map_fingerprint=fingerprint)
            payload = self.store.get_order(task)['payload']
            self.store.mark_attempted(task, payload, alloc, first.resources)
            self.store.mark_broker_acked(task, payload, alloc, first.resources)
            self.events[robot] = {'type': 'rear_clear', 'event_id': 'event-' + task,
                                  'robot_session': sessions[robot], 'order_id': task, 'seq': 1,
                                  'map_id': 'two@v1', 'stopped_at': first.released_until,
                                  'cleared_resources': [list(x) for x in sorted(
                                      set(first.resources) - set(next_segment.resources))]}
        self.gate = RecoveryGate(path)
        self.gate.initialize()
        self.progress = SegmentProgress(self.store, graph, simulation_enabled=True, recovery_gate=self.gate)
        self.progress.initialize()
        self.runtime = SimRuntime.__new__(SimRuntime)
        self.runtime._lock = threading.RLock()
        self.runtime.receiver = SimpleNamespace(_dispatch_lock=threading.RLock())
        self.runtime._ready = lambda: None
        self.runtime._tasks = {tasks[robot]: (robot, sessions[robot]) for robot in tasks}
        self.runtime._field_simulation = False
        self.runtime.path = str(path)
        self.runtime.gate = self.gate
        self.watcher = PhysicalStateWatcher(list(routes), client=SimpleNamespace(), host='127.0.0.1',
                                            port=1, clock=lambda: 100.0, log=lambda _: None)
        self.runtime.physical_watcher = self.watcher
        self.watcher._runtime = self.runtime
        self.watcher._sessions = sessions
        self.watcher._mqtt_started = True
        self.watcher._connected.set()
        self.watcher._subscription_done.set()
        self.watcher._online.update({robot: True for robot in routes})
        self.gate.bind_physical_watcher(self.watcher)
        for robot, event in self.events.items():
            state = {'robot_session': sessions[robot], 'status': 'WAITING',
                     'order_id': tasks[robot], 'seq': 1, 'last_node': event['stopped_at'],
                     'released_until': event['stopped_at'], 'map_id': 'two@v1',
                     'error': None, 'rejected': None}
            self.watcher._on_message(None, None, message(robot, 'state', state))
            self.watcher._maybe_prove(robot, state)
            self.assertEqual(self.watcher._proven[robot][0], (tasks[robot], 1))
        return graph

    def offline_one(self):
        before = {resource: owner for resource, owner in self.store.list_occupancy().items()
                  if owner == 'physical-1'}
        self.watcher._on_message(None, None, message('physical-1', 'conn', b'offline'))
        self.assertIn('physical-1', self.watcher._invalidated)
        self.assertNotIn('physical-2', self.watcher._invalidated)
        self.assertEqual(before, {resource: owner for resource, owner in self.store.list_occupancy().items()
                                  if owner == 'physical-1'})
        return before

    def test_other_robot_can_advance_and_publish_on_disjoint_route(self):
        self.configure(['P', 'Q', 'R', 'U', 'V'])
        reserved = self.offline_one()
        with self.assertRaises(RecoveryError):
            self.progress.advance('physical-1', self.events['physical-1'])
        self.assertIsNone(self.progress.get_order('T1', 2))
        result = self.progress.advance('physical-2', self.events['physical-2'])
        self.assertEqual((result['status'], result['next_seq']), ('advanced', 2))
        client = FakeClient()
        published = ProgressPublisher(self.progress, OrderPublisher(client, enabled=True)).publish('T2', 2, 'boot-2')
        self.assertEqual(published['state'], 'acked')
        self.assertEqual(len(client.publications), 1)
        self.assertEqual(client.publications[0][0], 'agv/physical-2/order')
        self.assertEqual(reserved, {resource: owner for resource, owner in self.store.list_occupancy().items()
                                    if owner == 'physical-1'})

    def test_other_robot_cannot_enter_offline_robots_shared_zone(self):
        self.configure(['P', 'Q', 'R', 'S', 'T'])
        self.offline_one()
        before = self.store.list_occupancy()
        self.assertEqual(before[('zone', 'common')], 'physical-1')
        with self.assertRaises(SegmentOccupied):
            self.progress.advance('physical-2', self.events['physical-2'])
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertIsNone(self.progress.get_order('T2', 2))


if __name__ == '__main__':
    unittest.main()
