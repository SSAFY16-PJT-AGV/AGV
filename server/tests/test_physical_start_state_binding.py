"""Initial configured physical occupancy must agree with live robot reports."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from server.physical_state_proof import FROM_STATE
from server.sim_runtime import SimRuntime
from server.tests.test_field_map_planner import sample_map


def report(session, node):
    return {'robot_session': session, 'map_id': 'field-test@v1', 'last_node': node,
            'status': 'IDLE', 'error': None, 'order_id': None, 'seq': None,
            'released_until': None}


class PhysicalStartStateBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = SimRuntime(Path(self.temp.name) / 'state.sqlite', sample_map(),
            {'physical-1': {'T1': FROM_STATE}, 'physical-2': {'T2': FROM_STATE}},
            port=1883, client_id='startup-check', field_simulation=True, physical_robots=True,
            field_initial_positions={'physical-1': 'A', 'physical-2': 'C'})
        self.watcher = Mock()
        self.watcher.start.return_value = self.watcher
        self.watcher.wait_sessions.return_value = {'physical-1': 's1', 'physical-2': 's2'}
        self.states = {'physical-1': report('s1', 'A'), 'physical-2': report('s2', 'C')}
        self.watcher.latest.side_effect = lambda robot: self.states[robot]

    def start_watcher(self):
        with patch('server.sim_runtime.PhysicalStateWatcher', return_value=self.watcher), \
                patch.object(self.runtime, '_new_client', return_value=Mock()):
            self.runtime._start_physical_watcher()

    def test_matching_reports_bind_both_sessions(self):
        self.start_watcher()
        self.assertEqual(self.runtime._tasks['T1'], ('physical-1', 's1'))
        self.assertEqual(self.runtime._tasks['T2'], ('physical-2', 's2'))

    def test_wrong_location_does_not_partially_bind_sessions(self):
        self.states['physical-2']['last_node'] = 'B'
        with self.assertRaises(ValueError):
            self.start_watcher()
        self.assertEqual(self.runtime._tasks['T1'], ('physical-1', FROM_STATE))
        self.assertEqual(self.runtime._tasks['T2'], ('physical-2', FROM_STATE))

    def test_session_changed_during_discovery_is_rejected(self):
        self.states['physical-2']['robot_session'] = 'new-session'
        with self.assertRaises(ValueError):
            self.start_watcher()
        self.assertEqual(self.runtime._tasks['T1'], ('physical-1', FROM_STATE))

    def test_missing_initial_position_or_fresh_state_is_rejected(self):
        self.runtime._field_initial_positions = None
        with self.assertRaises(ValueError):
            self.start_watcher()
        self.runtime._field_initial_positions = {'physical-1': 'A', 'physical-2': 'C'}
        self.states['physical-1'] = None
        with self.assertRaises(ValueError):
            self.start_watcher()


if __name__ == '__main__':
    unittest.main()
