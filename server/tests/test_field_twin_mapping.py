import copy
import hashlib
import json
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import patch

from server.tests.test_occurrence_allocation_lab import laboratory_map


def fixture():
    field = laboratory_map()
    # Synthetic UE points: rotate field XY by +90 degrees and add (2, 3) metres.
    unreal = {'map_id': 'ue-test', 'coordinate_unit': 'meter', 'nodes': [
        {'id': 'U_A', 'x': 2, 'y': 3}, {'id': 'U_B', 'x': 2, 'y': 3.001},
        {'id': 'U_C', 'x': 2, 'y': 3.002}, {'id': 'U_D', 'x': 1.999, 'y': 3.001}]}
    digest = hashlib.sha256(json.dumps(field, sort_keys=True, ensure_ascii=True,
        allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
    config = {'field_map_sha256': digest, 'unreal_map_id': 'ue-test', 'reflect_y': False,
        'tolerance_m': .00001, 'anchors': [{'field_node': 'A', 'unreal_node': 'U_A'},
            {'field_node': 'C', 'unreal_node': 'U_C'}],
        'robots': {'sim-r1': {'node': 'A', 'incoming_edge': 'E0'},
                   'sim-r2': {'node': 'X', 'incoming_edge': 'E5'}},
        'products': [{'id': 'red', 'label': 'red', 'product_type': 'Red',
            'pickup_node': 'B', 'destination_node': 'D', 'unreal_pickup_node': 'U_B',
            'unreal_destination_node': 'U_D', 'shelf_id': 'SHELF_RED',
            'slot_index': 0, 'slot_count': 6}]}
    return field, unreal, config


class FieldTwinMappingTests(unittest.TestCase):
    def make(self, field=None, unreal=None, config=None):
        from server.field_twin_mapping import FieldTwinMapping
        a, b, c = fixture()
        return FieldTwinMapping(field or a, unreal or b, config or c)

    def test_same_transform_applies_to_pose_and_action_position(self):
        mapping = self.make()
        converted = mapping.pose({'x': 1, 'y': 1, 'th': 90})
        self.assertAlmostEqual(converted['x'], 1.999)
        self.assertAlmostEqual(converted['y'], 3.001)
        self.assertAlmostEqual(abs(converted['yaw']), 180)
        self.assertEqual(mapping.map_ref, 'lab@v1')
        self.assertEqual(mapping.pickup_sources['U_B'], 'B')

    def test_rejects_unverified_map_and_inconsistent_product_position(self):
        field, unreal, config = fixture()
        config['field_map_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.make(field, unreal, config)
        field, unreal, config = fixture()
        unreal['nodes'][2]['x'] = 100
        with self.assertRaises(ValueError):
            self.make(field, unreal, config)
        field, unreal, config = fixture()
        unreal['nodes'][1]['x'] += .1
        with self.assertRaises(ValueError):
            self.make(field, unreal, config)

    def test_rejects_nonfinite_pose_and_wrong_robot_entry(self):
        mapping = self.make()
        for pose in ({'x': float('nan'), 'y': 0, 'th': 0},
                     {'x': 0, 'y': True, 'th': 0}, {'x': 0, 'y': 0, 'th': float('inf')}):
            with self.subTest(pose=pose), self.assertRaises(ValueError):
                mapping.pose(pose)
        field, unreal, config = fixture()
        config['robots']['sim-r1']['incoming_edge'] = 'E1'
        with self.assertRaises(ValueError):
            self.make(field, unreal, config)

    def test_invalid_robot_shape_and_oversized_pose_raise_value_error(self):
        field, unreal, config = fixture()
        config['robots']['sim-r1'] = []
        with self.assertRaises(ValueError):
            self.make(field, unreal, config)
        with self.assertRaises(ValueError):
            self.make().pose({'x': 10 ** 1000, 'y': 0, 'th': 0})

    def test_rejects_ambiguous_product_source_mapping(self):
        field, unreal, config = fixture()
        second = copy.deepcopy(config['products'][0])
        second.update(id='blue', product_type='Blue', pickup_node='C')
        config['products'].append(second)
        with self.assertRaises(ValueError):
            self.make(field, unreal, config)

    def test_rejects_duplicate_product_type_or_shelf(self):
        for duplicate in ('product_type', 'shelf_id'):
            field, unreal, config = fixture()
            other = copy.deepcopy(config['products'][0])
            other.update(id='blue', product_type='Blue', shelf_id='SHELF_BLUE',
                         pickup_node='C', unreal_pickup_node='U_C')
            other[duplicate] = config['products'][0][duplicate]
            config['products'].append(other)
            with self.subTest(duplicate=duplicate), self.assertRaises(ValueError):
                self.make(field, unreal, config)

    def test_subset_pickup_mapping_requires_exact_valid_configured_sources(self):
        mapping = self.make()
        red = {'source_id': 'U_B', 'product_type': 'Red', 'available': 4}
        blue = {'source_id': 'unconfigured', 'product_type': 'Blue', 'available': 3}
        self.assertEqual(mapping.pickups_to_field([red, blue]),
                         [{'source_id': 'B', 'product_type': 'Red', 'available': 4}])
        for invalid in ([blue], [red, red], [dict(red, product_type='Blue')],
                        [dict(red, available=True)], [red, blue, blue]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                mapping.pickups_to_field(invalid)

    def test_reflection_correctly_changes_yaw(self):
        field, unreal, config = fixture()
        # Reflection across X precedes the +90 degree rotation.
        config['reflect_y'] = True
        unreal['nodes'][3]['x'] = 2.001
        mapping = self.make(field, unreal, config)
        pose = mapping.pose({'x': 1, 'y': 1, 'th': 90})
        self.assertAlmostEqual(pose['x'], 2.001)
        self.assertAlmostEqual(pose['yaw'], 0)

    def test_websocket_state_and_action_use_identical_coordinates(self):
        from server.twin_websocket import _message
        from server.order_demo_actions import DemoActionSession
        mapping = self.make()
        observation = {'state_received_live': True, 'state': {'map_id': 'lab@v1',
            'last_node': 'D', 'status': 'IDLE', 'battery': .8, 'pose': {'x': 1, 'y': 1, 'th': 90}}}
        with patch.dict('os.environ', {'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1'}):
            state = _message('sim-r1', observation, field_mapping=mapping)
        self.assertAlmostEqual(state['data']['x'], 1.999)
        self.assertAlmostEqual(state['data']['y'], 3.001)
        class Engine:
            def snapshot(self):
                return {'pose': observation['state']['pose'], 'pending_action': {
                    'order_id': 'o1', 'action_id': 'a1', 'agv_id': 'AGV_01', 'action': 'grab'}}
            def action_unknown(self):
                pass
        owner = object()
        session = DemoActionSession(Engine(), threading.RLock(), settle_seconds=0,
                                    pose_transform=mapping.pose)
        self.assertTrue(session.attach(owner, True))
        action = session.poll(owner, 1)
        self.assertAlmostEqual(action['data']['expected_x'], state['data']['x'])
        self.assertAlmostEqual(action['data']['expected_y'], state['data']['y'])

    def test_websocket_rejects_wrong_field_map(self):
        from server.twin_websocket import _message
        observation = {'state_received_live': True, 'state': {'map_id': 'unknown',
            'last_node': 'D', 'status': 'IDLE', 'battery': .8, 'pose': {'x': 1, 'y': 1, 'th': 90}}}
        self.assertIsNone(_message('sim-r1', observation, field_mapping=self.make()))

    def test_world_recovery_compares_transformed_pose_and_maps_pickup_ids(self):
        from server.order_demo_app import DemoRuntime
        mapping = self.make()
        class Engine:
            def __init__(self, twin, pose):
                self.TWIN, self.pose = twin, pose
            def snapshot(self):
                return {'status': 'completed', 'pose': self.pose}
        runtime = object.__new__(DemoRuntime)
        runtime.field_mapping = mapping
        runtime.journal = SimpleNamespace(world_id='a' * 32, list_orders=lambda: [{'state': 'completed'}],
                                          load_shelves=lambda: {'reservations': []})
        runtime.pickup_store = object()
        runtime._world_ready = threading.Event()
        runtime._restart_blocked = False
        runtime.engine = SimpleNamespace(engines={
            'sim-r1': Engine('AGV_01', {'x': 0, 'y': 0, 'th': 0}),
            'sim-r2': Engine('AGV_02', {'x': 1, 'y': 1, 'th': 90})})
        data = {'world_id': 'a' * 32, 'request_id': 'r', 'shelves': [],
                'pickups': [{'source_id': 'U_B', 'product_type': 'Red', 'available': 1}],
                'robots': [{'agv_id': 'AGV_01', 'x': 2, 'y': 3, 'yaw': 90,
                            'carrying': False, 'interpolating': False},
                           {'agv_id': 'AGV_02', 'x': 1.999, 'y': 3.001, 'yaw': -180,
                            'carrying': False, 'interpolating': False}]}
        runtime._validate_world(data)
        self.assertFalse(runtime._validated_bootstrap)
        self.assertEqual(runtime._validated_pickups[0]['source_id'], 'B')
        data['robots'][1]['yaw'] = 0
        with self.assertRaises(ValueError):
            runtime._validate_world(data)


if __name__ == '__main__':
    unittest.main()
