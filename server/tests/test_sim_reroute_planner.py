"""Reservation-aware route calculation is advisory and never grants motion."""
import copy
import json
from pathlib import Path
import unittest
from server.field_map_planner import NoFieldRouteError, plan_field_route
from server.segments import _edge_id


def branch_map():
    points = {'S': [0, 0], 'W': [1, 0], 'A': [2, 0], 'B': [2, 1], 'G': [3, 0]}
    pairs = [('S', 'W'), ('W', 'A'), ('A', 'G'), ('W', 'B'), ('B', 'G')]
    return {'map_id': 'branch', 'version': 1, 'unit': 'mm',
            'nodes': [{'id': n, 'pos': p} for n, p in points.items()],
            'edges': [{'id': 'E' + str(i), 'from': a, 'to': b, 'shape': [points[a], points[b]],
                       'lanes': 1, 'bidirectional': True} for i, (a, b) in enumerate(pairs)],
            'zones': [{'id': 'za', 'type': 'shared', 'nodes': ['A']},
                      {'id': 'wg', 'type': 'waiting', 'node': 'G'}]}


class AvoidOccupiedTest(unittest.TestCase):
    def test_avoid_node_edge_and_shared_zone(self):
        data = branch_map()
        for blocked in ({('node', 'A')}, {('edge', _edge_id('W', 'A'))}, {('zone', 'za')}):
            with self.subTest(blocked=blocked):
                result = plan_field_route(data, map_ref='branch@v1', incoming_edge='E0',
                                          goal='G', blocked_resources=blocked)
                self.assertEqual(result.nodes, ('W', 'B', 'G'))
        with self.assertRaises(NoFieldRouteError):
            plan_field_route(data, map_ref='branch@v1', incoming_edge='E0', goal='G',
                             blocked_resources={('node', 'A'), ('node', 'B')})

    def test_invalid_obstacle_or_overlapping_start_is_rejected(self):
        data = branch_map()
        for blocked in ({('invalid', 'A')}, {('node', 'missing')}, {('zone', 'missing')},
                        {('edge', 'not-an-edge')}, {('node', 'W')}):
            with self.subTest(blocked=blocked), self.assertRaises(ValueError):
                plan_field_route(data, map_ref='branch@v1', incoming_edge='E0',
                                 goal='G', blocked_resources=blocked)

    def test_team_v3_direction_aware_detour(self):
        path = Path(__file__).resolve().parents[1] / 'examples' / 'mat_ssafy_v3_simulated_waits.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        original = plan_field_route(data, map_ref='mat_ssafy_v3@v0', incoming_edge='E1~r', goal='N02')
        avoided = plan_field_route(data, map_ref='mat_ssafy_v3@v0', incoming_edge='E1~r', goal='N02',
                                   blocked_resources={('node', 'N07')})
        self.assertEqual(original.nodes, ('N01', 'N07', 'N02'))
        self.assertEqual(avoided.nodes, ('N01', 'J02', 'J07', 'N03', 'N02'))


if __name__ == '__main__':
    unittest.main()
