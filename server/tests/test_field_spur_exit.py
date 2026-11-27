"""Direction remains the actual exited spur when planning a later order."""
import copy
import json
from pathlib import Path
import unittest
from server.field_map_planner import plan_field_route, validate_field_route
from server.segments import _edge_id


class SpurExitPlanningTest(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((Path(__file__).resolve().parents[1] / 'examples' /
                                'mat_ssafy_v3_simulated_waits.json').read_text(encoding='utf-8'))
        self.ref = '{}@v{}'.format(self.data['map_id'], self.data['version'])

    def test_following_order_retains_real_spur_exit_direction(self):
        first = plan_field_route(self.data, map_ref=self.ref, incoming_edge='E_D01', goal='N01')
        self.assertEqual((first.nodes, first.edge_ids), (('D01', 'N01'), ('E_D01~r',)))
        second = plan_field_route(self.data, map_ref=self.ref, incoming_edge=first.edge_ids[-1], goal='D02')
        self.assertEqual((second.nodes[0], second.nodes[-1]), ('N01', 'D02'))
        self.assertEqual(second.incoming_edge, 'E_D01~r')
        checked = validate_field_route(self.data, map_ref=self.ref, incoming_edge='E_D01~r',
                                       nodes=second.nodes, edge_ids=second.edge_ids)
        self.assertEqual(checked, second)

    def test_exit_can_remain_at_parent_or_reenter_only_its_matching_spur(self):
        stopped = plan_field_route(self.data, map_ref=self.ref, incoming_edge='E_D01~r', goal='N01')
        self.assertEqual((stopped.nodes, stopped.edge_ids), (('N01',), ()))
        reentry = plan_field_route(self.data, map_ref=self.ref, incoming_edge='E_D01~r', goal='D01')
        self.assertEqual((reentry.nodes, reentry.edge_ids), (('N01', 'D01'), ('E_D01',)))
        with self.assertRaises(ValueError):
            validate_field_route(self.data, map_ref=self.ref, incoming_edge='E_D01~r',
                                 nodes=('N01', 'D02'), edge_ids=('E_D02',))

    def test_exit_parent_and_spur_metadata_are_checked(self):
        for mutation in ('crossing', 'missing', 'wrong_side', 'wrong_node'):
            data = copy.deepcopy(self.data)
            parent = next(node for node in data['nodes'] if node['id'] == 'N01')
            if mutation == 'crossing': parent['crossing'] = True
            elif mutation == 'missing': parent.pop('spur')
            elif mutation == 'wrong_side': parent['spur']['side'] = 'middle'
            else: parent['spur']['node'] = 'D02'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                plan_field_route(data, map_ref=self.ref, incoming_edge='E_D01~r', goal='D02')

    def test_exit_path_respects_blocked_next_edge(self):
        default = plan_field_route(self.data, map_ref=self.ref, incoming_edge='E_D01~r', goal='D02')
        blocked = _edge_id(default.nodes[0], default.nodes[1])
        try:
            alternate = plan_field_route(self.data, map_ref=self.ref, incoming_edge='E_D01~r', goal='D02',
                                         blocked_resources={('edge', blocked)})
        except ValueError:
            return  # no alternate route is a safe result
        self.assertNotIn(blocked, [_edge_id(a, b) for a, b in zip(alternate.nodes, alternate.nodes[1:])])

    def test_spur_exit_must_be_a_real_directed_exit(self):
        for value in ('E_D01~r~r', 'E_NO_SUCH~r'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                plan_field_route(self.data, map_ref=self.ref, incoming_edge=value, goal='D02')
