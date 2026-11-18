"""Explicit field/virtual-world mapping, not physical calibration."""
import json
import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from server.field_twin_mapping import load_field_twin_mapping
from server.synthetic_peer import SyntheticPeer
from server.tests.test_field_web_unreal_integration import fixtures


class SyntheticFieldPeerTests(unittest.TestCase):
    def test_field_pose_heading_and_distinct_pickup_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            map_path, config_path, _, _ = fixtures(root, None)
            world = json.loads(map_path.read_text(encoding='utf-8'))
            config = json.loads(config_path.read_text(encoding='utf-8'))
            field = json.loads((root / config['field_map_file']).read_text(encoding='utf-8'))
            # An explicitly different namespace and a quarter-turn transform;
            # same-named nodes or identity mapping would hide both bugs.
            for node in world['nodes']:
                x, y = node['x'] - 3, node['y'] - 4
                node.update(id='world-' + node['id'], x=3-y, y=4+x)
            for edge in world['edges']:
                edge['from'], edge['to'] = 'world-' + edge['from'], 'world-' + edge['to']
            for anchor in config['anchors']:
                anchor['unreal_node'] = 'world-' + anchor['unreal_node']
            for product in config['products']:
                for key in ('unreal_pickup_node', 'unreal_destination_node'):
                    product[key] = 'world-' + product[key]
            map_path.write_text(json.dumps(world), encoding='utf-8')
            config_path.write_text(json.dumps(config), encoding='utf-8')
            _, mapping = load_field_twin_mapping(config_path, world)
            field_nodes = {node['id']: node['pos'] for node in field['nodes']}
            starts = {}
            for robot, twin in (('sim-r1', 'AGV_01'), ('sim-r2', 'AGV_02')):
                x, y = field_nodes[mapping.robots[robot]['node']]
                starts[robot] = dict(agv_id=twin, **mapping.pose({'x': x, 'y': y, 'th': 0.}))
                self.assertAlmostEqual(starts[robot]['yaw'], 90.)
            observer = Mock()
            def state(robot):
                x, y = field_nodes[mapping.robots[robot]['node']]
                return {'connected': True, 'ready': True, 'received_at': time.monotonic(),
                        'observation': {'state_received_live': True,
                                        'state': {'map_id': mapping.map_ref,
                                                  'pose': {'x': x, 'y': y, 'th': 0.}}}}
            observer.twin_observation.side_effect = state
            peer = SyntheticPeer(Mock(), observer, mapping.products, starts, 'a' * 32, field_mapping=mapping)
            mismatched = copy.deepcopy(mapping.products)
            mismatched[0]['shelf_id'] = 'invented-shelf'
            with self.assertRaisesRegex(ValueError, 'products differ'):
                SyntheticPeer(Mock(), observer, mismatched, starts, 'a' * 32, field_mapping=mapping)
            mismatched_pose = copy.deepcopy(starts)
            mismatched_pose['sim-r1']['yaw'] += 1.
            with self.assertRaisesRegex(ValueError, 'initial pose differs'):
                SyntheticPeer(Mock(), observer, mapping.products, mismatched_pose, 'a' * 32, field_mapping=mapping)
            peer._refresh_poses()
            snapshot = peer._snapshot('request')['data']
            self.assertEqual({item['source_id'] for item in snapshot['pickups']},
                             {'world-P_orange', 'world-P_yellow'})
            self.assertEqual({item['source_id'] for item in mapping.pickups_to_field(snapshot['pickups'])},
                             {'P_orange', 'P_yellow'})
            pose = starts['sim-r1']
            result = peer._apply({'order_id': 'task', 'action_id': 'a1', 'agv_id': 'AGV_01',
                                  'action': 'grab', 'product_type': 'Red', 'result': 'success',
                                  'expected_x': pose['x'], 'expected_y': pose['y']})
            self.assertTrue(result['data']['applied'])
            self.assertEqual(peer._stock['world-P_orange'], 2)
