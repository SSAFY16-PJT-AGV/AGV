"""A robot must validate an exact reroute even when it is not globally shortest."""
import json
from pathlib import Path
import unittest

from server.field_map_planner import plan_field_route, validate_field_route
from server.tests.test_field_map_planner import sample_map


class FieldRouteWitnessTest(unittest.TestCase):
    def test_exact_team_detour_is_legal_even_when_not_unblocked_shortest(self):
        source = Path(__file__).resolve().parents[1] / 'examples/mat_ssafy_v3_simulated_waits.json'
        data = json.loads(source.read_text(encoding='utf-8'))
        regular = plan_field_route(data, map_ref='mat_ssafy_v3@v0', incoming_edge='E1~r', goal='N02')
        detour = plan_field_route(data, map_ref='mat_ssafy_v3@v0', incoming_edge='E1~r', goal='N02',
                                  blocked_resources={('node', 'N07')})
        self.assertNotEqual(regular.nodes, detour.nodes)
        checked = validate_field_route(data, map_ref='mat_ssafy_v3@v0', incoming_edge='E1~r',
                                       nodes=list(detour.nodes), edge_ids=list(detour.edge_ids))
        self.assertEqual((checked.nodes, checked.edge_ids), (detour.nodes, detour.edge_ids))

    def test_invalid_crossing_wrong_edge_and_impossible_spur_are_rejected(self):
        data = sample_map()
        bad = [(['B', 'D'], ['E4']), (['B', 'C'], ['E4']), (['B', 'C', 'D'], ['E2', 'E4']),
               (['C', 'D_C'], ['ED']), (['B', 'B'], ['E1']), (['B', 'C', 'B'], ['E2', 'E2~r'])]
        for nodes, edge_ids in bad:
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                validate_field_route(data, map_ref='field-test@v1', incoming_edge='E1',
                                     nodes=nodes, edge_ids=edge_ids)

    def test_valid_spur_entry_and_exit(self):
        data = sample_map()
        for incoming, goal in [('E1', 'D_C'), ('ED', 'B')]:
            route = plan_field_route(data, map_ref='field-test@v1', incoming_edge=incoming, goal=goal)
            result = validate_field_route(data, map_ref='field-test@v1', incoming_edge=incoming,
                                          nodes=list(route.nodes), edge_ids=list(route.edge_ids))
            self.assertEqual((result.nodes, result.edge_ids), (route.nodes, route.edge_ids))

    def test_team_v3_generated_simple_routes_match_exact_witness(self):
        source = Path(__file__).resolve().parents[1] / 'examples/mat_ssafy_v3_simulated_waits.json'
        data = json.loads(source.read_text(encoding='utf-8'))
        incoming = [edge['id'] for edge in data['edges']]
        incoming += [edge['id'] + '~r' for edge in data['edges']
                     if edge.get('bidirectional') is True and edge.get('directed') is not True
                     and edge.get('type') != 'destination']
        checked = 0
        for edge in incoming:
            for goal in [node['id'] for node in data['nodes']]:
                try:
                    route = plan_field_route(data, map_ref='mat_ssafy_v3@v0', incoming_edge=edge, goal=goal)
                except ValueError:
                    continue
                if len(route.nodes) < 2 or len(route.nodes) != len(set(route.nodes)):
                    continue
                with self.subTest(incoming=edge, goal=goal):
                    witness = validate_field_route(data, map_ref='mat_ssafy_v3@v0', incoming_edge=edge,
                        nodes=list(route.nodes), edge_ids=list(route.edge_ids))
                    self.assertEqual((witness.nodes, witness.edge_ids), (route.nodes, route.edge_ids))
                    self.assertAlmostEqual(witness.distance_mm, route.distance_mm, places=8)
                    checked += 1
        self.assertGreater(checked, 100)


if __name__ == '__main__':
    unittest.main()
