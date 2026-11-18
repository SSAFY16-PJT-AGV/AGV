"""Direction-aware selection for explicitly simulated field-map candidates."""
import json
import os
from pathlib import Path
import unittest

from server.field_dispatch import FieldCandidate, choose_field_robot
from server.tests.test_field_map_planner import sample_map


class FieldDispatchTests(unittest.TestCase):
    def test_picks_nearest_direction_compatible_available_robot(self):
        candidates = [FieldCandidate('sim-a', 'B', 'E1', True),
                      FieldCandidate('sim-b', 'B', 'E3', True),
                      FieldCandidate('sim-c', 'C', 'E2', False)]
        choice = choose_field_robot(sample_map(), 'field-test@v1', 'D', candidates)
        self.assertEqual(choice.robot_id, 'sim-b')
        self.assertEqual(choice.route.nodes, ('B', 'D'))

    def test_incoming_edge_must_end_at_claimed_node(self):
        with self.assertRaisesRegex(ValueError, 'node|edge'):
            choose_field_robot(sample_map(), 'field-test@v1', 'C',
                               [FieldCandidate('sim-a', 'A', 'E1', True)])

    def test_non_simulated_duplicate_and_invalid_candidates_are_rejected(self):
        for candidates in ([FieldCandidate('jetank-1', 'B', 'E1', True)],
                           [FieldCandidate('sim-a', 'B', 'E1', True)] * 2,
                           [FieldCandidate('sim-a', 'B', 'E1', 1)]):
            with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                choose_field_robot(sample_map(), 'field-test@v1', 'C', candidates)

    def test_invalid_map_or_unknown_goal_rejected_even_without_available_robot(self):
        for ref, goal in [('field-test@v2', 'C'), ('field-test@v1', 'missing')]:
            with self.subTest(ref=ref, goal=goal), self.assertRaises(ValueError):
                choose_field_robot(sample_map(), ref, goal, [])

    def test_deterministic_tie_breaking(self):
        candidates = [FieldCandidate('sim-b', 'B', 'E1', True),
                      FieldCandidate('sim-a', 'B', 'E1', True)]
        self.assertEqual(choose_field_robot(sample_map(), 'field-test@v1', 'C', candidates).robot_id, 'sim-a')

    @unittest.skipUnless(os.getenv('AGV_FIELD_REPO'), 'set AGV_FIELD_REPO to team field-run checkout')
    def test_team_map_selects_compatible_entry_at_same_crossing(self):
        root = Path(os.environ['AGV_FIELD_REPO'])
        data = json.loads((root / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
        candidates = [FieldCandidate('sim-a', 'P_yellow', 'E8', True),
                      FieldCandidate('sim-b', 'P_yellow', 'E16b', True)]
        result = choose_field_robot(data, 'mat_ssafy_v2@v0', 'X2', candidates)
        self.assertEqual((result.robot_id, result.route.nodes), ('sim-b', ('P_yellow', 'X2')))


if __name__ == '__main__':
    unittest.main()
