import copy
import unittest

from server.order_sim_map import make_order_sim_map
from server.order_demo_engine import OrderDemoEngine
from server.routing import MapGraph
from server.segments import plan_segment


def unreal_map():
    return {
        'map_id': 'warehouse-test', 'version': 3, 'coordinate_unit': 'meter',
        'nodes': [{'id': 'A', 'x': 1, 'y': 2}, {'id': 'B', 'x': 3, 'y': 2},
                  {'id': 'C', 'x': 3, 'y': 4}],
        'edges': [{'from': 'A', 'to': 'B', 'points': [{'x': 2, 'y': 2.5}]},
                  {'from': 'B', 'to': 'C', 'points': []}],
    }


class SimMapTest(unittest.TestCase):
    def test_rejects_robot_navigation_metadata_before_conversion(self):
        # Conversion drops metadata, so validate the source before converting.
        variants = [({'schema': 'agv-map-v1'}, {}),
                    ({}, {'crossing': True, 'through': [['E8', 'E9']]}),
                    ({}, {'uturn': True}), ({}, {'kind': 'via'})]
        for map_fields, node_fields in variants:
            data = unreal_map()
            data.update(map_fields)
            data['nodes'][1].update(node_fields)
            for converter in (make_order_sim_map, OrderDemoEngine):
                with self.subTest(map_fields=map_fields, node_fields=node_fields,
                                  converter=converter.__name__):
                    with self.assertRaisesRegex(ValueError, 'heading|direction'):
                        converter(data)

    def test_converts_nodes_curve_and_version_without_mutating_input(self):
        data = unreal_map()
        original = copy.deepcopy(data)
        converted = make_order_sim_map(data)
        self.assertEqual(data, original)
        self.assertEqual((converted['map_id'], converted['version'], converted['unit']),
                         ('warehouse-test', 3, 'mm'))
        self.assertEqual(converted['nodes'][0], {'id': 'A', 'pos': [1000, 2000]})
        self.assertEqual(converted['edges'][0]['shape'], [[1000, 2000], [2000, 2500], [3000, 2000]])
        self.assertTrue(all(e['bidirectional'] is True and e['lanes'] == 1 for e in converted['edges']))
        self.assertEqual(MapGraph(converted).shortest_path('C', 'A').nodes, ('C', 'B', 'A'))

    def test_every_node_is_an_explicit_simulated_waiting_point(self):
        converted = make_order_sim_map(unreal_map())
        self.assertEqual({z['node'] for z in converted['zones']}, {'A', 'B', 'C'})
        self.assertTrue(all(z['type'] == 'waiting' for z in converted['zones']))
        segment = plan_segment(converted, ['A', 'B', 'C'])
        self.assertEqual(segment.released_until, 'B')
        self.assertEqual(set(segment.nodes), {'A', 'B'})
        self.assertIn(('node', 'A'), segment.resources)
        self.assertIn(('node', 'B'), segment.resources)

    def test_missing_version_defaults_to_simulation_version_one(self):
        data = unreal_map()
        del data['version']
        self.assertEqual(make_order_sim_map(data)['version'], 1)

    def test_invalid_and_ambiguous_map_is_rejected(self):
        for kind in ('duplicate_node', 'duplicate_edge', 'bad_coordinate', 'bad_unit',
                     'bad_version', 'bad_map_id', 'unknown_endpoint', 'one_way'):
            data = unreal_map()
            if kind == 'duplicate_node':
                data['nodes'].append(copy.deepcopy(data['nodes'][0]))
            elif kind == 'duplicate_edge':
                data['edges'].append(copy.deepcopy(data['edges'][0]))
            elif kind == 'bad_coordinate':
                data['nodes'][0]['x'] = float('nan')
            elif kind == 'bad_unit':
                data['coordinate_unit'] = 'mm'
            elif kind == 'bad_version':
                data['version'] = True
            elif kind == 'bad_map_id':
                data['map_id'] = 'bad@v1'
            elif kind == 'unknown_endpoint':
                data['edges'][0]['to'] = 'missing'
            else:
                data['edges'][0]['bidirectional'] = False
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                make_order_sim_map(data)


if __name__ == '__main__':
    unittest.main()
