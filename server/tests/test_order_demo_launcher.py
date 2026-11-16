import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from server.tools import order_demo as tool


def sample_map():
    return {'map_id': 'test', 'coordinate_unit': 'meter',
            'nodes': [{'id': 'A', 'x': 0, 'y': 0}, {'id': 'B', 'x': 1, 'y': 0}],
            'edges': [{'from': 'A', 'to': 'B', 'points': []}]}


class OrderDemoLauncherTests(unittest.TestCase):
    def test_environment_cannot_inherit_real_broker_or_order_settings(self):
        source = {'MQTT_HOST': 'physical', 'MQTT_PASSWORD': 'secret',
                  'AGV_ORDER_DEMO_MAP': 'real-map', 'AGV_OTHER': 'x',
                  'AGV_SIM_CONFIG': 'real.json', 'AGV_TWIN_ROBOT_MAP': '{}',
                  'HTTP_PROXY': 'http://remote:1', 'PATH': 'p'}
        env = tool.make_server_environment(source, 12345, 'temporary-token', Path('map.json'))
        self.assertEqual(env['MQTT_HOST'], '127.0.0.1')
        self.assertEqual(env['MQTT_PORT'], '12345')
        self.assertEqual(env['AGV_ORDER_DEMO_ENABLED'], '1')
        self.assertEqual(env['AGV_ORDER_COORDINATED'], '1')
        self.assertTrue(Path(env['AGV_ORDER_DEMO_MAP']).is_absolute())
        self.assertEqual(env['AGV_SIM_CONFIG'], '')
        self.assertEqual(env['PYTHON_DOTENV_DISABLED'], '1')
        self.assertEqual(json.loads(env['AGV_TWIN_ROBOT_MAP']),
                         {'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'})
        self.assertEqual(env['AGV_TWIN_POSE_MAP_ID'], 'twin-validation-v1')
        self.assertEqual(env['AGV_TWIN_UNREAL_TOKEN'], 'temporary-token')
        for key in ('MQTT_PASSWORD', 'AGV_OTHER', 'HTTP_PROXY'):
            self.assertNotIn(key, env)

    def test_inputs_validate_map_and_require_editor_project_pair(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            map_file = root / 'map.json'
            map_file.write_text(json.dumps(sample_map()), encoding='utf-8')
            deps = root / 'deps'
            (deps / 'amqtt').mkdir(parents=True)
            result = tool.validate_inputs(map_file, None, None, None, deps)
            self.assertEqual(result[0], map_file.resolve())
            with self.assertRaises(ValueError):
                tool.validate_inputs(map_file, 'editor.exe', None, None, deps)
            with self.assertRaises(ValueError):
                tool.validate_inputs(map_file, None, None, root, deps)
            map_file.write_text('{bad', encoding='utf-8')
            with self.assertRaises(ValueError):
                tool.validate_inputs(map_file, None, None, None, deps)

    def test_ctrl_c_stops_only_created_broker_and_server_not_editor(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            map_file = root / 'map.json'
            map_file.write_text(json.dumps(sample_map()), encoding='utf-8')
            deps = root / 'deps'
            (deps / 'amqtt').mkdir(parents=True)
            editor = root / 'Editor.exe'
            project = root / 'test.uproject'
            editor.touch(); project.touch()
            broker, server, unreal = Mock(pid=101), Mock(pid=102), Mock(pid=103)
            broker.poll.return_value = server.poll.return_value = None
            with patch.object(tool, '_select_broker_port', return_value=18832), \
                    patch.object(tool, 'wait_for_ready_log'), \
                    patch.object(tool, 'wait_for_demo'), \
                    patch.object(tool.subprocess, 'Popen', side_effect=[broker, server, unreal]) as popen, \
                    patch.object(tool, 'stop_process') as stop, \
                    patch.object(tool.time, 'sleep', side_effect=KeyboardInterrupt), \
                    patch('builtins.print') as output:
                tool.run(map_file, editor, project, None, deps)
            self.assertEqual(stop.call_args_list, [unittest.mock.call(server), unittest.mock.call(broker)])
            self.assertEqual(popen.call_count, 3)
            server_call = popen.call_args_list[1]
            self.assertIn('server.order_demo_app:app', server_call.args[0])
            self.assertEqual(server_call.kwargs['env']['MQTT_HOST'], '127.0.0.1')
            self.assertEqual(server_call.kwargs['env']['AGV_ORDER_DEMO_ENABLED'], '1')
            editor_env = popen.call_args_list[2].kwargs['env']
            self.assertEqual(editor_env['AGV_TWIN_UNREAL_TOKEN'],
                             server_call.kwargs['env']['AGV_TWIN_UNREAL_TOKEN'])
            printed = ' '.join(str(call) for call in output.call_args_list)
            self.assertNotIn(editor_env['AGV_TWIN_UNREAL_TOKEN'], printed)

    def test_server_start_failure_stops_broker_and_does_not_launch_editor(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            map_file = root / 'map.json'
            map_file.write_text(json.dumps(sample_map()), encoding='utf-8')
            deps = root / 'deps'
            (deps / 'amqtt').mkdir(parents=True)
            broker = Mock(pid=101)
            with patch.object(tool, '_select_broker_port', return_value=18832), \
                    patch.object(tool, 'wait_for_ready_log'), \
                    patch.object(tool.subprocess, 'Popen', side_effect=[broker, OSError('failed')]) as popen, \
                    patch.object(tool, 'stop_process') as stop:
                with self.assertRaises(OSError):
                    tool.run(map_file, broker_deps=deps)
            self.assertEqual(popen.call_count, 2)
            self.assertEqual(stop.call_args_list, [unittest.mock.call(None), unittest.mock.call(broker)])


if __name__ == '__main__':
    unittest.main()
