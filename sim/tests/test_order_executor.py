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


if __name__ == '__main__':
    unittest.main()
