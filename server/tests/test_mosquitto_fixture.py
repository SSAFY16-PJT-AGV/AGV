"""Regression tests for the optional broker test fixture's startup checks."""
import os
import unittest
from unittest import mock

from server.tests import test_fleet_mqtt_integration as fleet


class _Broker:
    def __init__(self, exited=False):
        self.exited = exited

    def poll(self):
        return 1 if self.exited else None

    def terminate(self):
        self.exited = True

    def wait(self, timeout=None):
        return 0


class MosquittoFixtureTests(unittest.TestCase):
    def _setup_fixture(self, broker_log, *, exited=False, clock=None):
        instance = fleet.FleetMqttIntegrationTest('test_fake_robot_order_and_reservation_survive_restart')
        self.addCleanup(instance.doCleanups)

        def spawn(_command, *, stdout, **_kwargs):
            stdout.buffer.write(broker_log)
            stdout.flush()
            return _Broker(exited=exited)

        with mock.patch.dict(os.environ, {'MOSQUITTO_EXE': 'test-only'}), \
                mock.patch.object(fleet.subprocess, 'Popen', side_effect=spawn):
            if clock is None:
                instance.setUp()
            else:
                with mock.patch.object(fleet.time, 'monotonic', side_effect=clock):
                    instance.setUp()
        return instance

    def test_non_utf8_path_does_not_hide_actual_start_marker(self):
        self._setup_fixture(b'Config loaded from D:\\' + bytes([0xb0, 0xfc]) +
                            b'\\broker.conf.\nmosquitto version 2.1.2 running\n')

    def test_exited_process_is_rejected_even_if_log_contains_start_marker(self):
        with self.assertRaisesRegex(AssertionError, 'exited before becoming ready'):
            self._setup_fixture(b'mosquitto version 2.1.2 running\n', exited=True)

    def test_missing_start_marker_does_not_count_as_ready(self):
        read_bytes = fleet.Path.read_bytes
        with mock.patch.object(fleet.Path, 'read_bytes', autospec=True, side_effect=read_bytes) as read:
            with self.assertRaisesRegex(AssertionError, 'did not report running'):
                self._setup_fixture(b'mosquitto version 2.1.2 starting\n', clock=[0.0, 0.0, 9.0])
        read.assert_called_once()

    def test_unreadable_startup_log_is_not_treated_as_ready(self):
        with mock.patch.object(fleet.Path, 'read_bytes', side_effect=OSError('log unavailable')):
            with self.assertRaisesRegex(OSError, 'log unavailable'):
                self._setup_fixture(b'mosquitto version 2.1.2 running\n')


if __name__ == '__main__':
    unittest.main()
