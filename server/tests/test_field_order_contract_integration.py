"""Field-mode orders must fit the team's parser before reserving or advancing."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from server.allocation_store import AllocationStore
from server.coordinator import Coordinator
from server.field_dispatch import FieldCandidate
from server.field_robot_contract import MAX_FIELD_ORDER_BYTES, validate_field_robot_order
from server.route_occurrence import occurrence_clearance, plan_occurrence_segment
from server.segment_progress import SegmentProgress
from server.tests.test_coordinator import FakePublisher
from server.tests.test_occurrence_allocation_lab import laboratory_map
from server.tests.test_segment_progress import FakeGate


class FieldOrderContractIntegrationTests(unittest.TestCase):
    def test_explicit_field_mode_with_plain_map_advances(self):
        data = laboratory_map()
        for node in data['nodes']:
            node.pop('crossing', None)
            node.pop('through', None)
        data['zones'] = [{'id': 'wait-b', 'type': 'waiting', 'node': 'B'},
                         {'id': 'wait-c', 'type': 'waiting', 'node': 'C'}]
        with tempfile.TemporaryDirectory() as folder:
            store = AllocationStore(Path(folder) / 'db.sqlite3', field_required=True)
            store.initialize()
            store.create_task('T1')
            coordinator = Coordinator(store, data, FakePublisher(), field_simulation=True)
            allocation = coordinator.prepare_field('T1', 'C',
                [FieldCandidate('sim-r1', 'A', 'E0', True)], {'type': 'none'})
            self.assertEqual(allocation['nodes'], ['A', 'B', 'C'])
            coordinator.publish('T1')
            gate = FakeGate(store)
            gate.expected = ('T1', 'boot-1', 1, 'B')
            progress = SegmentProgress(store, data, simulation_enabled=True,
                                       field_simulation=True, recovery_gate=gate)
            progress.initialize()
            event = {'type': 'rear_clear', 'event_id': 'e1', 'robot_session': 'boot-1',
                     'order_id': 'T1', 'seq': 1, 'map_id': 'lab@v1', 'stopped_at': 'B',
                     'cleared_resources': [list(x) for x in occurrence_clearance(data, allocation['nodes'], 0)]}
            result = progress.advance('sim-r1', event)
            self.assertEqual((result['status'], result['next_seq']), ('advanced', 2))
            self.assertEqual(store.get_allocation('T1')['released_until'], 'C')

    def test_direct_field_store_allocation_enforces_team_limits(self):
        data = laboratory_map()
        canonical = json.dumps(data, ensure_ascii=True, sort_keys=True,
                               allow_nan=False, separators=(',', ':')).encode('ascii')
        fingerprint = hashlib.sha256(canonical).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            store = AllocationStore(Path(folder) / 'db.sqlite3', field_required=True)
            store.initialize()
            tid = 'T' * 129
            store.create_task(tid)
            segment = plan_occurrence_segment(data, ['A', 'B'], 0)
            with self.assertRaises(ValueError):
                store.allocate(tid, 'sim-r1', 'lab@v1', ['A', 'B'], 'B', segment.resources,
                    {'type': 'none'}, map_fingerprint=fingerprint,
                    field_context={'map_data': data, 'incoming_edge': 'E0', 'goal': 'B'})
            self.assertIsNone(store.get_allocation(tid))
            self.assertEqual(store.list_occupancy(), {})

    def test_initial_field_orders_are_checked_before_reservation(self):
        for goal in ('B', 'D'):  # unique and repeated routes
            for tid, action in [('T' * 129, {'type': 'none'}),
                                ('T1', {'type': 'none', 'padding': 'x' * MAX_FIELD_ORDER_BYTES})]:
                with self.subTest(goal=goal, long_id=len(tid) > 128), tempfile.TemporaryDirectory() as folder:
                    store = AllocationStore(Path(folder) / 'db.sqlite3', field_required=True)
                    store.initialize()
                    store.create_task(tid)
                    coordinator = Coordinator(store, laboratory_map(), FakePublisher(), field_simulation=True)
                    with self.assertRaises(ValueError):
                        coordinator.prepare_field(tid, goal, [FieldCandidate('sim-r1', 'A', 'E0', True)], action)
                    self.assertIsNone(store.get_allocation(tid))
                    self.assertEqual(store.list_occupancy(), {})

    def test_oversized_next_order_rolls_back_the_reservation_transition(self):
        data = laboratory_map()
        long_goal = 'Y' * 32
        for node in data['nodes']:
            if node['id'] == 'Y':
                node['id'] = long_goal
        for edge in data['edges']:
            if edge['to'] == 'Y':
                edge['to'] = long_goal
        data['zones'] = [{'id': 'wait-c', 'type': 'waiting', 'node': 'C'},
                         {'id': 'wait-y', 'type': 'waiting', 'node': long_goal}]
        with tempfile.TemporaryDirectory() as folder:
            store = AllocationStore(Path(folder) / 'db.sqlite3', field_required=True)
            store.initialize()
            store.create_task('T1')
            path = ['X', 'C', long_goal]
            initial = {'order_id': 'T1', 'seq': 1, 'map_id': 'lab@v1', 'nodes': path,
                       'released_until': 'C', 'action_at_end': {'type': 'none', 'padding': ''}}
            raw = json.dumps(initial, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            action = {'type': 'none', 'padding': 'x' * (MAX_FIELD_ORDER_BYTES - len(raw))}
            initial['action_at_end'] = action
            validate_field_robot_order(initial)
            publisher = FakePublisher()
            coordinator = Coordinator(store, data, publisher, field_simulation=True)
            allocation = coordinator.prepare_field('T1', long_goal,
                [FieldCandidate('sim-r1', 'X', 'E5', True)], action)
            coordinator.publish('T1')
            gate = FakeGate(store)
            gate.expected = ('T1', 'boot-1', 1, 'C')
            progress = SegmentProgress(store, data, simulation_enabled=True,
                                       field_simulation=True, recovery_gate=gate)
            progress.initialize()
            before = store.list_occupancy()
            event = {'type': 'rear_clear', 'event_id': 'e1', 'robot_session': 'boot-1',
                     'order_id': 'T1', 'seq': 1, 'map_id': 'lab@v1', 'stopped_at': 'C',
                     'cleared_resources': [list(x) for x in occurrence_clearance(data, path, 0)]}
            with self.assertRaises(ValueError):
                progress.advance('sim-r1', event)
            self.assertEqual(store.list_occupancy(), before)
            self.assertEqual(store.get_allocation('T1'), allocation)
            self.assertIsNone(progress.get_order('T1', 2))


if __name__ == '__main__':
    unittest.main()
