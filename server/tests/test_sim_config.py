import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.main import load_sim_runtime


class SimConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / '.env.sim.json'
        (self.root / 'map.json').write_text('{"map_id":"example"}', encoding='utf-8')
        self.config.write_text(json.dumps({'database': 'sim.sqlite3', 'map_file': 'map.json',
                                          'robots': {'sim-1': {'T1': 'boot-1'}},
                                          'mqtt_port': 18883, 'mqtt_client_id': 'test-runtime'}), encoding='utf-8')

    def test_simulation_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(load_sim_runtime())

    def test_requires_long_ascii_token_and_loads_relative_files(self):
        env = {'AGV_SIM_CONFIG': str(self.config), 'AGV_SIM_API_TOKEN': 'x' * 40}
        with patch.dict(os.environ, env, clear=True), patch('server.sim_runtime.SimRuntime') as constructor:
            runtime, token = load_sim_runtime()
        self.assertIs(runtime, constructor.return_value)
        self.assertEqual(token, 'x' * 40)
        self.assertEqual(constructor.call_args.args[:2], (self.root / 'sim.sqlite3', {'map_id': 'example'}))
        self.assertEqual(constructor.call_args.kwargs['port'], 18883)

    def test_rejects_missing_or_weak_token(self):
        for token in ('', 'short', 'x' * 31 + ' ', 'x' * 31 + '한'):
            with self.subTest(token=token), patch.dict(os.environ, {'AGV_SIM_CONFIG': str(self.config),
                                                                     'AGV_SIM_API_TOKEN': token}, clear=True):
                with self.assertRaises(ValueError):
                    load_sim_runtime()

    def test_rejects_duplicate_or_unknown_configuration_fields(self):
        env = {'AGV_SIM_CONFIG': str(self.config), 'AGV_SIM_API_TOKEN': 'x' * 40}
        for value in ('{"database":"a","database":"b"}', '{"unknown":1}'):
            self.config.write_text(value, encoding='utf-8')
            with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ValueError):
                    load_sim_runtime()


if __name__ == '__main__':
    unittest.main()
