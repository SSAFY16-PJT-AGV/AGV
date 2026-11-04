import json
from contextlib import contextmanager
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.allocation_store import AllocationStore, AllocationConflict, InvalidAllocation
from server.task_store import SqliteTaskStore


class AllocationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'allocation.sqlite3')
        self.store = AllocationStore(self.path)
        self.store.initialize()

    @staticmethod
    def segment(a, b):
        edge = json.dumps(sorted([a, b]), ensure_ascii=True, separators=(',', ':'))
        return [('node', a), ('node', b), ('edge', edge)]

    def args(self, task='T1', robot='r1', resources=None, nodes=None):
        nodes = nodes if nodes is not None else ['N1', 'N2']
        return (task, robot, 'layout@v1', nodes, nodes[-1],
                self.segment(*nodes) if resources is None else resources,
                {'type': 'unload', 'shelf': 'S1'})

    def test_allocation_and_outbox_persist_across_restart(self):
        self.store.create_task('T1')
        allocation = self.store.allocate(*self.args())
        self.assertEqual(allocation['robot_id'], 'r1')
        reopened = AllocationStore(self.path)
        self.assertEqual(reopened.get_allocation('T1')['robot_id'], 'r1')
        self.assertEqual(reopened.list_occupancy(), {r: 'r1' for r in self.segment('N1', 'N2')})
        order = reopened.get_order('T1')
        self.assertEqual(order['state'], 'pending')
        self.assertEqual(json.loads(order['payload']), {
            'order_id': 'T1', 'seq': 1, 'map_id': 'layout@v1', 'nodes': ['N1', 'N2'],
            'released_until': 'N2', 'action_at_end': {'type': 'unload', 'shelf': 'S1'}})
        reopened.mark_attempted('T1')
        self.assertEqual(AllocationStore(self.path).get_order('T1')['state'], 'uncertain')
        reopened.mark_broker_acked('T1')
        self.assertEqual(AllocationStore(self.path).get_order('T1')['state'], 'acked')

    def test_existing_task_store_sees_atomic_assignment_and_rollback(self):
        tasks = SqliteTaskStore(self.path)
        tasks.initialize()
        tasks.create('T1'); tasks.create('T2')
        self.store.allocate(*self.args())
        self.assertEqual(tasks.get('T1').status, 'ASSIGNED')
        with self.assertRaises(AllocationConflict):
            self.store.allocate(*self.args('T2', 'r2'))
        self.assertEqual(tasks.get('T2').status, 'PENDING')
        self.assertIsNone(self.store.get_allocation('T2'))

    def test_conflict_is_atomic_and_failed_task_can_retry(self):
        self.store.create_task('T1'); self.store.create_task('T2')
        self.store.allocate(*self.args(resources=self.segment('N1', 'N2') + [('zone', 'Z1')]))
        with self.assertRaises(AllocationConflict):
            self.store.allocate(*self.args('T2', 'r2', self.segment('N3', 'N4') + [('zone', 'Z1')], ['N3', 'N4']))
        self.assertIsNone(self.store.get_allocation('T2'))
        self.assertIsNone(self.store.get_order('T2'))
        self.assertNotIn(('node', 'N3'), self.store.list_occupancy())
        self.store.allocate(*self.args('T2', 'r2', nodes=['N3', 'N4']))

    def test_same_robot_cannot_receive_two_active_tasks(self):
        self.store.create_task('T1'); self.store.create_task('T2')
        self.store.allocate(*self.args())
        with self.assertRaises(AllocationConflict):
            self.store.allocate(*self.args('T2', 'r1', nodes=['N3', 'N4']))
        self.assertIsNone(self.store.get_allocation('T2'))

    def test_bad_inputs_rejected_without_allocation(self):
        self.store.create_task('T1')
        for nodes, released, resources, action in [([], 'N2', [('node', 'N2')], {}),
                (['N1', 'N2'], 'N3', [('node', 'N2')], {}),
                (['N1', 'N2'], 'N2', [], {}),
                (['N1', 'N2'], 'N2', [('unknown', 'id')], {}),
                (['N1', 'N2'], 'N2', self.segment('N1', 'N2'), {'x': float('nan')})]:
            with self.subTest(nodes=nodes, released=released, resources=resources, action=action):
                with self.assertRaises(InvalidAllocation):
                    self.store.allocate('T1', 'r1', 'layout@v1', nodes, released, resources, action)
        self.assertIsNone(self.store.get_allocation('T1'))

    def test_invalid_robot_topic_or_cyclic_action_rejected(self):
        self.store.create_task('T1')
        cyclic = {}; cyclic['self'] = cyclic
        for robot, action in [('robot/other', {}), ('r1', cyclic), ('r1', {'x': '\ud800'})]:
            with self.assertRaises(InvalidAllocation):
                self.store.allocate('T1', robot, 'layout@v1', ['N1', 'N2'], 'N2', self.segment('N1', 'N2'), action)
        self.assertIsNone(self.store.get_allocation('T1'))

    def test_missing_required_node_or_canonical_edge_cannot_be_allocated(self):
        self.store.create_task('T1')
        valid = self.segment('N1', 'N2')
        for resource in valid:
            with self.subTest(missing=resource):
                with self.assertRaises(InvalidAllocation):
                    self.store.allocate(*self.args(resources=[r for r in valid if r != resource]))
        with self.assertRaises(InvalidAllocation):
            self.store.allocate('T1', 'r1', 'layout@v1', ['N1'], 'N1', [('node', 'N1')], {})
        self.assertIsNone(self.store.get_order('T1'))
        self.assertEqual(self.store.list_occupancy(), {})

    def test_released_prefix_includes_all_its_nodes_and_edges(self):
        self.store.create_task('T1')
        nodes = ['N1', 'N2', 'N3', 'N4']
        prefix = self.segment('N1', 'N2') + self.segment('N2', 'N3')
        with self.assertRaises(InvalidAllocation):
            self.store.allocate('T1', 'r1', 'layout@v1', nodes, 'N3', self.segment('N1', 'N2'), {})
        self.store.allocate('T1', 'r1', 'layout@v1', nodes, 'N3', prefix, {})

    def test_caller_mutation_during_transaction_cannot_split_stored_snapshot(self):
        self.store.create_task('T1')
        nodes = ['N1', 'N2']
        resources = self.segment(*nodes)
        action = {'type': 'unload', 'shelf': 'S1'}
        original_write = self.store._write

        @contextmanager
        def concurrent_change():
            nodes.append('N3')
            resources.append(('zone', 'Z_changed'))
            action['type'] = 'changed'
            with original_write() as db:
                yield db

        with patch.object(self.store, '_write', concurrent_change):
            self.store.allocate('T1', 'r1', 'layout@v1', nodes, 'N2', resources, action)
        payload = json.loads(self.store.get_order('T1')['payload'])
        self.assertEqual(payload['nodes'], ['N1', 'N2'])
        self.assertEqual(self.store.get_allocation('T1')['nodes'], payload['nodes'])
        self.assertEqual(payload['action_at_end']['type'], 'unload')
        self.assertNotIn(('zone', 'Z_changed'), self.store.list_occupancy())

    def test_two_connections_competing_for_shared_zone_have_one_winner(self):
        self.store.create_task('T1'); self.store.create_task('T2')
        gate = threading.Barrier(2); results = []
        def attempt(task, robot):
            gate.wait()
            try:
                nodes = ['N1', 'N2'] if task == 'T1' else ['N3', 'N4']
                resources = self.segment(*nodes) + [('zone', 'Z1')]
                AllocationStore(self.path).allocate(*self.args(task, robot, resources, nodes))
                results.append('ok')
            except AllocationConflict:
                results.append('conflict')
        threads = [threading.Thread(target=attempt, args=x) for x in [('T1', 'r1'), ('T2', 'r2')]]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(results), ['conflict', 'ok'])
        self.assertEqual(len(self.store.list_occupancy()), 4)

    def test_outbox_state_cannot_skip_or_repeat(self):
        self.store.create_task('T1'); self.store.allocate(*self.args())
        with self.assertRaises(AllocationConflict): self.store.mark_broker_acked('T1')
        self.store.mark_attempted('T1')
        with self.assertRaises(AllocationConflict): self.store.mark_attempted('T1')
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
