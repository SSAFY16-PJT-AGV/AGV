"""Spur routing requires explicit, verifiable geometry and direction data."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import unittest

from server.field_map_planner import plan_field_route
from server.field_dispatch import FieldCandidate, choose_field_robot
from server.tests.test_field_map_planner import sample_map


class SpurFieldPlannerTests(unittest.TestCase):
    def plan(self, data, incoming, goal):
        return plan_field_route(data, map_ref='field-test@v1', incoming_edge=incoming, goal=goal)

    def test_mainline_to_spur_and_parked_departure(self):
        data = sample_map()
        enter = self.plan(data, 'E1', 'D_C')
        self.assertEqual((enter.nodes, enter.edge_ids, enter.distance_mm),
                         (('B', 'C', 'D_C'), ('E2', 'ED'), 20))
        back = self.plan(data, 'ED', 'A')
        self.assertEqual((back.nodes, back.edge_ids, back.distance_mm),
                         (('D_C', 'C', 'B', 'A'), ('ED~r', 'E2~r', 'E1~r'), 30))
        self.assertEqual(self.plan(data, 'ED', 'D_C').nodes, ('D_C',))

    def test_simulated_dispatch_can_target_and_start_at_a_spur(self):
        data = sample_map()
        choice = choose_field_robot(data, 'field-test@v1', 'D_C', [
            FieldCandidate('sim-r1', 'B', 'E1', True)])
        self.assertEqual(choice.route.nodes, ('B', 'C', 'D_C'))
        choice = choose_field_robot(data, 'field-test@v1', 'A', [
            FieldCandidate('sim-r1', 'D_C', 'ED', True)])
        self.assertEqual(choice.route.nodes, ('D_C', 'C', 'B', 'A'))

    def test_spur_without_explicit_two_way_access_is_rejected(self):
        for changes in ({'bidirectional': False}, {'directed': True}):
            data = sample_map()
            data['edges'][-1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.plan(data, 'ED', 'C')
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.plan(data, 'E1', 'D_C')

    def test_spur_requires_a_matching_observable_side_and_destination(self):
        for meta in (None, {}, {'side': 'left'}, {'side': 'up', 'node': 'D_C'},
                     {'side': 'right', 'node': 'other'}):
            data = sample_map()
            data['nodes'][2]['spur'] = meta
            with self.subTest(meta=meta), self.assertRaises(ValueError):
                self.plan(data, 'E1', 'D_C')
            with self.subTest(meta=meta), self.assertRaises(ValueError):
                self.plan(data, 'ED', 'C')

    def test_spur_at_crossing_remains_rejected(self):
        data = sample_map()
        data['nodes'].append({'id': 'D_B', 'pos': [10, 5], 'kind': 'spur_end'})
        data['edges'].append({'id': 'EB', 'from': 'B', 'to': 'D_B', 'length_mm': 5,
                              'bidirectional': True, 'type': 'destination', 'shape': [[10, 0], [10, 5]]})
        with self.assertRaises(ValueError):
            self.plan(data, 'E1', 'D_B')

    def test_parked_self_route_still_validates_all_crossing_rules(self):
        data = sample_map()
        next(node for node in data['nodes'] if node['id'] == 'B')['through'] = []
        with self.assertRaisesRegex(ValueError, 'crossing requires explicit through rules'):
            self.plan(data, 'ED', 'D_C')

    def test_unverified_junction_parent_is_rejected_without_blocking_mainline(self):
        data = sample_map()
        data['nodes'].append({'id': 'D_H', 'pos': [10, -20], 'kind': 'spur_end'})
        data['edges'].append({'id': 'EH', 'from': 'H', 'to': 'D_H', 'length_mm': 10,
                              'bidirectional': True, 'type': 'destination', 'shape': [[10, -10], [10, -20]]})
        self.assertEqual(self.plan(data, 'E1', 'C').nodes, ('B', 'C'))
        with self.assertRaisesRegex(ValueError, 'verified patch'):
            self.plan(data, 'E1', 'D_H')
        with self.assertRaisesRegex(ValueError, 'verified patch'):
            self.plan(data, 'EH', 'C')


@unittest.skipUnless(os.getenv('AGV_FIELD_V3_REPO'), 'set AGV_FIELD_V3_REPO to the PR #62 checkout')
class TeamV3RouteComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(os.environ['AGV_FIELD_V3_REPO'])
        cls.data = json.loads((root / 'maps/mat_ssafy_v3.json').read_text(encoding='utf-8'))
        spec = importlib.util.spec_from_file_location('v3_team_navmap', root / 'agv/nav/navmap.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.team = mod.NavMap(cls.data)
        cls.ref = 'mat_ssafy_v3@v0'
        cls.main = cls.team.main_edges()
        cls.spurs = {n: cls.team.spur_parent(n) for n in cls.team.nodes
                     if cls.team.nodes[n]['kind'] == 'spur_end'}
        if len(cls.spurs) != 8 or any(p is None for p in cls.spurs.values()):
            raise AssertionError('expected all eight PR #62 spur ends')

    def test_j03_patch_overlap_is_zero_cost_only_for_the_known_source_geometry(self):
        path = plan_field_route(self.data, map_ref=self.ref, incoming_edge='E2', goal='N04')
        self.assertEqual((path.nodes, path.edge_ids, path.distance_mm),
                         (('J03', 'N04'), ('E3',), 0.0))

    def test_other_zero_or_modified_overlap_is_not_silently_accepted(self):
        mutations = [
            lambda d: d['edges'][0].update(length_mm=0.0),
            lambda d: next(e for e in d['edges'] if e['id'] == 'E3').update(length_mm=True),
            lambda d: next(e for e in d['edges'] if e['id'] == 'E3').update(length_m=None),
            lambda d: next(n for n in d['nodes'] if n['id'] == 'J03').update(kind='split'),
            lambda d: next(n for n in d['nodes'] if n['id'] == 'N04').update(kind='merge'),
            lambda d: next(n for n in d['nodes'] if n['id'] == 'J03').update(pos=[1200.3, 149.9]),
            lambda d: next(n for n in d['nodes'] if n['id'] == 'J03').update(x=1.201),
            lambda d: next(n for n in d['nodes'] if n['id'] == 'N04').update(y=0.149),
            lambda d: next(e for e in d['edges'] if e['id'] == 'E3').update(length_mm=-1),
            lambda d: next(e for e in d['edges'] if e['id'] == 'E3').update(
                shape=[[1200.2, 149.9], [1500, 500], [1256.0, 148.0]]),
            lambda d: next(e for e in d['edges'] if e['id'] == 'E3').update(length_src='unknown'),
        ]
        for mutate in mutations:
            data = copy.deepcopy(self.data)
            mutate(data)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                plan_field_route(data, map_ref=self.ref, incoming_edge='E2', goal='N04')

    def test_every_incoming_main_edge_and_all_v3_destinations_match_team_cost(self):
        from server.field_map_planner import NoFieldRouteError
        tested = 0
        for incoming in self.main:
            for goal, node in self.team.nodes.items():
                parent = self.spurs.get(goal)
                target = parent or goal
                expected = self.team.dist_to(target).get(incoming)
                with self.subTest(incoming=incoming, goal=goal):
                    if expected is None:
                        with self.assertRaises(NoFieldRouteError):
                            plan_field_route(self.data, map_ref=self.ref, incoming_edge=incoming, goal=goal)
                    else:
                        path = plan_field_route(self.data, map_ref=self.ref, incoming_edge=incoming, goal=goal)
                        self.assertEqual(path.nodes[-1], goal)
                        if parent:
                            self.assertEqual(path.nodes[-2:], (parent, goal))
                            expected += self.team.spur_length(goal)
                        self.assertAlmostEqual(path.distance_mm, expected, places=6)
                tested += 1
        self.assertGreaterEqual(tested, 500)

    def test_every_parked_spur_and_all_v3_destinations_match_team_cost(self):
        from server.field_map_planner import NoFieldRouteError
        edge_for = {e['to']: e['id'] for e in self.data['edges'] if e['to'] in self.spurs}
        for start, start_parent in self.spurs.items():
            incoming = edge_for[start]
            for goal in self.team.nodes:
                with self.subTest(start=start, goal=goal):
                    if goal == start:
                        self.assertEqual(plan_field_route(self.data, map_ref=self.ref,
                            incoming_edge=incoming, goal=goal).nodes, (start,))
                        continue
                    parent = self.spurs.get(goal)
                    main_goal = parent or goal
                    if start_parent == main_goal:
                        expected = 0.0
                    else:
                        distances = self.team.dist_to(main_goal)
                        choices = [self.team.edges[e]['len'] + distances[e]
                                   for e in self.team.edges_out_of(start_parent)
                                   if e in distances]
                        expected = min(choices) if choices else None
                    if expected is None:
                        with self.assertRaises(NoFieldRouteError):
                            plan_field_route(self.data, map_ref=self.ref, incoming_edge=incoming, goal=goal)
                    else:
                        expected += self.team.spur_length(start)
                        if parent:
                            expected += self.team.spur_length(goal)
                        path = plan_field_route(self.data, map_ref=self.ref, incoming_edge=incoming, goal=goal)
                        self.assertEqual(path.nodes[:2], (start, start_parent))
                        self.assertEqual(path.nodes[-1], goal)
                        self.assertAlmostEqual(path.distance_mm, expected, places=6)


if __name__ == '__main__':
    unittest.main()
