"""A reentrant shutdown must never wait on its own reference operation."""
import os
import subprocess
import sys
import unittest


class InvestigationReentrantStopTests(unittest.TestCase):
    def test_reference_owner_cannot_stop_itself(self):
        source = '''
from server.sim_runtime import SimRuntime
from server.tests.test_segment_recovery_mqtt import two_segment_map
runtime = SimRuntime('unused.sqlite', two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                     port=18883, client_id='reentrant-test', investigation_notes=True)
runtime._started = True
runtime.investigation_notes = object()
with runtime._reference_operation():
    try:
        runtime.stop()
    except RuntimeError:
        print('rejected', flush=True)
    else:
        raise AssertionError('reentrant stop succeeded')
assert runtime._reference_operations == 0
assert not runtime._stopped
'''
        result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True,
                                timeout=4, env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rejected', result.stdout)


if __name__ == '__main__':
    unittest.main()
