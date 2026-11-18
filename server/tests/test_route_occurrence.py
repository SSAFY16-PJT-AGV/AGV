"""Experimental occurrence-indexed resource calculation; no movement authority."""
import importlib.util
import os
from pathlib import Path
import unittest

from server.route_occurrence import plan_occurrence_segment, occurrence_clearance
from server.segments import plan_segment


def map_data():
    return {
        'map_id': 'indexed-test', 'version': 0, 'unit': 'mm',
        'nodes': [{'id': 'A', 'pos': [0, 0]}, {'id': 'B', 'pos': [1, 0]},
                  {'id': 'C', 'pos': [1, 1]}, {'id': 'D', 'pos': [2, 0]}],
        'edges': [
            {'from': 'A', 'to': 'B', 'shape': [[0, 0], [1, 0]], 'bidirectional': True, 'lanes': 1},
            {'from': 'A', 'to': 'C', 'shape': [[0, 0], [1, 1]], 'bidirectional': True, 'lanes': 1},
            {'from': 'C', 'to': 'B', 'shape': [[1, 1], [1, 0]], 'bidirectional': True, 'lanes': 1},
            {'from': 'B', 'to': 'D', 'shape': [[1, 0], [2, 0]], 'bidirectional': True, 'lanes': 1},
        ],
        'zones': [{'id': 'wait-b', 'type': 'waiting', 'node': 'B'},
                  {'id': 'wait-d', 'type': 'waiting', 'node': 'D'},
                  {'id': 'shared-c', 'type': 'shared', 'nodes': ['C']}],
    }


class OccurrenceSegmentTests(unittest.TestCase):
    def test_directed_flag_blocks_reverse_even_when_bidirectional(self):
        data = map_data()
        data['zones'].append({'id': 'wait-a', 'type': 'waiting', 'node': 'A'})
        data['edges'][0]['directed'] = True
        with self.assertRaises(ValueError):
            plan_occurrence_segment(data, ['B', 'A'], 0)
        data['edges'][0]['directed'] = False
        self.assertEqual(plan_occurrence_segment(data, ['B', 'A'], 0).end_index, 1)
        data['edges'][0]['directed'] = 'yes'
        with self.assertRaises(ValueError):
            plan_occurrence_segment(data, ['B', 'A'], 0)

    def test_unique_route_matches_existing_resource_planner(self):
        data = map_data()
        route = ['A', 'B', 'D']
        actual = plan_occurrence_segment(data, route, 0)
        existing = plan_segment(data, route)
        self.assertEqual((actual.start_index, actual.end_index), (0, 1))
        self.assertEqual((actual.nodes, actual.released_until, actual.resources),
                         (existing.nodes, existing.released_until, existing.resources))
        self.assertEqual(data, map_data())

    def test_repeat_same_stop_uses_occurrence_not_first_matching_name(self):
        data = map_data()
        route = ['A', 'B', 'A', 'C', 'B', 'D']
        first = plan_occurrence_segment(data, route, 0)
        second = plan_occurrence_segment(data, route, first.end_index)
        final = plan_occurrence_segment(data, route, second.end_index)
        self.assertEqual((first.end_index, second.end_index, final.end_index), (1, 4, 5))
        self.assertEqual(second.nodes, ('B', 'A', 'C', 'B'))
        self.assertIn(('zone', 'shared-c'), second.resources)
        cleared = set(occurrence_clearance(data, route, first.start_index))
        self.assertNotIn(('node', 'B'), cleared)
        self.assertNotIn(('edge', '["A","B"]'), cleared)
        self.assertEqual(set(occurrence_clearance(data, route, second.start_index)),
                         set(second.resources) - set(final.resources))
        self.assertIn(('zone', 'shared-c'), occurrence_clearance(data, route, second.start_index))
        self.assertNotIn(('node', 'D'), occurrence_clearance(data, route, final.start_index))

    def test_shared_zone_stays_occupied_when_next_segment_also_needs_it(self):
        data = map_data()
        data['zones'][-1]['nodes'].append('A')
        first = plan_occurrence_segment(data, ['C', 'A', 'B', 'A', 'C', 'B', 'D'], 0)
        self.assertIn(('zone', 'shared-c'), first.resources)
        self.assertNotIn(('zone', 'shared-c'), occurrence_clearance(
            data, ['C', 'A', 'B', 'A', 'C', 'B', 'D'], 0))

    def test_invalid_route_offset_and_no_future_wait_are_rejected(self):
        data = map_data()
        cases = [([], 0), (['A'], 0), (['A', 'A'], 0), (['A', 'D'], 0),
                 (['A', 'B', 'D'], -1), (['A', 'B', 'D'], True),
                 (['A', 'B', 'D'], 2), (['A', 'C', 'B'], 1),
                 (['A', 'C'], 0), (['A', 'B', 'missing'], 0)]
        for route, start in cases:
            with self.subTest(route=route, start=start), self.assertRaises(ValueError):
                plan_occurrence_segment(data, route, start)

    def test_bad_map_and_waiting_in_shared_zone_are_rejected(self):
        variants = []
        data = map_data(); data['zones'][-1]['nodes'].append('B'); variants.append(data)
        data = map_data(); data['zones'][0]['node'] = 'missing'; variants.append(data)
        data = map_data(); data['edges'][0]['lanes'] = True; variants.append(data)
        data = map_data(); data['edges'][0]['bidirectional'] = False; variants.append(data)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError):
                plan_occurrence_segment(data, ['B', 'A', 'B', 'D'], 0)

    def test_inconsistent_waiting_aliases_are_rejected(self):
        # The team robot also reads zone.nodes, while the server reads node.
        # They must not silently disagree about the next release occurrence.
        for members in (['A'], ['B', 'A'], ['B', 'B'], 'B'):
            data = map_data()
            data['zones'][0]['nodes'] = members
            with self.subTest(members=members), self.assertRaises(ValueError):
                plan_occurrence_segment(data, ['A', 'B', 'D'], 0)
        data = map_data()
        data['zones'][0]['nodes'] = ['B']
        self.assertEqual(plan_occurrence_segment(data, ['A', 'B', 'D'], 0).end_index, 1)

    def test_missing_future_wait_prevents_any_clearance(self):
        data = map_data()
        # B is a waiting point, but the remaining path to C has none.
        with self.assertRaises(ValueError):
            occurrence_clearance(data, ['A', 'B', 'C'], 0)

    @unittest.skipUnless(os.getenv('AGV_FIELD_REPO'), 'set AGV_FIELD_REPO to the field-run checkout')
    def test_repeated_visit_clearance_matches_team_robot_rules(self):
        path = Path(os.environ['AGV_FIELD_REPO']) / 'agv/link/segments.py'
        spec = importlib.util.spec_from_file_location('field_occurrence_segments', path)
        if spec is None or spec.loader is None:
            self.fail('could not load team SegmentRules')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = map_data()
        route = ['A', 'B', 'A', 'C', 'B', 'D']
        rules = module.SegmentRules(data, 'zones')
        for start in (0, 1, 4):
            segment = plan_occurrence_segment(data, route, start)
            robot = {tuple(resource) for resource in rules.cleared(route, start, segment.end_index)}
            self.assertEqual(set(occurrence_clearance(data, route, start)), robot)


if __name__ == '__main__':
    unittest.main()
