"""A virtual robot only moves across explicitly released order segments."""
import json
from pathlib import Path
import tempfile
import unittest

from sim.order_executor import SimOrderExecutor
from sim.order_inbox import OrderRejected, SimOrderInbox


def test_map():
    return {'map_id': 'synthetic', 'version': 1, 'unit': 'mm',
            'nodes': [{'id': 'A', 'pos': [0, 0]}, {'id': 'B', 'pos': [1000, 0]},
                      {'id': 'C', 'pos': [2000, 0]}, {'id': 'D', 'pos': [3000, 1000]}],
            'edges': [
                {'from': 'A', 'to': 'B', 'shape': [[0, 0], [1000, 0]], 'bidirectional': True},
                {'from': 'B', 'to': 'C', 'shape': [[1000, 0], [2000, 0]], 'bidirectional': True},
                {'from': 'C', 'to': 'D', 'shape': [[2000, 0], [2500, 0], [3000, 1000]],
                 'bidirectional': True}]}


def field_map():
    positions = {'S': [-1000, 0], 'A': [0, 0], 'B': [1000, 0],
                 'C': [2000, 0], 'D': [1000, 1000]}
    edges = []
    for edge_id, source, target in [('E0', 'S', 'A'), ('E1', 'A', 'B'),
                                    ('E2', 'B', 'C'), ('E3', 'C', 'B'), ('E4', 'B', 'D')]:
        edges.append({'id': edge_id, 'from': source, 'to': target,
                      'shape': [positions[source], positions[target]], 'bidirectional': False,
                      'lanes': 1, 'length_mm': 1000})
    return {'map_id': 'synthetic', 'version': 1, 'unit': 'mm', 'schema': 'agv-map-v1',
            'nodes': [dict(id=node, pos=point, **({'crossing': True,
                       'through': [['E1', 'E2'], ['E3', 'E4']]} if node == 'B' else {}))
                      for node, point in positions.items()], 'edges': edges}


def payload(seq=1, stop='B', nodes=None, order_id='task-1', map_id='synthetic@v1'):
    return json.dumps({'order_id': order_id, 'seq': seq, 'map_id': map_id,
                       'nodes': nodes or ['A', 'B', 'C', 'D'], 'released_until': stop,
                       'action_at_end': {'type': 'load'}}).encode('utf-8')


class SimOrderExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = SimOrderInbox(Path(self.tmp.name) / 'robot.sqlite3', 'sim-r1', 'synthetic@v1')
        self.inbox.initialize()
        self.executor = SimOrderExecutor(self.inbox, test_map(), 'A')

    def test_never_passes_released_node_even_with_large_tick(self):
        receipt = self.executor.accept(payload())
        self.assertTrue(receipt.new)
        mid = self.executor.tick(0.5)
        self.assertEqual(mid['status'], 'moving')
        self.assertAlmostEqual(mid['pose']['x'], 500)
        stopped = self.executor.tick(100)
        self.assertEqual((stopped['status'], stopped['current_node']), ('stopped', 'B'))
        self.assertEqual(stopped['pose']['x'], 1000)
        self.assertEqual(stopped['order']['released_until'], 'B')
        self.assertEqual(stopped['receipt']['digest'], receipt.digest)
        self.assertEqual(self.executor.tick(100)['pose']['x'], 1000)

    def test_duplicate_does_not_restart_and_changed_payload_is_rejected(self):
        first = self.executor.accept(payload())
        self.executor.tick(2)
        duplicate = self.executor.accept(payload())
        self.assertFalse(duplicate.new)
        self.assertEqual(self.executor.snapshot()['status'], 'stopped')
        self.assertEqual(first.digest, duplicate.digest)
        with self.assertRaises(OrderRejected):
            self.executor.accept(payload(stop='C'))
        self.assertEqual(self.executor.snapshot()['current_node'], 'B')

    def test_next_sequence_resumes_from_previous_stop(self):
        self.executor.accept(payload())
        self.executor.tick(2)
        second = self.executor.accept(payload(seq=2, stop='D'))
        self.assertTrue(second.new)
        self.assertEqual(self.executor.snapshot()['pose']['x'], 1000)
        result = self.executor.tick(100)
        self.assertEqual((result['status'], result['current_node']), ('stopped', 'D'))
        self.assertEqual(result['pose']['x'], 3000)
        self.assertEqual(result['pose']['y'], 1000)

    def test_bad_route_and_map_are_rejected_before_inbox_changes(self):
        for bad in (payload(nodes=['A', 'C', 'D']), payload(map_id='elsewhere@v1'),
                    payload(nodes=['C', 'B', 'A'], stop='A')):
            with self.subTest(bad=bad), self.assertRaises(OrderRejected):
                self.executor.accept(bad)
        self.assertTrue(self.executor.accept(payload()).new)

    def test_directed_true_never_creates_reverse_simulated_edge(self):
        data = test_map()
        data['edges'][0]['directed'] = True
        executor = SimOrderExecutor(self.inbox, data, 'B')
        with self.assertRaises(OrderRejected):
            executor.accept(payload(stop='A', nodes=['B', 'A']))
        self.assertEqual(executor.snapshot()['status'], 'idle')
        data['edges'][0]['directed'] = 'true'
        with self.assertRaises(ValueError):
            SimOrderExecutor(self.inbox, data, 'B')

    def test_out_of_order_and_new_while_moving_rejected(self):
        with self.assertRaises(OrderRejected):
            self.executor.accept(payload(seq=2, stop='C'))
        self.executor.accept(payload())
        with self.assertRaises(OrderRejected):
            self.executor.accept(payload(seq=2, stop='C'))
        self.assertEqual(self.executor.snapshot()['status'], 'moving')

    def test_bad_elapsed_time_and_snapshot_copy(self):
        self.executor.accept(payload())
        for elapsed in (-1, float('inf'), float('nan'), True):
            with self.subTest(elapsed=elapsed), self.assertRaises(ValueError):
                self.executor.tick(elapsed)
        snapshot = self.executor.snapshot()
        snapshot['pose']['x'] = -100
        snapshot['order']['nodes'][0] = 'bad'
        self.assertEqual(self.executor.snapshot()['pose']['x'], 0)
        self.assertEqual(self.executor.snapshot()['order']['nodes'][0], 'A')

    def test_repeated_stop_visits_are_executed_in_order(self):
        data = test_map()
        data['edges'].append({'from': 'B', 'to': 'D', 'shape': [[1000, 0], [3000, 1000]],
                              'bidirectional': True})
        executor = SimOrderExecutor(self.inbox, data, 'A')
        route = ['A', 'B', 'C', 'B', 'D']
        executor.accept(payload(seq=1, stop='B', nodes=route))
        self.assertEqual(executor.tick(2)['current_node'], 'B')
        executor.accept(payload(seq=2, stop='B', nodes=route))
        self.assertEqual(executor.tick(1)['current_node'], 'C')
        state = executor.tick(1)
        self.assertEqual((state['current_node'], state['status']), ('B', 'stopped'))
        executor.accept(payload(seq=3, stop='D', nodes=route))
        self.assertEqual(executor.tick(10)['current_node'], 'D')

    def test_reboot_never_guesses_repeated_intermediate_stop(self):
        route = ['A', 'B', 'C', 'B', 'D']
        data = test_map()
        data['edges'].append({'from': 'B', 'to': 'D', 'shape': [[1000, 0], [3000, 1000]],
                              'bidirectional': True})
        executor = SimOrderExecutor(self.inbox, data, 'A')
        executor.accept(payload(seq=1, stop='B', nodes=route))
        executor.tick(2)
        reopened = SimOrderInbox(self.inbox.path, 'sim-r1', 'synthetic@v1')
        reopened.initialize()
        fresh = SimOrderExecutor(reopened, data, 'B')
        with self.assertRaises(OrderRejected):
            fresh.accept(payload(seq=2, stop='B', nodes=route))
        self.assertEqual(fresh.snapshot()['status'], 'idle')

    def test_field_map_requires_explicit_direction_and_simulation_opt_in(self):
        for kwargs in ({}, {'experimental_field_map': True},
                       {'field_incoming_edge': 'E0'},
                       {'experimental_field_map': True, 'field_incoming_edge': 'E1'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SimOrderExecutor(self.inbox, field_map(), 'A', **kwargs)

    def test_field_opt_in_accepts_only_exact_planned_repeated_route(self):
        executor = SimOrderExecutor(self.inbox, field_map(), 'A',
                                    experimental_field_map=True, field_incoming_edge='E0')
        with self.assertRaises(OrderRejected):
            executor.accept(payload(seq=1, stop='B', nodes=['A', 'B', 'D']))
        route = ['A', 'B', 'C', 'B', 'D']
        executor.accept(payload(seq=1, stop='B', nodes=route))
        stopped = executor.tick(2)
        self.assertEqual((stopped['current_node'], stopped['visit_index'], stopped['released_index']), ('B', 1, 1))
        executor.accept(payload(seq=2, stop='B', nodes=route))
        state = executor.tick(1)
        self.assertEqual((state['current_node'], state['visit_index'], state['released_index']), ('C', 2, 3))
        state = executor.tick(1)
        self.assertEqual((state['current_node'], state['visit_index'], state['released_index']), ('B', 3, 3))
        executor.accept(payload(seq=3, stop='D', nodes=route))
        self.assertEqual(executor.tick(2)['current_node'], 'D')

    def test_field_opt_in_reboot_cannot_resume_without_prior_execution(self):
        data = field_map()
        executor = SimOrderExecutor(self.inbox, data, 'A',
                                    experimental_field_map=True, field_incoming_edge='E0')
        route = ['A', 'B', 'C', 'B', 'D']
        executor.accept(payload(seq=1, stop='B', nodes=route))
        executor.tick(2)
        reopened = SimOrderInbox(self.inbox.path, 'sim-r1', 'synthetic@v1')
        reopened.initialize()
        fresh = SimOrderExecutor(reopened, data, 'B',
                                 experimental_field_map=True, field_incoming_edge='E1')
        with self.assertRaises(OrderRejected):
            fresh.accept(payload(seq=2, stop='B', nodes=route))
        self.assertEqual(fresh.snapshot()['status'], 'idle')

    def test_field_map_or_cached_route_tamper_cannot_move(self):
        for index, corrupt in enumerate(('map', 'route_edges', 'visit_mapping', 'release_index',
                                         'canonical_guard', 'incoming_edge')):
            with self.subTest(corrupt=corrupt):
                inbox = SimOrderInbox(Path(self.tmp.name) / ('field-%d.sqlite3' % index),
                                      'sim-r1', 'synthetic@v1')
                inbox.initialize()
                executor = SimOrderExecutor(inbox, field_map(), 'A', experimental_field_map=True,
                                            field_incoming_edge='E0')
                executor.accept(payload(seq=1, stop='B', nodes=['A', 'B', 'C', 'B', 'D']))
                before = executor.snapshot()['pose']
                if corrupt == 'map':
                    executor._field_map['version'] = 99
                elif corrupt == 'route_edges':
                    executor._field_route_edges = ('E4',)
                elif corrupt == 'visit_mapping':
                    executor._waypoint_visits = (3,)
                elif corrupt == 'release_index':
                    executor._release_index = 3
                elif corrupt == 'canonical_guard':
                    executor._field_map_canonical = None
                else:
                    executor._field_incoming_edge = 'E1'
                with self.assertRaises(OrderRejected):
                    executor.tick(1)
                self.assertEqual(executor.snapshot()['pose'], before)

    def test_field_pose_must_remain_finite_and_on_current_waypoint_segment(self):
        corruptions = [(5000.0, 5000.0, 0.0), (500.0, 1.0, 0.0),
                       (-1.0, 0.0, 0.0), (1001.0, 0.0, 0.0),
                       (float('nan'), 0.0, 0.0), (float('inf'), 0.0, 0.0),
                       (500.0, 0.0, float('nan'))]
        for index, (x, y, yaw) in enumerate(corruptions):
            with self.subTest(pose=(x, y, yaw)):
                inbox = SimOrderInbox(Path(self.tmp.name) / ('pose-%d.sqlite3' % index),
                                      'sim-r1', 'synthetic@v1')
                inbox.initialize()
                executor = SimOrderExecutor(inbox, field_map(), 'A', experimental_field_map=True,
                                            field_incoming_edge='E0')
                executor.accept(payload(seq=1, stop='B', nodes=['A', 'B', 'C', 'B', 'D']))
                executor.tick(.5)
                executor._x, executor._y, executor._yaw = x, y, yaw
                cursor = executor._next_waypoint
                with self.assertRaises(OrderRejected):
                    executor.tick(1)
                self.assertEqual(executor._next_waypoint, cursor)

    def test_field_idle_pose_must_match_start_before_order_persisted(self):
        executor = SimOrderExecutor(self.inbox, field_map(), 'A',
                                    experimental_field_map=True, field_incoming_edge='E0')
        executor._x = 2000.0
        with self.assertRaises(OrderRejected):
            executor.accept(payload(seq=1, stop='B', nodes=['A', 'B', 'C', 'B', 'D']))
        self.assertIsNone(executor.snapshot()['order'])


if __name__ == '__main__':
    unittest.main()
