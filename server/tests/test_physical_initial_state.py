"""Reported initial state is necessary, but not proof of real robot position."""
import importlib
import importlib.util
import unittest


class InitialPhysicalStateTests(unittest.TestCase):
    def setUp(self):
        name = 'server.physical_initial_state'
        self.assertIsNotNone(importlib.util.find_spec(name), 'initial state validator is missing')
        self.validate = importlib.import_module(name).validate_initial_state
        self.state = {
            'ts': 100.0, 'map_id': 'mat_ssafy_v2@v0', 'robot_session': '9b6cb70a5e354152',
            'last_node': 'P_orange', 'edge': None, 'next_node': None, 'progress': None,
            'dir': None, 'pose': None, 'status': 'IDLE', 'carrying': None,
            'battery': None, 'order_id': None, 'seq': None, 'released_until': None,
            'error': None, 'rejected': None,
        }

    def check(self, state):
        return self.validate(state, expected_map='mat_ssafy_v2@v0', expected_node='P_orange')

    def test_live_team_state_matching_config_returns_session(self):
        self.assertEqual(self.check(self.state), '9b6cb70a5e354152')

    def test_missing_required_evidence_is_not_assumed_null(self):
        for key in ('map_id', 'robot_session', 'last_node', 'status', 'error',
                    'order_id', 'seq', 'released_until'):
            with self.subTest(key=key):
                state = dict(self.state)
                del state[key]
                with self.assertRaises(ValueError):
                    self.check(state)

    def test_moving_error_wrong_map_or_wrong_start_are_rejected(self):
        for change in ({'status': 'MOVING'}, {'status': 'WAITING'}, {'status': 'ERROR'},
                       {'map_id': 'other@v0'}, {'last_node': 'P_blue'},
                       {'error': 'link_lost'}, {'error': False}, {'order_id': 'old'},
                       {'seq': 0}, {'seq': False}, {'released_until': 'P_blue'}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    self.check(dict(self.state, **change))

    def test_invalid_or_placeholder_session_is_rejected(self):
        for session in (None, '', '  ', ' abc', 'abc ', 'from_state', 'a\x00b', '\ud800'):
            with self.subTest(session=repr(session)):
                with self.assertRaises(ValueError):
                    self.check(dict(self.state, robot_session=session))

    def test_invalid_state_or_config_is_rejected(self):
        for state in (None, [], 'state', 42):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    self.check(state)
        for key, value in (('expected_map', None), ('expected_map', ''),
                           ('expected_node', None), ('expected_node', '')):
            with self.subTest(key=key):
                args = {'expected_map': 'mat_ssafy_v2@v0', 'expected_node': 'P_orange'}
                args[key] = value
                with self.assertRaises(ValueError):
                    self.validate(self.state, **args)


if __name__ == '__main__':
    unittest.main()
