"""Direction-sensitive team maps must not drive simulation progression."""
import importlib.util
import json
import os
from pathlib import Path
import unittest

from server.segment_progress import SegmentProgress
from server.coordinator import Coordinator
from server.dispatch import RobotCandidate
from server.segments import plan_segment


def direction_sensitive_map():
    return {
            'map_id': 'mat', 'version': 0, 'unit': 'mm', 'schema': 'agv-map-v1',
            'nodes': [{'id': 'A', 'pos': [0, 0]}, {'id': 'B', 'pos': [1, 0]}],
            'edges': [{'from': 'A', 'to': 'B', 'shape': [[0, 0], [1, 0]],
                       'bidirectional': True, 'lanes': 1}],
            'zones': [{'id': 'wait', 'type': 'waiting', 'node': 'B'}],
    }


class FieldMapProgressGuardTests(unittest.TestCase):
    def test_progress_rejects_robot_navigation_map_before_loading_tasks(self):
        with self.assertRaisesRegex(ValueError, 'heading|direction'):
            SegmentProgress(object(), direction_sensitive_map(), simulation_enabled=True)

    def test_coordinator_refuses_to_prepare_task_before_reserving(self):
        coordinator = Coordinator(object(), direction_sensitive_map(), publisher=object())
        with self.assertRaisesRegex(ValueError, 'heading|direction'):
            coordinator.prepare('task', 'B', [RobotCandidate('sim-r1', 'A', True)],
                                {'type': 'none'})

    @unittest.skipUnless(os.getenv('AGV_FIELD_REPO'), 'set AGV_FIELD_REPO to team field-run checkout')
    def test_segment_resources_match_team_robot_without_implying_route_safety(self):
        root = Path(os.environ['AGV_FIELD_REPO'])
        data = json.loads((root / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
        spec = importlib.util.spec_from_file_location('field_segment_rules', root / 'agv/link/segments.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = ['D_P_orange', 'P_orange', 'S1', 'M3', 'P_red', 'D_P_red']
        segment = plan_segment(data, path)
        self.assertEqual(segment.released_until, 'D_P_red')
        expected = set(segment.resources) - {('node', 'D_P_red')}
        got = {tuple(item) for item in module.SegmentRules(data, 'zones').cleared(path, 0, len(path) - 1)}
        self.assertEqual(got, expected)


if __name__ == '__main__':
    unittest.main()
