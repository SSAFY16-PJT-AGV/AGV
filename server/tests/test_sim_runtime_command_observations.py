"""Opt-in proposed command observations cannot grant physical permission."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from server.main import load_sim_runtime
from server.sim_runtime import SimRuntime
from server.tests.test_occurrence_allocation_lab import laboratory_map


class CommandObservationConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sim-observation-config-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def runtime(self, **kwargs):
        return SimRuntime(self.root / 'state.sqlite3', laboratory_map(),
                          {'sim-r1': {'T1': 'boot-1'}}, port=18883,
                          client_id='test-runtime', **kwargs)

    def test_disabled_by_default_and_enabled_status_is_separate(self):
        default = self.runtime()
        self.assertNotIn('command_observation_ready', default.status())
        runtime = self.runtime(field_simulation=True, command_observations=True)
        self.assertFalse(runtime.status()['ready'])
        self.assertFalse(runtime.status()['command_observation_ready'])

    def test_requires_boolean_and_explicit_field_simulation(self):
        for value in (1, 'true', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.runtime(field_simulation=True, command_observations=value)
        with self.assertRaises(ValueError):
            self.runtime(command_observations=True)
        with self.assertRaises(ValueError):
            self.runtime(host='localhost', field_simulation=True, command_observations=True)

    def test_config_must_explicitly_opt_in_and_reject_wrong_types(self):
        map_path = self.root / 'map.json'
        map_path.write_text(json.dumps(laboratory_map()), encoding='utf-8')
        config_path = self.root / 'config.json'
        config = {'database': 'state.sqlite3', 'map_file': 'map.json',
                  'robots': {'sim-r1': {'T1': 'boot-1'}}, 'mqtt_port': 18883,
                  'mqtt_client_id': 'config-test', 'field_simulation': True,
                  'command_observations': True}
        with patch.dict(os.environ, {'AGV_SIM_CONFIG': str(config_path),
                                     'AGV_SIM_API_TOKEN': 'a' * 32}, clear=True):
            config_path.write_text(json.dumps(config), encoding='utf-8')
            runtime, _ = load_sim_runtime()
            self.assertFalse(runtime.status()['command_observation_ready'])
            config['command_observations'] = 1
            config_path.write_text(json.dumps(config), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_sim_runtime()
            config['command_observations'] = True
            config['field_simulation'] = False
            config_path.write_text(json.dumps(config), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_sim_runtime()

    def test_observer_start_failure_stops_partial_resources_and_releases_owner(self):
        from server.sim_command_observation_receiver import SimCommandObservationReceiver
        runtime = self.runtime(field_simulation=True, command_observations=True)
        with patch.object(SimCommandObservationReceiver, 'start', side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                runtime.start()
        self.assertFalse(runtime.status()['ready'])
        self.assertFalse(runtime.status()['command_observation_ready'])
        self.assertTrue(runtime.command_observation_receiver._closed)
        self.assertIsNone(runtime._owner.file)
        with closing(sqlite3.connect(runtime.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM allocation_jobs').fetchone(), (0,))

    def test_security_environment_change_is_rejected_before_network_start(self):
        from server.sim_command_observation_receiver import SimCommandObservationReceiver
        with patch.dict(os.environ, {}, clear=True):
            runtime = self.runtime(field_simulation=True, command_observations=True)
        with patch.dict(os.environ, {'MQTT_USERNAME': 'changed'}, clear=True), \
                patch.object(SimCommandObservationReceiver, 'start') as start:
            with self.assertRaisesRegex(RuntimeError, 'security configuration changed'):
                runtime.start()
        start.assert_not_called()
        self.assertIsNone(runtime._owner.file)


if __name__ == '__main__':
    unittest.main()
