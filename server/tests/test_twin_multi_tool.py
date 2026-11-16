"""Checks for the portable, isolated two-robot visual test helper."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from server.tools import twin_multi_test as tool


class TwinMultiToolTest(unittest.TestCase):
    def test_simulated_states_remain_on_two_separate_map_segments(self):
        for elapsed in (0, 0.2, 1.0, 3.0, 10.0, 123.4):
            states = tool.simulated_states(elapsed)
            self.assertEqual(set(states), {'sim-r1', 'sim-r2'})
            for robot, bounds, lane in (('sim-r1', (1500, 6500), 2000),
                                        ('sim-r2', (2800, 5500), 7000)):
                value = states[robot]
                self.assertEqual(value['map_id'], 'twin-validation-v1')
                self.assertEqual(value['status'], 'MOVING')
                self.assertLessEqual(bounds[0], value['pose']['x'])
                self.assertLessEqual(value['pose']['x'], bounds[1])
                self.assertEqual(value['pose']['y'], lane)
                self.assertEqual(value['pose']['th'], 0)

    def test_test_server_never_inherits_real_broker_credentials_or_proxy(self):
        hostile = {'MQTT_HOST': 'real-broker', 'MQTT_PASSWORD': 'secret',
                   'AGV_SIM_CONFIG': 'real.json', 'AGV_TWIN_ROBOT_MAP': '{}',
                   'HTTP_PROXY': 'http://remote:1', 'mqtt_proxy': 'socks5://remote:1',
                   'NO_PROXY': '', 'PATH': 'test-path'}
        env = tool.server_environment(hostile, 12345, 'test-token')
        self.assertEqual(env['MQTT_HOST'], '127.0.0.1')
        self.assertEqual(env['MQTT_PORT'], '12345')
        self.assertNotIn('MQTT_PASSWORD', env)
        self.assertNotIn('HTTP_PROXY', env)
        self.assertNotIn('mqtt_proxy', env)
        self.assertEqual(env['NO_PROXY'], '127.0.0.1,localhost')
        self.assertEqual(env['AGV_SIM_CONFIG'], '')
        self.assertEqual(env['PYTHON_DOTENV_DISABLED'], '1')
        self.assertEqual(json.loads(env['AGV_TWIN_ROBOT_MAP']),
                         {'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'})

    def test_only_matching_live_child_ready_marker_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'broker.log'
            child = Mock(pid=42)
            child.poll.return_value = None
            log.write_text('broker-ready:41\n', encoding='utf-8')
            with self.assertRaises(TimeoutError):
                tool.wait_for_ready_log(child, log, 'broker-ready:42',
                                        'broker', timeout=0.02)
            log.write_text('broker-ready:42\n', encoding='utf-8')
            tool.wait_for_ready_log(child, log, 'broker-ready:42', 'broker', timeout=0.2)
            child.poll.return_value = 1
            with self.assertRaises(RuntimeError):
                tool.wait_for_ready_log(child, log, 'broker-ready:42', 'broker', timeout=0.2)

    def test_invalid_editor_or_project_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            editor = Path(folder) / 'editor.exe'
            editor.touch()
            with self.assertRaises(ValueError):
                tool.validate_paths(editor, Path(folder) / 'missing.uproject', None)
            project = Path(folder) / 'test.uproject'
            project.touch()
            got_editor, got_project, ddc = tool.validate_paths(editor, project, None)
            self.assertEqual(got_editor, editor.resolve())
            self.assertEqual(got_project, project.resolve())
            self.assertIsNone(ddc)

    def test_stop_process_only_stops_running_process_given_to_it(self):
        child = Mock()
        child.poll.return_value = None
        tool.stop_process(child)
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)
        child.kill.assert_not_called()
        done = Mock()
        done.poll.return_value = 0
        tool.stop_process(done)
        done.terminate.assert_not_called()

    def test_socks_default_proxy_is_rejected_without_connect(self):
        client = Mock()
        with patch.object(tool.mqtt, 'socks', Mock(get_default_proxy=Mock(
                return_value=(2, '127.0.0.1', 1, True, None, None)))):
            with self.assertRaises(RuntimeError):
                tool.connect_local_publisher(client, 12345)
        client.connect.assert_not_called()

    def test_publish_only_synthetic_state_without_retention(self):
        client = Mock()
        client.publish.return_value.is_published.return_value = True
        tool.publish_states(client, 1.0)
        calls = client.publish.call_args_list
        self.assertEqual([call.args[0] for call in calls],
                         ['agv/sim-r1/state', 'agv/sim-r2/state'])
        for call in calls:
            self.assertEqual(call.kwargs, {'qos': 0, 'retain': False})
            self.assertEqual(json.loads(call.args[1])['map_id'], 'twin-validation-v1')


if __name__ == '__main__':
    unittest.main()
