"""The team v3 map must not silently target the older Unreal world."""
import json
import unittest
from pathlib import Path

from server.field_map_compatibility import _edges, compare_field_twin_maps, project_field_map_to_twin
from server.field_twin_mapping import FieldTwinMapping, _fingerprint


ROOT = Path(__file__).resolve().parents[2]


def maps():
    field = json.loads((ROOT / 'server/examples/mat_ssafy_v3.json').read_text(encoding='utf-8'))
    twin = json.loads((ROOT / 'server/tests/fixtures/unreal_warehouse_ce23ec8.json').read_text(encoding='utf-8'))
    return field, twin


class V3TwinMappingTests(unittest.TestCase):
    def test_real_v3_and_latest_unreal_are_not_the_same_map(self):
        field, twin = maps()
        comparison = compare_field_twin_maps(field, twin)
        self.assertFalse(comparison['compatible'])
        self.assertIn('N08', comparison['field_only_nodes'])
        self.assertIn('D08', comparison['field_only_nodes'])
        self.assertIn('N07', comparison['node_mismatches'])
        self.assertIn('color', comparison['node_mismatches']['N07'])

    def test_two_anchor_mapping_cannot_hide_v3_world_mismatch(self):
        field, twin = maps()
        config = {'field_map_sha256': _fingerprint(field), 'unreal_map_id': 'warehouse_01',
                  'reflect_y': False, 'tolerance_m': .001,
                  'anchors': [{'field_node': 'N01', 'unreal_node': 'N01'},
                              {'field_node': 'N04', 'unreal_node': 'N04'}],
                  'robots': {}, 'products': []}
        with self.assertRaisesRegex(ValueError, 'v3.*Unreal.*match'):
            FieldTwinMapping(field, twin, config)

    def test_synthetic_projection_preserves_full_geometry(self):
        field, _ = maps()
        projected = project_field_map_to_twin(field)
        self.assertTrue(compare_field_twin_maps(field, projected)['compatible'])
        self.assertEqual(len(projected['nodes']), len(field['nodes']))
        self.assertEqual(len(projected['edges']), len(field['edges']))
        self.assertEqual(set(_edges(field, {n['id'] for n in field['nodes']}, field=True)),
                         set(_edges(projected, {n['id'] for n in projected['nodes']})))


if __name__ == '__main__':
    unittest.main()
