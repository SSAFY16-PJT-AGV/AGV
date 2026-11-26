"""The v3 Unreal demo must use the original map and explicitly mapped spurs."""
import json
from pathlib import Path
import unittest

from server.field_map_compatibility import project_field_map_to_twin
from server.field_map_planner import plan_field_route
from server.field_twin_mapping import load_field_twin_mapping

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'


class V3UnrealDemoConfigTests(unittest.TestCase):
    def test_original_map_projection_and_mapped_product_spurs(self):
        original = json.loads((EXAMPLES / 'mat_ssafy_v3.json').read_text(encoding='utf-8'))
        world = json.loads((EXAMPLES / 'mat_ssafy_v3_unreal_sim.json').read_text(encoding='utf-8'))
        self.assertEqual(world, project_field_map_to_twin(original))
        selected, mapping = load_field_twin_mapping(EXAMPLES / 'field_unreal_v3.json', world)
        self.assertEqual(selected, original)
        self.assertEqual(mapping.robots, {
            'sim-r1': {'node': 'D07', 'incoming_edge': 'E_D07'},
            'sim-r2': {'node': 'D08', 'incoming_edge': 'E_D08'},
        })
        products = {item['id']: item for item in mapping.products}
        self.assertEqual((products['red']['pickup_node'], products['red']['destination_node'],
                          products['red']['shelf_id']), ('D01', 'D04', 'SHELF_RED'))
        self.assertEqual((products['blue']['pickup_node'], products['blue']['destination_node'],
                          products['blue']['shelf_id']), ('D06', 'D03', 'SHELF_BLUE'))
        for product in products.values():
            start = 'E_' + product['pickup_node']
            path = plan_field_route(original, map_ref='mat_ssafy_v3@v0', incoming_edge=start,
                                    goal=product['destination_node'])
            self.assertEqual(path.nodes[0], product['pickup_node'])
            self.assertEqual(path.nodes[-1], product['destination_node'])


if __name__ == '__main__':
    unittest.main()
