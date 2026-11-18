"""Isolated simulated reservations; never a real-robot drive permit."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import copy
import sqlite3
import tempfile
import threading
import unittest

from server.occurrence_allocation_lab import LabConflict, OccurrenceAllocationLab
from server.route_occurrence import occurrence_clearance


def laboratory_map():
    nodes = [{'id': name, 'pos': list(pos)} for name, pos in {
        'S': (-1, 0), 'A': (0, 0), 'B': (1, 0), 'C': (2, 0), 'D': (1, 1),
        'W': (3, -1), 'X': (3, 0), 'Y': (3, 1)}.items()]
    next(node for node in nodes if node['id'] == 'B').update(
        crossing=True, through=[['E1', 'E2'], ['E3', 'E4']])
    positions = {node['id']: node['pos'] for node in nodes}
    edges = [{'id': eid, 'from': a, 'to': b, 'shape': [positions[a], positions[b]],
              'length_mm': 1, 'lanes': 1, 'bidirectional': False}
             for eid, a, b in [('E0', 'S', 'A'), ('E1', 'A', 'B'), ('E2', 'B', 'C'),
                               ('E3', 'C', 'B'), ('E4', 'B', 'D'),
                               ('E5', 'W', 'X'), ('E6', 'X', 'C'), ('E7', 'C', 'Y')]]
    return {'map_id': 'lab', 'version': 1, 'unit': 'mm', 'nodes': nodes, 'edges': edges,
            'zones': [{'id': 'wait-b', 'type': 'waiting', 'node': 'B'},
                      {'id': 'wait-d', 'type': 'waiting', 'node': 'D'},
                      {'id': 'wait-y', 'type': 'waiting', 'node': 'Y'},
                      {'id': 'shared-c', 'type': 'shared', 'nodes': ['C']}]}


class OccurrenceAllocationLabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'experiment.sqlite3'
        self.map = laboratory_map()
        self.lab = OccurrenceAllocationLab(self.path, self.map, simulation=True)
        self.lab.initialize()

    def start_a(self):
        return self.lab.start('A-task', 'sim-a', 'session-a', 'E0', 'D')

    def ack(self, task, seq, session):
        self.lab.mark_attempted(task, seq, session)
        self.lab.mark_broker_acked(task, seq, session)

    def event(self, job, event_id):
        order = job['orders'][-1]
        return {'type': 'rear_clear', 'event_id': event_id, 'robot_session': job['session_id'],
                'order_id': job['task_id'], 'seq': order['seq'], 'map_id': order['map_id'],
                'stopped_at': order['released_until'],
                'cleared_resources': [list(x) for x in occurrence_clearance(
                    self.map, order['nodes'], job['start_index'])]}

    def advance(self, robot, event, **kw):
        return self.lab.advance_simulated(robot, event, generation=self.lab.generation, **kw)

    def test_repeated_waiting_node_advances_by_occurrence_and_duplicate_is_idempotent(self):
        job = self.start_a()
        self.assertEqual(job['orders'][0]['nodes'], ['A', 'B', 'C', 'B', 'D'])
        self.assertEqual((job['start_index'], job['end_index']), (0, 1))
        self.ack('A-task', 1, 'session-a')
        first = self.event(job, 'e1')
        moved = self.advance('sim-a', first)
        self.assertEqual((moved['start_index'], moved['end_index']), (1, 3))
        self.assertEqual(moved['orders'][-1]['released_until'], 'B')
        duplicate = self.advance('sim-a', first)
        self.assertEqual(duplicate['orders'], moved['orders'])
        changed = copy.deepcopy(first); changed['stopped_at'] = 'C'
        with self.assertRaises(LabConflict):
            self.advance('sim-a', changed)
        self.assertEqual(self.lab.snapshot('A-task')['end_index'], 3)

    def test_shared_zone_conflict_rolls_back_all_changes_and_allows_retry(self):
        job_a = self.start_a()
        job_b = self.lab.start('B-task', 'sim-b', 'session-b', 'E5', 'Y')
        self.ack('A-task', 1, 'session-a'); self.ack('B-task', 1, 'session-b')
        event_a = self.event(job_a, 'a1')
        before_a = self.lab.snapshot('A-task'); before_b = self.lab.snapshot('B-task')
        with self.assertRaises(LabConflict):
            self.advance('sim-a', event_a)
        self.assertEqual(self.lab.snapshot('A-task'), before_a)
        self.assertEqual(self.lab.snapshot('B-task'), before_b)
        self.advance('sim-b', self.event(job_b, 'b1'))
        self.assertEqual(self.advance('sim-a', event_a)['end_index'], 3)

    def test_stale_or_uncertain_event_does_not_advance_or_release(self):
        job = self.start_a()
        event = self.event(job, 'e1')
        self.lab.mark_attempted('A-task', 1, 'session-a')
        before = self.lab.snapshot('A-task')
        for candidate, kwargs in [(event, {}), (dict(event, robot_session='old'), {}),
                                  (dict(event, seq=2), {}), (event, {'retained': True})]:
            with self.subTest(candidate=candidate, kwargs=kwargs), self.assertRaises(LabConflict):
                self.advance('sim-a', candidate, **kwargs)
        self.assertEqual(self.lab.snapshot('A-task'), before)

    def test_restart_blocks_and_fences_old_instance_without_dropping_occupancy(self):
        job = self.start_a(); self.ack('A-task', 1, 'session-a')
        event = self.event(job, 'e1')
        before = self.lab.snapshot('A-task')
        restarted = OccurrenceAllocationLab(self.path, self.map, simulation=True)
        restarted.initialize()
        after = restarted.snapshot('A-task')
        self.assertEqual(after['occupancy'], before['occupancy'])
        self.assertEqual(after['status'], 'blocked')
        with self.assertRaises(LabConflict):
            self.advance('sim-a', event)
        with self.assertRaises(LabConflict):
            restarted.advance_simulated('sim-a', event, generation=restarted.generation)
        # An unresolved pre-restart robot blocks admission of another task,
        # even when its first resources do not overlap the held segment.
        with self.assertRaises(LabConflict):
            restarted.start('B-task', 'sim-b', 'session-b', 'E5', 'Y')

    def test_two_concurrent_identical_events_produce_only_one_next_order(self):
        job = self.start_a(); self.ack('A-task', 1, 'session-a')
        event = self.event(job, 'same')
        barrier = threading.Barrier(2)
        def run(_):
            barrier.wait(timeout=5)
            return self.advance('sim-a', event)
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(run, (0, 1)))
        self.assertTrue(all(x['end_index'] == 3 for x in result))
        self.assertEqual(len(self.lab.snapshot('A-task')['orders']), 2)

    def test_stored_event_generation_must_be_valid(self):
        job = self.start_a(); self.ack('A-task', 1, 'session-a')
        self.advance('sim-a', self.event(job, 'e1'))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE lab_events SET generation=?', ('z' * 64,))
        with self.assertRaises(LabConflict):
            self.lab.snapshot('A-task')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE lab_events SET generation=?', ('b' * 64,))
        with self.assertRaises(LabConflict):
            self.lab.snapshot('A-task')


if __name__ == '__main__':
    unittest.main()
