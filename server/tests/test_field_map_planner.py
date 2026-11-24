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

    def test_spur_goal_appends_destination_edge(self):
        plan = self.plan(sample_map(), 'E1', 'D_C')
        self.assertEqual((plan.nodes, plan.edge_ids, plan.distance_mm), (('B', 'C', 'D_C'), ('E2', 'ED'), 20))

    def test_spur_start_backs_out_then_may_take_either_direction(self):
        plan = self.plan(sample_map(), 'ED', 'C')
        self.assertEqual((plan.nodes, plan.edge_ids, plan.distance_mm), (('D_C', 'C'), ('ED~r',), 10))
        # C is a dead end towards D_C; leaving the spur towards B needs no U-turn cost.
        plan = self.plan(sample_map(), 'ED', 'A')
        self.assertEqual(plan.nodes, ('D_C', 'C', 'B', 'A'))
        self.assertEqual(plan.edge_ids, ('ED~r', 'E2~r', 'E1~r'))
        self.assertEqual(plan.distance_mm, 30)

    def test_parked_spur_self_route_is_empty(self):
        plan = self.plan(sample_map(), 'ED', 'D_C')
        self.assertEqual((plan.nodes, plan.edge_ids, plan.distance_mm), (('D_C',), (), 0.0))

    def test_reverse_of_spur_edge_is_not_an_incoming_edge(self):
        with self.assertRaises(ValueError):
            self.plan(sample_map(), 'ED~r', 'C')

    def test_spur_at_a_crossing_is_rejected(self):
        data = sample_map()
        data['nodes'].append({'id': 'D_B', 'pos': [10, 5], 'kind': 'spur_end'})
        data['edges'].append({'id': 'EB', 'from': 'B', 'to': 'D_B', 'length_mm': 5, 'bidirectional': True,
                              'type': 'destination', 'shape': [[10, 0], [10, 5]]})
        for start, goal in (('E1', 'D_B'), ('EB', 'C')):
            with self.subTest(start=start, goal=goal), self.assertRaisesRegex(ValueError, 'crossing'):
                self.plan(data, start, goal)

    def test_spur_edge_is_never_a_mainline_arc_whatever_its_orientation(self):
        data = sample_map()
        data['edges'] = [edge for edge in data['edges'] if edge['id'] != 'ED']
        data['edges'].append({'id': 'ES', 'from': 'D_C', 'to': 'C',
                              'length_mm': 10, 'bidirectional': True,
                              'shape': [[30, 0], [20, 0]]})
        # As an incoming edge it means "parked at the spur end", not "arrived at C on a main line".
        plan = self.plan(data, 'ES', 'C')
        self.assertEqual((plan.nodes, plan.edge_ids), (('D_C', 'C'), ('ES',)))
        plan = self.plan(data, 'E1', 'D_C')
        self.assertEqual((plan.nodes, plan.edge_ids), (('B', 'C', 'D_C'), ('E2', 'ES~r')))
        with self.assertRaises(ValueError):
            self.plan(data, 'ES~r', 'C')

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

    def test_spur_goals_cost_team_parent_distance_plus_spur(self):
        from server.field_map_planner import plan_field_route, NoFieldRouteError
        root = Path(os.environ['AGV_FIELD_REPO'])
        raw = json.loads((root / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
        spec = importlib.util.spec_from_file_location('field_team_navmap_spur', root / 'agv/nav/navmap.py')
        navmap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(navmap)
        team = navmap.NavMap(raw)
        for incoming in team.main_edges():
            for goal, node in team.nodes.items():
                parent = team.spur_parent(goal) if node['kind'] == 'spur_end' else None
                if parent is None or team.nodes[parent].get('crossing'):
                    continue
                with self.subTest(incoming=incoming, goal=goal):
                    expected = team.dist_to(parent).get(incoming)
                    if expected is None:
                        with self.assertRaises(NoFieldRouteError):
                            plan_field_route(raw, map_ref='mat_ssafy_v2@v0', incoming_edge=incoming, goal=goal)
                    else:
                        ours = plan_field_route(raw, map_ref='mat_ssafy_v2@v0', incoming_edge=incoming, goal=goal)
                        self.assertEqual(ours.nodes[-2:], (parent, goal))
                        self.assertAlmostEqual(ours.distance_mm, expected + team.spur_length(goal), places=6)


if __name__ == '__main__':
    unittest.main()
