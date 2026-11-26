"""A synthetic UE projection may not hide a field map discrepancy."""
import copy
import math
import unittest

try:
    from server.field_map_compatibility import compare_field_twin_maps, project_field_map_to_twin
except ModuleNotFoundError as error:
    if error.name != 'server.field_map_compatibility':
        raise
    compare_field_twin_maps = project_field_map_to_twin = None


def field_map():
    return {
        'map_id': 'mat_v3', 'version': 0, 'unit': 'mm', 'coordinate_unit': 'meter',
        'nodes': [
            {'id': 'N01', 'type': 'access', 'pos': [466., 148.], 'x': .466, 'y': .148,
             'color': 'orange', 'role': 'unsorted'},
            {'id': 'N07', 'type': 'access', 'pos': [148., 508.], 'x': .148, 'y': .508,
             'color': 'pink', 'role': 'charge_agv01'},
            {'id': 'D07', 'type': 'destination', 'pos': [0., 508.], 'x': 0., 'y': .508,
             'purpose': 'charging'},
        ],
        'edges': [
            {'id': 'E1', 'from': 'N01', 'to': 'N07', 'type': 'main',
             'points': [{'x': .3, 'y': .2}]},
            {'id': 'E2', 'from': 'N07', 'to': 'D07', 'type': 'destination', 'points': []},
        ],
    }


class FieldMapCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(project_field_map_to_twin, 'field projection is not implemented')

    def test_projection_preserves_ids_metric_geometry_roles_and_topology(self):
        source = field_map()
        before = copy.deepcopy(source)
        projection = project_field_map_to_twin(source)
        self.assertEqual(source, before)
        self.assertEqual(projection['map_id'], 'mat_v3')
        self.assertEqual(projection['coordinate_unit'], 'meter')
        self.assertEqual(len(projection['nodes']), 3)
        self.assertEqual(len(projection['edges']), 2)
        self.assertEqual(projection['nodes'][1]['role'], 'charge_agv01')
        self.assertEqual(projection['edges'][0]['points'], [{'x': .3, 'y': .2}])
        report = compare_field_twin_maps(source, projection)
        self.assertTrue(report['compatible'], report)
        self.assertEqual(report['field_only_nodes'], [])
        self.assertEqual(report['twin_only_nodes'], [])
        self.assertEqual(report['node_mismatches'], {})
        self.assertEqual(report['field_only_edges'], [])
        self.assertEqual(report['twin_only_edges'], [])

    def test_all_node_differences_and_missing_ids_are_reported(self):
        source = field_map()
        twin = project_field_map_to_twin(source)
        twin['map_id'] = 'warehouse_01'
        twin['nodes'][1].update(x=12.25, y=2., color='cyan')
        del twin['nodes'][1]['role']
        twin['nodes'].pop()
        twin['nodes'].append({'id': 'J01', 'x': 1.5, 'y': 2., 'type': 'junction'})
        twin['edges'].pop()
        report = compare_field_twin_maps(source, twin)
        self.assertFalse(report['compatible'])
        self.assertEqual(report['field_only_nodes'], ['D07'])
        self.assertEqual(report['twin_only_nodes'], ['J01'])
        self.assertIn('map_id', report['metadata_mismatches'])
        self.assertIn('coordinates_m', report['node_mismatches']['N07'])
        self.assertIn('color', report['node_mismatches']['N07'])
        self.assertIn('role', report['node_mismatches']['N07'])
        self.assertEqual(len(report['field_only_edges']), 1)

    def test_reversed_edge_points_are_compared_in_opposite_order(self):
        source = field_map()
        twin = project_field_map_to_twin(source)
        edge = twin['edges'][0]
        edge['from'], edge['to'] = edge['to'], edge['from']
        edge['points'].reverse()
        self.assertTrue(compare_field_twin_maps(source, twin)['compatible'])
        edge['points'][0]['y'] += .1
        report = compare_field_twin_maps(source, twin)
        self.assertFalse(report['compatible'])
        self.assertEqual(len(report['edge_geometry_mismatches']), 1)

    def test_nonfinite_duplicate_invalid_units_or_inconsistent_coordinates_are_rejected(self):
        variants = []
        bad = field_map(); bad['unit'] = 'meter'; variants.append(bad)
        bad = field_map(); bad['nodes'][0]['x'] = math.nan; variants.append(bad)
        bad = field_map(); bad['nodes'][0]['x'] = 10 ** 1000; variants.append(bad)
        bad = field_map(); bad['nodes'][0]['pos'][0] += 1; variants.append(bad)
        bad = field_map(); bad['nodes'][0]['pos'][0] = True; variants.append(bad)
        bad = field_map(); bad['nodes'].append(copy.deepcopy(bad['nodes'][0])); variants.append(bad)
        bad = field_map(); bad['edges'][0]['to'] = 'missing'; variants.append(bad)
        bad = field_map(); bad['edges'][0]['points'] = [{'x': 0., 'y': float('inf')}]; variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    project_field_map_to_twin(bad)
        with self.assertRaises(ValueError):
            compare_field_twin_maps(field_map(), project_field_map_to_twin(field_map()), tolerance_m=True)
        twin = project_field_map_to_twin(field_map())
        twin['coordinate_unit'] = 'mm'
        with self.assertRaises(ValueError):
            compare_field_twin_maps(field_map(), twin)

    def test_projection_rejects_explicit_one_way_edges(self):
        for key, value in [('directed', True), ('bidirectional', False)]:
            source = field_map()
            source['edges'][0][key] = value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    project_field_map_to_twin(source)


if __name__ == '__main__':
    unittest.main()
