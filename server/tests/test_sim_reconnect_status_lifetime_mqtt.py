"""Reconnect must not hold the runtime lock while sampling an observer."""
import os
import threading
import unittest
from unittest.mock import patch

from server.sim_runtime import SimRuntime
from server.tests import test_sim_runtime_command_observations_mqtt as fixture


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class ReconnectStatusLifetimeMqttTests(unittest.TestCase):
    setUp = fixture.RuntimeCommandObservationMqttTests.setUp
    wait_for = fixture.RuntimeCommandObservationMqttTests.wait_for

    def test_reconnect_observer_sampling_does_not_block_status_or_stop(self):
        runtime = SimRuntime(self.path, self.map_data, {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reconnect-status-lifetime',
                             field_simulation=True, command_observations=True)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'initial disconnect')
        sampling, release, status_done, stopped = (threading.Event() for _ in range(4))
        errors = []
        receiver_type = type(runtime.command_observation_receiver)
        original = receiver_type.ready.fget

        def delayed_ready(receiver):
            if threading.current_thread().name == 'reconnector':
                sampling.set()
                if not release.wait(10):
                    raise RuntimeError('test did not release observer sample')
            return original(receiver)

        def reconnect():
            try:
                runtime.reconnect()
            except BaseException as exc:
                errors.append(exc)

        def read_status():
            try:
                runtime.status()
                status_done.set()
            except BaseException as exc:
                errors.append(exc)

        def shutdown():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                stopped.set()

        with patch.object(receiver_type, 'ready', property(delayed_ready)):
            reconnect_thread = threading.Thread(target=reconnect, name='reconnector')
            reader = threading.Thread(target=read_status)
            stopper = threading.Thread(target=shutdown)
            reconnect_thread.start()
            try:
                self.assertTrue(sampling.wait(10))
                reader.start()
                self.assertTrue(status_done.wait(1), 'reconnect held runtime lock during observer sample')
                stopper.start()
                self.assertTrue(stopped.wait(5), 'stop waited on unrelated observer sample')
                self.assertIsNone(runtime._owner.file)
            finally:
                release.set()
                for thread in (reconnect_thread, reader, stopper):
                    if thread.ident is not None:
                        thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in (reconnect_thread, reader, stopper)))
        self.assertEqual(errors, [])
        self.assertFalse(runtime.status()['ready'])

    def test_reconnect_setup_failure_keeps_gate_block_and_can_stop(self):
        runtime = SimRuntime(self.path, self.map_data, {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reconnect-failure-lifetime',
                             field_simulation=True, command_observations=True)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'initial disconnect')
        self.assertIn('T1', runtime.gate._blocked)
        with patch.object(runtime, '_new_client', side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError):
                runtime.reconnect()
        self.assertIn('T1', runtime.gate._blocked)
        self.assertFalse(runtime.status()['ready'])
        runtime.stop()
        self.assertIsNone(runtime._owner.file)


if __name__ == '__main__':
    unittest.main()
