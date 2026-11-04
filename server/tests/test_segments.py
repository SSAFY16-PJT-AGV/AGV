import importlib
import importlib.util
import unittest


def map_data():
    return {
        'map_id': 'layout_A', 'version': 4, 'unit': 'mm',
        'nodes': [
            {'id': 'N1', 'pos': [0, 0]},
            {'id': 'N2', 'pos': [10, 0]},
            {'id': 'N3', 'pos': [20, 0]},
            {'id': 'N4', 'pos': [30, 0]},
        ],
        'edges': [
            {'from': 'N1', 'to': 'N2', 'shape': [[0, 0], [10, 0]], 'bidirectional': True, 'lanes': 1},
            {'from': 'N2', 'to': 'N3', 'shape': [[10, 0], [20, 0]], 'bidirectional': True, 'lanes': 1},
            {'from': 'N3', 'to': 'N4', 'shape': [[20, 0], [30, 0]], 'bidirectional': True, 'lanes': 1},
        ],
        'zones': [
            {'id': 'cross', 'type': 'shared', 'nodes': ['N2', 'N3']},
            {'id': 'wait3', 'type': 'waiting', 'node': 'N3'},
            {'id': 'wait4', 'type': 'waiting', 'node': 'N4'},
        ],
    }


class SegmentTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('server.segments'), 'segment planner is not implemented')
        self.segment = importlib.import_module('server.segments')

    def test_first_waiting_point_is_limit_and_shared_zone_is_reserved(self):
        plan = self.segment.plan_segment(map_data(), ['N1', 'N2', 'N3', 'N4'])
        self.assertEqual(plan.nodes, ('N1', 'N2', 'N3'))
        self.assertEqual(plan.released_until, 'N3')
        self.assertEqual(set(plan.resources), {
            ('node', 'N1'), ('node', 'N2'), ('node', 'N3'),
            ('edge', '["N1","N2"]'), ('edge', '["N2","N3"]'), ('zone', 'cross')})

    def test_reverse_directions_share_the_same_edge_resource(self):
        data = map_data()
        data['zones'].append({'id': 'wait1', 'type': 'waiting', 'node': 'N1'})
        forward = self.segment.plan_segment(data, ['N1', 'N2', 'N3'])
        reverse = self.segment.plan_segment(data, ['N3', 'N2', 'N1'])
        self.assertEqual({r for r in forward.resources if r[0] == 'edge'},
                         {r for r in reverse.resources if r[0] == 'edge'})

    def test_all_lanes_are_treated_as_single_exclusive_resource(self):
        data = map_data()
        data['edges'][0]['lanes'] = 2
        plan = self.segment.plan_segment(data, ['N1', 'N2', 'N3'])
        self.assertIn(('edge', '["N1","N2"]'), plan.resources)

    def test_destination_must_be_a_waiting_node_if_no_earlier_waiting_node(self):
        with self.assertRaises(ValueError):
            self.segment.plan_segment(map_data(), ['N1', 'N2'])

    def test_invalid_edges_directions_and_paths_are_rejected(self):
        data = map_data()
        data['edges'][0]['bidirectional'] = False
        for nodes in ([], ['N1'], ['N1', 'N1'], ['N2', 'N1'],
                      ['N1', 'N3'], ['N1', 'missing'], ['N1', 'N2', 'N1']):
            with self.subTest(nodes=nodes):
                with self.assertRaises(ValueError):
                    self.segment.plan_segment(data, nodes)

    def test_invalid_shared_or_waiting_zones_are_rejected(self):
        variants = []
        data = map_data(); del data['zones']; variants.append(data)
        data = map_data(); data['zones'][0]['nodes'] = ['missing']; variants.append(data)
        data = map_data(); data['zones'][0]['nodes'] = []; variants.append(data)
        data = map_data(); data['zones'][1]['node'] = 'missing'; variants.append(data)
        data = map_data(); data['zones'][1]['type'] = 'unknown'; variants.append(data)
        data = map_data(); data['zones'][1]['id'] = 'cross'; variants.append(data)
        for data in variants:
            with self.subTest(zones=data.get('zones')):
                with self.assertRaises(ValueError):
                    self.segment.plan_segment(data, ['N1', 'N2', 'N3'])

    def test_bad_lanes_and_geometry_are_rejected(self):
        variants = []
        for lanes in (None, 0, -1, True, '1'):
            data = map_data(); data['edges'][0]['lanes'] = lanes; variants.append(data)
        data = map_data(); data['edges'][0]['shape'][-1] = [999, 0]; variants.append(data)
        for data in variants:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.segment.plan_segment(data, ['N1', 'N2', 'N3'])


if __name__ == '__main__':
    unittest.main()
