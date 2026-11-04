import asyncio
import unittest
from unittest.mock import patch

from server import main


class SimLifespanTest(unittest.TestCase):
    def setUp(self):
        self.state = main.app.state._state.copy()
        self.events = []

    def tearDown(self):
        main.app.state._state.clear()
        main.app.state._state.update(self.state)

    def run_lifespan(self, runtime_and_token, fail=False):
        events = self.events
        class Runtime:
            def start(self):
                events.append('sim_start')
                if fail:
                    raise RuntimeError('failed')
            def stop(self):
                events.append('sim_stop')
        runtime = Runtime()
        value = (runtime, 'token') if runtime_and_token else None
        async def exercise():
            with patch.object(main, 'start_mqtt', side_effect=lambda: events.append('mqtt_start') or 'mqtt'), \
                 patch.object(main, 'stop_mqtt', side_effect=lambda _service: events.append('mqtt_stop')), \
                 patch.object(main, 'load_sim_runtime', return_value=value, create=True):
                async with main.lifespan(main.app):
                    events.append('running')
                    self.assertEqual(getattr(main.app.state, 'sim_runtime', None), runtime if value else None)
        asyncio.run(exercise())

    def test_default_does_not_start_simulation(self):
        self.run_lifespan(False)
        self.assertEqual(self.events, ['mqtt_start', 'running', 'mqtt_stop'])

    def test_enabled_runtime_lives_only_with_the_app(self):
        self.run_lifespan(True)
        self.assertEqual(self.events, ['mqtt_start', 'sim_start', 'running', 'sim_stop', 'mqtt_stop'])
        self.assertIsNone(getattr(main.app.state, 'sim_runtime', None))

    def test_failure_cleans_both_services(self):
        with self.assertRaises(RuntimeError):
            self.run_lifespan(True, fail=True)
        self.assertEqual(self.events, ['mqtt_start', 'sim_start', 'sim_stop', 'mqtt_stop'])
        self.assertIsNone(getattr(main.app.state, 'sim_runtime', None))


if __name__ == '__main__':
    unittest.main()
