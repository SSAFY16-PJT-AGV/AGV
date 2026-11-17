"""Failures in a restart test must remain diagnosable after a child exits."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from server.tests import test_order_persistent_restart_integration as restart


class RestartDiagnosticsTests(unittest.TestCase):
    def helper(self, name):
        value = getattr(restart, name, None)
        self.assertTrue(callable(value), name + ' must be available to the restart harness')
        return value

    def test_server_uses_normal_uvicorn_entrypoint_without_repeating_dump(self):
        start = self.helper('_start_test_server')
        child, log = object(), object()
        env = {'AGV_ORDER_DEMO_ENABLED': '1'}
        cwd = Path('test-workspace')
        with patch.object(restart.subprocess, 'Popen', return_value=child) as popen:
            result = start('python.exe', 54321, env, log, cwd, 0)
        self.assertIs(result, child)
        args, kwargs = popen.call_args
        command = args[0]
        self.assertEqual(command[:4], ['python.exe', '-m', 'uvicorn', 'server.order_demo_app:app'])
        self.assertNotIn('-c', command)
        self.assertNotIn('faulthandler', repr(command))
        self.assertEqual(command[command.index('--host') + 1], '127.0.0.1')
        self.assertEqual(command[command.index('--port') + 1], '54321')
        self.assertIn('--no-proxy-headers', command)
        self.assertIn('--no-access-log', command)
        self.assertIs(kwargs['env'], env)
        self.assertEqual(kwargs['cwd'], cwd)
        self.assertIs(kwargs['stdout'], log)
        self.assertIs(kwargs['stderr'], log)

    def test_startup_wait_delegates_to_owned_server_check(self):
        wait = self.helper('_wait_for_test_server')
        testcase, child = object(), Mock(pid=4321)
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(restart, 'wait_for_owned_server') as owned:
                wait(testcase, child, Path(folder), 54321)
            owned.assert_called_once_with(testcase, child, str(Path(folder) / 'server.log'), 54321)

    def test_startup_failure_preserves_child_exit_and_server_log(self):
        wait = self.helper('_wait_for_test_server')
        original = AssertionError('server exited before binding')
        child = Mock(pid=4321)
        child.poll.return_value = 3221225477
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'server.log').write_text('last server startup evidence', encoding='utf-8')
            with patch.object(restart, 'wait_for_owned_server', side_effect=original):
                with self.assertRaises(AssertionError) as failure:
                    wait(object(), child, Path(folder), 54321)
        self.assertIs(failure.exception.__cause__, original)
        self.assertIn('server exited before binding', str(failure.exception))
        self.assertIn('4321', str(failure.exception))
        self.assertIn('3221225477', str(failure.exception))
        self.assertIn('last server startup evidence', str(failure.exception))

    def test_startup_failure_preserves_original_when_log_is_missing(self):
        wait = self.helper('_wait_for_test_server')
        original = AssertionError('startup failed')
        child = Mock(pid=4321)
        child.poll.return_value = 1
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(restart, 'wait_for_owned_server', side_effect=original):
                with self.assertRaises(AssertionError) as failure:
                    wait(object(), child, Path(folder), 54321)
        self.assertIs(failure.exception.__cause__, original)
        self.assertIn('startup failed', str(failure.exception))
        self.assertIn('unavailable', str(failure.exception).lower())

    def test_successful_status_request_returns_its_response(self):
        checked = self.helper('_request_with_diagnostics')
        expected = {'status': 'moving'}
        self.assertIs(checked(lambda: expected, lambda: 'unused'), expected)

    def test_status_get_failure_reports_process_evidence_and_chains_original(self):
        checked = self.helper('_request_with_diagnostics')
        original = URLError(ConnectionRefusedError('local status connection refused'))

        def get_status():
            raise original

        with self.assertRaises(AssertionError) as failure:
            checked(get_status, lambda: 'server_exit=3221225477; broker_exit=None')
        self.assertIs(failure.exception.__cause__, original)
        self.assertIn('local status connection refused', str(failure.exception))
        self.assertIn('server_exit=3221225477', str(failure.exception))

    def test_unreadable_diagnostics_does_not_hide_original_connection_error(self):
        checked = self.helper('_request_with_diagnostics')
        original = URLError(ConnectionResetError('local status connection reset'))

        def get_status():
            raise original

        def diagnostics():
            raise PermissionError('log denied')

        with self.assertRaises(AssertionError) as failure:
            checked(get_status, diagnostics)
        self.assertIs(failure.exception.__cause__, original)
        self.assertIn('local status connection reset', str(failure.exception))
        self.assertIn('log denied', str(failure.exception))

    def test_log_tail_is_bounded_and_keeps_the_end(self):
        read_tail = self.helper('_read_log_tail')
        with tempfile.TemporaryDirectory() as folder:
            logfile = Path(folder) / 'server.log'
            logfile.write_bytes(b'start-marker' + b'x' * 4096 + b'end-marker')
            tail = read_tail(logfile, limit=64)
        self.assertIsInstance(tail, str)
        self.assertIn('end-marker', tail)
        self.assertNotIn('start-marker', tail)
        self.assertLessEqual(len(tail), 128)

    def test_missing_or_unreadable_log_returns_a_diagnostic_marker(self):
        read_tail = self.helper('_read_log_tail')
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'missing.log'
            result = read_tail(missing, limit=64)
            self.assertIsInstance(result, str)
            self.assertIn('unavailable', result.lower())
            with patch.object(Path, 'open', side_effect=PermissionError('log denied')):
                result = read_tail(Path(folder) / 'locked.log', limit=64)
            self.assertIn('unavailable', result.lower())
            self.assertIn('log denied', result)


if __name__ == '__main__':
    unittest.main()
