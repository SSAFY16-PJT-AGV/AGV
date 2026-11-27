"""The team shelf opt-in retains the validated v3 map transform."""
import json
from pathlib import Path
import unittest

from server.team_shelf_web_mapping import load_team_shelf_mapping


class TeamShelfWebMappingTest(unittest.TestCase):
    def test_four_products_share_one_pickup_and_use_team_stop_nodes(self):
        examples = Path(__file__).resolve().parents[1] / 'examples'
        unreal = json.loads((examples / 'mat_ssafy_v3_unreal_sim.json').read_text(encoding='utf-8'))
        field, mapping = load_team_shelf_mapping(examples / 'field_unreal_v3_sim.json', unreal)
        self.assertEqual({p['product_type'] for p in mapping.products}, {'milk', 'tissue', 'egg', 'juice'})
        self.assertEqual({p['pickup_node'] for p in mapping.products}, {'D01'})
        self.assertEqual({p['destination_node'] for p in mapping.products}, {'D02', 'D03', 'D04', 'D05'})
        self.assertEqual({p['slot_count'] for p in mapping.products}, {4})
        self.assertEqual(field['map_id'], 'mat_ssafy_v3')

    def test_changed_stop_coordinate_is_rejected(self):
        examples = Path(__file__).resolve().parents[1] / 'examples'
        unreal = json.loads((examples / 'mat_ssafy_v3_unreal_sim.json').read_text(encoding='utf-8'))
        next(n for n in unreal['nodes'] if n['id'] == 'D01')['x'] += .1
        with self.assertRaises(ValueError):
            load_team_shelf_mapping(examples / 'field_unreal_v3_sim.json', unreal)
