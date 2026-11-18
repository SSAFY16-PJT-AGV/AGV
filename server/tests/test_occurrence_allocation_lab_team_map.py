"""Synthetic two-AGV SQLite handoff on a private copy of the team map.

Main-line waiting zones and rear-clear events here are test assumptions.
Nothing in this test commands a robot or verifies physical clearance.
"""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from server.occurrence_allocation_lab import LabConflict, OccurrenceAllocationLab
from server.route_occurrence import occurrence_clearance


@unittest.skipUnless(os.getenv('AGV_FIELD_REPO'), 'set AGV_FIELD_REPO to team field-run checkout')
class TeamMapAtomicHandoffTests(unittest.TestCase):
    def test_shared_crossing_and_repeated_node_are_atomic(self):
        root = Path(os.environ['AGV_FIELD_REPO'])
        data = json.loads((root / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
        data = copy.deepcopy(data)
        for node in ('M3', 'P_green', 'P_purple'):
            data['zones'].append({'id': 'test-wait-' + node, 'type': 'waiting', 'node': node})
        with tempfile.TemporaryDirectory() as directory:
            lab = OccurrenceAllocationLab(Path(directory) / 'test.sqlite3', data, simulation=True)
            lab.initialize()

            def ack(task, job):
                order = job['orders'][-1]
                lab.mark_attempted(task, order['seq'], job['session_id'])
                lab.mark_broker_acked(task, order['seq'], job['session_id'])

            def event(job, event_id):
                order = job['orders'][-1]
                return {'type': 'rear_clear', 'event_id': event_id,
                        'robot_session': job['session_id'], 'order_id': job['task_id'],
                        'seq': order['seq'], 'map_id': order['map_id'],
                        'stopped_at': order['released_until'],
                        'cleared_resources': [list(item) for item in
                                              occurrence_clearance(data, order['nodes'], job['start_index'])]}

            a = lab.start('A-task', 'sim-a', 'session-a', 'E8', 'P_green')
            self.assertEqual((a['start_index'], a['end_index']), (0, 2))
            before = lab.snapshot('A-task')
            with self.assertRaises(LabConflict):
                lab.start('B-task', 'sim-b', 'session-b', 'E16', 'P_purple')
            self.assertEqual(lab.snapshot('A-task'), before)

            ack('A-task', a)
            a = lab.advance_simulated('sim-a', event(a, 'a1'), generation=lab.generation)
            self.assertEqual((a['start_index'], a['end_index']), (2, 4))
            self.assertEqual(a['orders'][-1]['released_until'], 'M3')
            b = lab.start('B-task', 'sim-b', 'session-b', 'E16', 'P_purple')
            ack('A-task', a)
            pending = event(a, 'a2')
            before = lab.snapshot('A-task')
            with self.assertRaises(LabConflict):
                lab.advance_simulated('sim-a', pending, generation=lab.generation)
            self.assertEqual(lab.snapshot('A-task'), before)
            ack('B-task', b)
            b = lab.advance_simulated('sim-b', event(b, 'b1'), generation=lab.generation)
            self.assertEqual(b['status'], 'complete')
            self.assertEqual(b['occupancy'], (('node', 'P_purple'),))
            a = lab.advance_simulated('sim-a', pending, generation=lab.generation)
            self.assertEqual((a['start_index'], a['end_index']), (4, 8))
            self.assertIn(('zone', 'Z_X1_X2'), a['occupancy'])


if __name__ == '__main__':
    unittest.main()
