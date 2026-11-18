"""Pure direction-aware calculations; they never permit robot motion."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest


def sample_map():
    return {
        'map_id': 'field-test', 'version': 1, 'unit': 'mm', 'schema': 'agv-map-v1',
        'nodes': [
            {'id': 'A', 'pos': [0, 0]},
            {'id': 'B', 'pos': [10, 0], 'crossing': True, 'through': [['E1', 'E2'], ['E3', 'E4']]},
            {'id': 'C', 'pos': [20, 0], 'kind': 'patch', 'uturn': True},
            {'id': 'H', 'pos': [10, -10]}, {'id': 'D', 'pos': [10, 10]},
            {'id': 'D_C', 'pos': [30, 0], 'kind': 'spur_end'},
        ],
        'edges': [
            {'id': 'E1', 'from': 'A', 'to': 'B', 'length_mm': 10, 'bidirectional': True, 'shape': [[0, 0], [10, 0]]},
            {'id': 'E2', 'from': 'B', 'to': 'C', 'length_mm': 10, 'bidirectional': True, 'shape': [[10, 0], [20, 0]]},
            {'id': 'E3', 'from': 'H', 'to': 'B', 'length_mm': 10, 'bidirectional': True, 'shape': [[10, -10], [10, 0]]},
            {'id': 'E4', 'from': 'B', 'to': 'D', 'length_mm': 10, 'bidirectional': True, 'shape': [[10, 0], [10, 10]]},
            {'id': 'ED', 'from': 'C', 'to': 'D_C', 'length_mm': 10, 'bidirectional': True,
             'type': 'destination', 'shape': [[20, 0], [30, 0]]},
        ],
    }


class FieldMapPlannerTests(unittest.TestCase):
    @staticmethod
    def plan(data, start, goal, ref='field-test@v1'):
        from server.field_map_planner import plan_field_route
        return plan_field_route(data, map_ref=ref, incoming_edge=start, goal=goal)

    def test_follows_matching_crossing_and_incoming_is_not_a_new_edge(self):
        plan = self.plan(sample_map(), 'E1', 'C')
        self.assertEqual(plan.nodes, ('B', 'C'))
        self.assertEqual(plan.edge_ids, ('E2',))
        self.assertEqual(plan.distance_mm, 10)
        self.assertEqual((plan.map_ref, plan.incoming_edge), ('field-test@v1', 'E1'))

    def test_wrong_crossing_exit_cannot_be_used(self):
        from server.field_map_planner import NoFieldRouteError
        with self.assertRaises(NoFieldRouteError):
            self.plan(sample_map(), 'E1', 'D')

    def test_reverse_direction_uses_inverted_crossing_pair(self):
        plan = self.plan(sample_map(), 'E2~r', 'A')
        self.assertEqual((plan.nodes, plan.edge_ids), (('B', 'A'), ('E1~r',)))

    def test_one_way_edge_does_not_create_reverse_crossing_exit(self):
        from server.field_map_planner import NoFieldRouteError
        data = sample_map()
        data['edges'][0]['bidirectional'] = False
        with self.assertRaises(NoFieldRouteError):
            self.plan(data, 'E2~r', 'A')

    def test_team_measured_length_takes_precedence_over_shape(self):
        data = sample_map()
        data['edges'][1]['length_mm'] = 17
        self.assertEqual(self.plan(data, 'E1', 'C').distance_mm, 17)

    def test_invalid_measured_lengths_are_rejected(self):
        for invalid in (0, -1, True, float('inf'), float('nan')):
            data = sample_map()
            data['edges'][1]['length_mm'] = invalid
            with self.subTest(length=invalid), self.assertRaises(ValueError):
                self.plan(data, 'E1', 'C')

    def test_uturn_retains_repeated_node_and_includes_penalty(self):
        plan = self.plan(sample_map(), 'E1', 'A')
        self.assertEqual(plan.nodes, ('B', 'C', 'B', 'A'))
        self.assertEqual(plan.edge_ids, ('E2', 'E2~r', 'E1~r'))
        self.assertEqual(plan.distance_mm, 530)

    def test_known_incoming_edge_and_exact_map_version_are_required(self):
        for start, ref in [('missing', 'field-test@v1'), ('E1', 'field-test@v2')]:
            with self.subTest(start=start, ref=ref), self.assertRaises(ValueError):
                self.plan(sample_map(), start, 'C', ref)

    def test_destination_spur_is_explicitly_unsupported(self):
        with self.assertRaises(ValueError):
            self.plan(sample_map(), 'E1', 'D_C')
        with self.assertRaises(ValueError):
            self.plan(sample_map(), 'ED', 'C')

    def test_spur_end_cannot_be_used_as_a_mainline_incoming_edge(self):
        data = sample_map()
        data['edges'] = [edge for edge in data['edges'] if edge['id'] != 'ED']
        data['edges'].append({'id': 'ES', 'from': 'D_C', 'to': 'C',
                              'length_mm': 10, 'bidirectional': True,
                              'shape': [[30, 0], [20, 0]]})
        with self.assertRaisesRegex(ValueError, 'incoming|spur'):
            self.plan(data, 'ES', 'C')

    def test_malformed_or_incomplete_crossing_rules_are_rejected(self):
        for through in (None, [], [['E1', 'missing']], [['E1', 'E4'], ['E3', 'E4']],
                        [['E1', 'E2']], [['E1', 'E2'], ['E1', 'E2'], ['E3', 'E4']]):
            data = sample_map()
            data['nodes'][1]['through'] = through
            with self.subTest(through=through), self.assertRaises(ValueError):
                self.plan(data, 'E1', 'C')

    def test_map_is_not_mutated(self):
        data = sample_map()
        original = copy.deepcopy(data)
        self.plan(data, 'E1', 'C')
        self.assertEqual(data, original)

    def test_malformed_geometry_is_rejected_even_if_measured_length_exists(self):
        data = sample_map()
        data['edges'][1]['shape'][-1] = [999, 0]
        with self.assertRaises(ValueError):
            self.plan(data, 'E1', 'C')


@unittest.skipUnless(os.getenv('AGV_FIELD_REPO'), 'set AGV_FIELD_REPO to the team field-run checkout')
class TeamNavigatorComparisonTests(unittest.TestCase):
    def test_team_edge_set_alone_does_not_preserve_repeat_order(self):
        root = Path(os.environ['AGV_FIELD_REPO'])
        spec = importlib.util.spec_from_file_location('field_team_navmap_repeat', root / 'agv/nav/navmap.py')
        navmap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(navmap)
        data = {'map_id': 'repeat', 'version': 1, 'nodes': [
            {'id': 'A'}, {'id': 'B'}, {'id': 'C', 'kind': 'patch', 'uturn': True}, {'id': 'D'}],
            'edges': [
                {'id': 'E1', 'from': 'A', 'to': 'B', 'length_mm': 10, 'bidirectional': True},
                {'id': 'E2', 'from': 'B', 'to': 'C', 'length_mm': 10, 'bidirectional': True},
                {'id': 'E3', 'from': 'B', 'to': 'D', 'length_mm': 10, 'bidirectional': True}]}
        team = navmap.NavMap(data)
        allowed = team.route_edges(['A', 'B', 'C', 'B', 'D'])
        # Edge-set filtering alone can choose B→D, skipping the required C visit.
        self.assertEqual(team.best_next('E1', 'D', allowed)[0], 'E3')

    def test_actual_team_map_and_navigator_match_valid_crossing_paths(self):
        from server.field_map_planner import plan_field_route
        root = Path(os.environ['AGV_FIELD_REPO'])
        raw = json.loads((root / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
        spec = importlib.util.spec_from_file_location('field_team_navmap', root / 'agv/nav/navmap.py')
        navmap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(navmap)
        team = navmap.NavMap(raw)
        for incoming, goal in [('E8', 'X2'), ('E16b', 'X2'), ('E8', 'X1'),
                               ('E9~r', 'S3'), ('E8', 'P_yellow')]:
            with self.subTest(incoming=incoming, goal=goal):
                ours = plan_field_route(raw, map_ref='mat_ssafy_v2@v0', incoming_edge=incoming, goal=goal)
                self.assertEqual(list(ours.nodes), team.shortest_path(incoming, goal))

    def test_all_team_main_edges_and_non_spur_goals_match_team_cost(self):
        from server.field_map_planner import plan_field_route, NoFieldRouteError
        root = Path(os.environ['AGV_FIELD_REPO'])
        raw = json.loads((root / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
        spec = importlib.util.spec_from_file_location('field_team_navmap_all', root / 'agv/nav/navmap.py')
        navmap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(navmap)
        team = navmap.NavMap(raw)
        for incoming in team.main_edges():
            for goal, node in team.nodes.items():
                if node['kind'] == 'spur_end':
                    continue
                with self.subTest(incoming=incoming, goal=goal):
                    expected = team.dist_to(goal).get(incoming)
                    if expected is None:
                        with self.assertRaises(NoFieldRouteError):
                            plan_field_route(raw, map_ref='mat_ssafy_v2@v0', incoming_edge=incoming, goal=goal)
                    else:
                        ours = plan_field_route(raw, map_ref='mat_ssafy_v2@v0', incoming_edge=incoming, goal=goal)
                        self.assertEqual(ours.nodes[-1], goal)
                        # Equal-cost routes may be tie-broken differently.
                        self.assertAlmostEqual(ours.distance_mm, expected, places=6)


if __name__ == '__main__':
    unittest.main()
