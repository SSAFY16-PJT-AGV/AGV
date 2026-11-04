import json
from contextlib import closing, contextmanager
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.allocation_store import AllocationStore, AllocationConflict, InvalidAllocation
from server.task_store import SqliteTaskStore
from server.tasks import StateConflictError


class FixtureAllocationStore(AllocationStore):
    def allocate(self, *args, **kwargs):
        kwargs.setdefault('map_fingerprint', 'a' * 64)
        return super().allocate(*args, **kwargs)


class AllocationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'allocation.sqlite3')
        self.store = FixtureAllocationStore(self.path)
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

    def expected(self, task='T1'):
        allocation = self.store.get_allocation(task)
        payload = self.store.get_order(task)['payload']
        resources = {r for r, owner in self.store.list_occupancy().items()
                     if owner == allocation['robot_id']}
        return payload, allocation, resources

    def test_allocation_and_outbox_persist_across_restart(self):
        self.store.create_task('T1')
        allocation = self.store.allocate(*self.args())
        self.assertEqual(allocation['robot_id'], 'r1')
        reopened = FixtureAllocationStore(self.path)
        self.assertEqual(reopened.get_allocation('T1')['robot_id'], 'r1')
        self.assertEqual(reopened.list_occupancy(), {r: 'r1' for r in self.segment('N1', 'N2')})
        order = reopened.get_order('T1')
        self.assertEqual(order['state'], 'pending')
        self.assertEqual(json.loads(order['payload']), {
            'order_id': 'T1', 'seq': 1, 'map_id': 'layout@v1', 'nodes': ['N1', 'N2'],
            'released_until': 'N2', 'action_at_end': {'type': 'unload', 'shelf': 'S1'}})
        reopened.mark_attempted('T1', *self.expected())
        self.assertEqual(AllocationStore(self.path).get_order('T1')['state'], 'uncertain')
        reopened.mark_broker_acked('T1', *self.expected())
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
                FixtureAllocationStore(self.path).allocate(*self.args(task, robot, resources, nodes))
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
        with self.assertRaises(AllocationConflict): self.store.mark_broker_acked('T1', *self.expected())
        expected = self.expected()
        self.store.mark_attempted('T1', *expected)
        with self.assertRaises(AllocationConflict): self.store.mark_attempted('T1', *expected)
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')

    def test_cancelled_task_cannot_be_marked_attempted(self):
        self.store.create_task('T1'); self.store.allocate(*self.args())
        expected = self.expected()
        SqliteTaskStore(self.path).transition('T1', 'ASSIGNED', 'CANCELLED')
        with self.assertRaises(AllocationConflict):
            self.store.mark_attempted('T1', *expected)
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')
        self.assertTrue(self.store.list_occupancy())

    def test_missing_occupancy_or_changed_payload_prevents_attempt(self):
        self.store.create_task('T1'); self.store.allocate(*self.args())
        payload, allocation, resources = self.expected()
        with self.assertRaises(AllocationConflict):
            self.store.mark_attempted('T1', payload + ' ', allocation, resources)
        altered = dict(allocation, robot_id='other')
        with self.assertRaises(AllocationConflict):
            self.store.mark_attempted('T1', payload, altered, resources)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM allocation_occupancy WHERE kind='node' AND resource_id='N1'")
            db.commit()
        with self.assertRaises(AllocationConflict):
            self.store.mark_attempted('T1', payload, allocation, resources)
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')

    def test_attempted_and_acked_order_blocks_task_state_transition(self):
        self.store.create_task('T1'); self.store.allocate(*self.args())
        self.store.mark_attempted('T1', *self.expected())
        tasks = SqliteTaskStore(self.path)
        with self.assertRaises(StateConflictError):
            tasks.transition('T1', 'ASSIGNED', 'CANCELLED')
        self.store.mark_broker_acked('T1', *self.expected())
        with self.assertRaises(StateConflictError):
            tasks.transition('T1', 'ASSIGNED', 'PICKING')
        self.assertEqual(tasks.get('T1').status, 'ASSIGNED')
        self.assertTrue(self.store.list_occupancy())

    def test_cancel_and_attempt_race_only_one_can_win(self):
        self.store.create_task('T1'); self.store.allocate(*self.args())
        expected = self.expected(); gate = threading.Barrier(2); results = []
        def attempt():
            gate.wait()
            try:
                AllocationStore(self.path).mark_attempted('T1', *expected)
                results.append('attempt')
            except AllocationConflict:
                results.append('attempt_conflict')
        def cancel():
            gate.wait()
            try:
                SqliteTaskStore(self.path).transition('T1', 'ASSIGNED', 'CANCELLED')
                results.append('cancel')
            except StateConflictError:
                results.append('cancel_conflict')
        threads = [threading.Thread(target=attempt), threading.Thread(target=cancel)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertIn(set(results), [{'attempt', 'cancel_conflict'}, {'cancel', 'attempt_conflict'}])
        status = SqliteTaskStore(self.path).get('T1').status
        outbox = self.store.get_order('T1')['state']
        self.assertIn((status, outbox), [('CANCELLED', 'pending'), ('ASSIGNED', 'uncertain')])
        self.assertTrue(self.store.list_occupancy())

    def test_ack_rejects_mutated_order_or_missing_occupancy(self):
        for kind in ('payload', 'resource', 'fingerprint', 'status', 'allocation'):
            with self.subTest(kind=kind):
                task = 'task-' + kind
                nodes = ['a-' + kind, 'b-' + kind]
                resources = self.segment(*nodes)
                self.store.create_task(task)
                self.store.allocate(*self.args(task, 'robot-' + kind, resources, nodes))
                expected = self.expected(task)
                self.store.mark_attempted(task, *expected)
                with closing(sqlite3.connect(self.path)) as db:
                    if kind == 'payload':
                        db.execute('UPDATE allocation_outbox SET payload=? WHERE task_id=?', ('{}', task))
                    elif kind == 'resource':
                        db.execute('DELETE FROM allocation_occupancy WHERE task_id=? AND kind=?', (task, 'edge'))
                    elif kind == 'fingerprint':
                        db.execute("UPDATE allocation_meta SET value=? WHERE key='map_fingerprint'", ('b' * 64,))
                    elif kind == 'status':
                        db.execute("UPDATE tasks SET status='CANCELLED' WHERE task_id=?", (task,))
                    else:
                        db.execute("UPDATE allocation_jobs SET released_until=? WHERE task_id=?", (nodes[0], task))
                    db.commit()
                with self.assertRaises(AllocationConflict):
                    self.store.mark_broker_acked(task, *expected)
                self.assertEqual(self.store.get_order(task)['state'], 'uncertain')
                if kind == 'fingerprint':
                    with closing(sqlite3.connect(self.path)) as db:
                        db.execute("UPDATE allocation_meta SET value=? WHERE key='map_fingerprint'", ('a' * 64,))
                        db.commit()

    def test_legacy_task_store_without_outbox_table_still_transitions(self):
        path = str(Path(self.tmp.name) / 'legacy.sqlite3')
        tasks = SqliteTaskStore(path); tasks.initialize(); tasks.create('legacy')
        tasks.transition('legacy', 'PENDING', 'ASSIGNED')
        self.assertEqual(tasks.get('legacy').status, 'ASSIGNED')

    def test_first_map_is_locked_and_survives_reopening(self):
        self.store.create_task('T1'); self.store.create_task('T2')
        self.store.allocate(*self.args())
        original_occupancy = self.store.list_occupancy()
        reopened = FixtureAllocationStore(self.path)
        reopened.initialize()
        different_map = list(self.args('T2', 'r2', nodes=['N3', 'N4']))
        different_map[2] = 'layout@v2'
        with self.assertRaises(AllocationConflict):
            reopened.allocate(*different_map)
        self.assertIsNone(reopened.get_allocation('T2'))
        self.assertIsNone(reopened.get_order('T2'))
        self.assertEqual(reopened.list_occupancy(), original_occupancy)
        self.assertEqual(SqliteTaskStore(self.path).get('T2').status, 'PENDING')
        reopened.allocate(*self.args('T2', 'r2', nodes=['N3', 'N4']))

    def test_unversioned_or_incomplete_map_identity_is_rejected(self):
        self.store.create_task('T1')
        for invalid_map in ('layout', 'layout@', '@v1', 'layout@4', 'layout@v', 'layout@v-1', 'layout@v１'):
            args = list(self.args()); args[2] = invalid_map
            with self.subTest(map_id=invalid_map):
                with self.assertRaises(InvalidAllocation):
                    self.store.allocate(*args)
        self.assertIsNone(self.store.get_allocation('T1'))
        self.assertEqual(self.store.list_occupancy(), {})

    def test_concurrent_first_allocations_on_different_maps_have_one_winner(self):
        self.store.create_task('T1'); self.store.create_task('T2')
        gate = threading.Barrier(2); results = []
        def attempt(task, robot, nodes, map_id):
            gate.wait()
            args = list(self.args(task, robot, nodes=nodes)); args[2] = map_id
            try:
                FixtureAllocationStore(self.path).allocate(*args)
                results.append('ok')
            except AllocationConflict:
                results.append('conflict')
        threads = [threading.Thread(target=attempt, args=x) for x in [
            ('T1', 'r1', ['N1', 'N2'], 'layout@v1'),
            ('T2', 'r2', ['N3', 'N4'], 'layout@v2')]]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(results), ['conflict', 'ok'])
        self.assertEqual(len(self.store.list_occupancy()), 3)

    def test_legacy_allocation_missing_fingerprint_is_not_trusted(self):
        self.store.create_task('T1'); self.store.create_task('T2')
        self.store.allocate(*self.args())
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM allocation_meta WHERE key='map_fingerprint'"); db.commit()
        reopened = FixtureAllocationStore(self.path)
        with self.assertRaises(AllocationConflict):
            reopened.initialize()
        with self.assertRaises(AllocationConflict):
            reopened.allocate(*self.args('T2', 'r2', nodes=['N3', 'N4']))
        self.assertIsNone(reopened.get_order('T2'))

    def test_inconsistent_existing_allocations_are_rejected(self):
        self.store.create_task('T1'); self.store.create_task('T2')
        self.store.allocate(*self.args())
        self.store.allocate(*self.args('T2', 'r2', nodes=['N3', 'N4']))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE allocation_jobs SET map_id='layout@v2' WHERE task_id='T2'"); db.commit()
        with self.assertRaises(AllocationConflict):
            AllocationStore(self.path).initialize()

    def test_same_version_different_fingerprint_fails_and_survives_restart(self):
        self.store.create_task('T1'); self.store.create_task('T2')
        self.store.allocate(*self.args())
        reopened = FixtureAllocationStore(self.path); reopened.initialize()
        with self.assertRaises(AllocationConflict):
            reopened.allocate(*self.args('T2', 'r2', nodes=['N3', 'N4']), map_fingerprint='b' * 64)
        self.assertIsNone(reopened.get_allocation('T2'))
        self.assertEqual(SqliteTaskStore(self.path).get('T2').status, 'PENDING')
        reopened.allocate(*self.args('T2', 'r2', nodes=['N3', 'N4']))

    def test_fingerprint_change_between_check_and_publish_rejects_attempt(self):
        self.store.create_task('T1'); self.store.allocate(*self.args())
        expected = self.expected()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE allocation_meta SET value=? WHERE key='map_fingerprint'", ('b' * 64,))
            db.commit()
        with self.assertRaises(AllocationConflict):
            self.store.mark_attempted('T1', *expected)
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')

    def test_fingerprint_is_mandatory_lowercase_sha256(self):
        self.store.create_task('T1')
        raw = AllocationStore(self.path)
        with self.assertRaises(TypeError): raw.allocate(*self.args())
        for bad in ('', 'x' * 64, 'A' * 64, 'a' * 63, 1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidAllocation):
                    raw.allocate(*self.args(), map_fingerprint=bad)
        self.assertIsNone(self.store.get_allocation('T1'))


if __name__ == '__main__':
    unittest.main()
