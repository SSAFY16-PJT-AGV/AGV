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
    def test_team_shelves_explicit_state_directory_is_passed_to_server(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder)
            editor, project = temp / 'Editor.exe', temp / 'team.uproject'
            editor.touch(); project.touch()
            deps = temp / 'deps'
            (deps / 'amqtt').mkdir(parents=True)
            broker, server, unreal, restarted = (Mock(pid=i) for i in (101, 102, 103, 104))
            broker.poll.return_value = None
            server.poll.return_value = 1
            restarted.poll.return_value = None
            with patch.object(tool, '_select_broker_port', return_value=18832), \
                    patch.object(tool, 'wait_for_ready_log'), patch.object(tool, 'wait_for_demo'), \
                    patch.object(tool.subprocess, 'Popen', side_effect=[broker, server, unreal, restarted]) as popen, \
                    patch.object(tool, 'stop_process'), patch.object(tool.time, 'sleep', side_effect=KeyboardInterrupt), \
                    patch('builtins.print'):
                tool.run(root / 'server/examples/mat_ssafy_v3_unreal_sim.json', editor, project,
                         None, deps, temp / 'state', field_config=root / 'server/examples/field_unreal_v3_sim.json',
                         team_shelves=True)
            self.assertEqual(popen.call_count, 4)
            self.assertEqual(popen.call_args_list[1].kwargs['env']['AGV_ORDER_PERSIST_DIR'], str(temp / 'state'))
            self.assertEqual(popen.call_args_list[3].kwargs['env']['AGV_ORDER_PERSIST_DIR'], str(temp / 'state'))

    def test_team_shelves_opt_in_has_no_persistence_and_will_not_restart_server(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder)
            editor, project = temp / 'Editor.exe', temp / 'team.uproject'
            editor.touch(); project.touch()
            deps = temp / 'deps'
            (deps / 'amqtt').mkdir(parents=True)
            broker, server, unreal = Mock(pid=101), Mock(pid=102), Mock(pid=103)
            broker.poll.return_value = None
            server.poll.return_value = 1
            with patch.object(tool, '_select_broker_port', return_value=18832), \
                    patch.object(tool, 'wait_for_ready_log'), patch.object(tool, 'wait_for_demo'), \
                    patch.object(tool.subprocess, 'Popen', side_effect=[broker, server, unreal]) as popen, \
                    patch.object(tool, 'stop_process'), patch('builtins.print'):
                with self.assertRaisesRegex(RuntimeError, '이전 상태로 다시 시작할 수 없습니다'):
                    tool.run(root / 'server/examples/mat_ssafy_v3_unreal_sim.json', editor, project,
                             None, deps, None, field_config=root / 'server/examples/field_unreal_v3_sim.json',
                             team_shelves=True)
            env = popen.call_args_list[1].kwargs['env']
            self.assertEqual(env['AGV_ORDER_TEAM_SHELVES'], '1')
            self.assertNotIn('AGV_ORDER_PERSIST_DIR', env)
            self.assertEqual(popen.call_count, 3)

    def test_explicit_field_config_is_validated_and_passed_to_isolated_server(self):
        from server.tests.test_field_twin_mapping import fixture
        field, unreal, config = fixture()
        unreal['edges'] = [{'from': 'U_A', 'to': 'U_C', 'points': []}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            map_file, field_file, config_file = root / 'map.json', root / 'field.json', root / 'field-config.json'
            map_file.write_text(json.dumps(unreal), encoding='utf-8')
            field_file.write_text(json.dumps(field), encoding='utf-8')
            config['field_map_file'] = field_file.name
            config_file.write_text(json.dumps(config), encoding='utf-8')
            deps = root / 'deps'
            (deps / 'amqtt').mkdir(parents=True)
            broker, server = Mock(pid=101), Mock(pid=102)
            broker.poll.return_value = server.poll.return_value = None
            with patch.object(tool, '_select_broker_port', return_value=18832), \
                    patch.object(tool, 'wait_for_ready_log'), patch.object(tool, 'wait_for_demo'), \
                    patch.object(tool.subprocess, 'Popen', side_effect=[broker, server]) as popen, \
                    patch.object(tool, 'stop_process'), patch.object(tool.time, 'sleep', side_effect=KeyboardInterrupt), \
                    patch('builtins.print'):
                tool.run(map_file, broker_deps=deps, state_dir=root / 'state', field_config=config_file)
            self.assertEqual(popen.call_args_list[1].kwargs['env']['AGV_ORDER_FIELD_CONFIG'], str(config_file.resolve()))
            config['field_map_sha256'] = '0' * 64
            config_file.write_text(json.dumps(config), encoding='utf-8')
            with self.assertRaises(ValueError):
                tool.run(map_file, broker_deps=deps, state_dir=root / 'state', field_config=config_file)

    def test_environment_cannot_inherit_real_broker_or_order_settings(self):
        source = {'MQTT_HOST': 'physical', 'MQTT_PASSWORD': 'secret',
                  'AGV_ORDER_DEMO_MAP': 'real-map', 'AGV_OTHER': 'x',
                  'AGV_SIM_CONFIG': 'real.json', 'AGV_TWIN_ROBOT_MAP': '{}',
                  'HTTP_PROXY': 'http://remote:1', 'PATH': 'p'}
        env = tool.make_server_environment(source, 12345, 'temporary-token', Path('map.json'))
        self.assertEqual(env['AGV_ORDER_FLEET'], '1')
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

    def test_broker_exit_restarts_only_owned_broker_and_preserves_server(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            map_file = root / 'map.json'
            map_file.write_text(json.dumps(sample_map()), encoding='utf-8')
            deps = root / 'deps'
            (deps / 'amqtt').mkdir(parents=True)
            broker, server, replacement = Mock(pid=101), Mock(pid=102), Mock(pid=103)
            broker.poll.return_value = 1
            server.poll.return_value = replacement.poll.return_value = None
            with patch.object(tool, '_select_broker_port', return_value=18832), \
                    patch.object(tool, 'wait_for_ready_log'), \
                    patch.object(tool, 'wait_for_demo'), \
                    patch.object(tool.subprocess, 'Popen', side_effect=[broker, server, replacement]) as popen, \
                    patch.object(tool, 'stop_process') as stop, \
                    patch.object(tool.time, 'sleep', side_effect=KeyboardInterrupt), \
                    patch('builtins.print'):
                tool.run(map_file, broker_deps=deps)
            self.assertEqual(popen.call_count, 3)
            self.assertEqual(popen.call_args_list[0].args, popen.call_args_list[2].args)
            self.assertEqual(stop.call_args_list[-2:],
                             [unittest.mock.call(server), unittest.mock.call(replacement)])

    def test_failed_broker_spawn_keeps_server_and_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            map_file = root / 'map.json'
            map_file.write_text(json.dumps(sample_map()), encoding='utf-8')
            deps = root / 'deps'
            (deps / 'amqtt').mkdir(parents=True)
            broker, server, replacement = Mock(pid=101), Mock(pid=102), Mock(pid=103)
            broker.poll.return_value = 1
            server.poll.return_value = replacement.poll.return_value = None
            with patch.object(tool, '_select_broker_port', return_value=18832), \
                    patch.object(tool, 'wait_for_ready_log'), \
                    patch.object(tool, 'wait_for_demo'), \
                    patch.object(tool.subprocess, 'Popen', side_effect=[broker, server, OSError('busy'), replacement]) as popen, \
                    patch.object(tool, 'stop_process') as stop, \
                    patch.object(tool.time, 'sleep', side_effect=[None, KeyboardInterrupt]), \
                    patch('builtins.print'):
                tool.run(map_file, broker_deps=deps)
            self.assertEqual(popen.call_count, 4)
            self.assertEqual(stop.call_args_list[-2:],
                             [unittest.mock.call(server), unittest.mock.call(replacement)])


if __name__ == '__main__':
    unittest.main()
