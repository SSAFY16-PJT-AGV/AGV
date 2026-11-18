"""Reentrant stop from a pinned read must fail rather than wait for itself."""
import os
import subprocess
import sys
import unittest


class ReadReentrantStopTests(unittest.TestCase):
    def test_all_pinned_reads_reject_same_thread_stop(self):
        source = '''
from unittest.mock import patch
from server.sim_runtime import SimRuntime
from server.tests.test_segment_recovery_mqtt import two_segment_map
for method in ('task_diagnostics', 'task_status', 'pending_events'):
    runtime = SimRuntime('unused.sqlite', two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                         port=18883, client_id='reentrant-read')
    runtime._started = True
    def nested_stop(*args, **kwargs):
        runtime.stop()
        raise AssertionError('reentrant stop succeeded')
    try:
        with patch('server.sim_runtime.sqlite3.connect', side_effect=nested_stop):
            getattr(runtime, method)(*(() if method == 'pending_events' else ('T1',)))
    except RuntimeError as exc:
        assert 'cannot stop from a reference operation' in str(exc)
    else:
        raise AssertionError('reentrant stop was not rejected')
    assert not runtime._stopped
    assert runtime._reference_operations == 0
print('rejected', flush=True)
'''
        result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True,
                                timeout=5, env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rejected', result.stdout)


if __name__ == '__main__':
    unittest.main()
