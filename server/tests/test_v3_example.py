"""The checked-in v3 demo must preserve the team map and its synthetic world."""
import copy
import json
from pathlib import Path
import unittest

from server.field_map_compatibility import project_field_map_to_twin
from server.field_twin_mapping import load_field_twin_mapping
from server.order_demo_engine import OrderDemoEngine

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'


def read(name):
    return json.loads((EXAMPLES / name).read_text(encoding='utf-8'))


class V3DemoExampleTest(unittest.TestCase):
    def test_demo_changes_only_explicitly_synthetic_waiting_zones(self):
        original = read('mat_ssafy_v3.json')
        demo = read('mat_ssafy_v3_simulated_waits.json')
        expected = copy.deepcopy(original)
        for node in ('N01', 'N02', 'N03', 'N04', 'N05', 'N06', 'N07', 'N08', 'J03', 'J07'):
            expected['zones'].append({'id': 'synthetic-wait-' + node, 'type': 'waiting', 'node': node})
        self.assertEqual(demo, expected)

    def test_demo_world_and_config_match_the_selected_v3_snapshot(self):
        field = read('mat_ssafy_v3_simulated_waits.json')
        world = read('mat_ssafy_v3_unreal_sim.json')
        self.assertEqual(world, project_field_map_to_twin(field))
        selected, mapping = load_field_twin_mapping(EXAMPLES / 'field_unreal_v3_sim.json', world)
        self.assertEqual(selected, field)
        self.assertEqual(mapping.map_ref, 'mat_ssafy_v3@v0')
        self.assertEqual(set(mapping.robots), {'sim-r1', 'sim-r2'})
        self.assertEqual({p['id'] for p in mapping.products}, {'red', 'blue'})
        OrderDemoEngine(world)


if __name__ == '__main__':
    unittest.main()
