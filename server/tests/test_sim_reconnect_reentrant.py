"""Old receiver callbacks cannot wait for the reconnect that joins them."""
import os
import subprocess
import sys
import unittest


class ReconnectReentrantTests(unittest.TestCase):
    def test_stop_callback_cannot_wait_for_its_own_shutdown(self):
        source = '''
from types import SimpleNamespace
from server.sim_runtime import SimRuntime
from server.tests.test_segment_recovery_mqtt import two_segment_map
r = SimRuntime('unused.sqlite', two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
               port=18883, client_id='reentrant-shutdown')
r._started = True
rejected = []
def on_close(*args, **kwargs):
    try:
        r.stop()
    except RuntimeError:
        rejected.append(True)
r.receiver = SimpleNamespace(_close_and_invalidate=on_close,
                             gate=SimpleNamespace(invalidate_many=lambda tasks: None), task_ids=('T1',))
r.stop()
assert rejected == [True]
assert r._stopped and not r._shutdown_running
print('rejected', flush=True)
'''
        result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True,
                                timeout=5, env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rejected', result.stdout)

    def test_old_network_thread_cannot_stop_while_its_workflow_is_joined(self):
        source = '''
import threading, time
from types import SimpleNamespace
from unittest.mock import patch
from server.sim_runtime import SimRuntime
from server.tests.test_segment_recovery_mqtt import two_segment_map
r = SimRuntime('unused.sqlite', two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
               port=18883, client_id='reentrant-reconnect')
r._started = True
callback_entered, callback_release, callback_done = (threading.Event() for _ in range(3))
errors = []
class Client:
    _thread = None
    def disconnect(self): pass
    def loop_stop(self): pass
class Workflow:
    _thread = None
    def stop(self):
        if not callback_done.wait(4): raise RuntimeError('callback did not finish')
c, w = Client(), Workflow()
r._client, r.workflow = c, w
r.receiver = SimpleNamespace(ready=False, _closed=True)
def callback():
    c._thread = threading.current_thread()
    callback_entered.set()
    callback_release.wait(4)
    try:
        r.stop()
    except RuntimeError:
        pass
    else:
        errors.append('callback stop was allowed')
    finally:
        callback_done.set()
def reconnect():
    try:
        with patch.object(r, '_new_client', side_effect=RuntimeError('expected')):
            r.reconnect()
    except RuntimeError:
        pass
t = threading.Thread(target=callback)
t.start()
assert callback_entered.wait(2)
worker = threading.Thread(target=reconnect)
worker.start()
deadline = time.monotonic() + 2
while r._client is not None and time.monotonic() < deadline:
    time.sleep(.01)
assert r._client is None
callback_release.set()
assert callback_done.wait(2), 'callback deadlocked waiting for its own reconnect'
t.join(2); worker.join(5)
assert not t.is_alive() and not worker.is_alive()
assert not errors
assert not r._stopped
print('rejected', flush=True)
'''
        result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True,
                                timeout=7, env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rejected', result.stdout)


if __name__ == '__main__':
    unittest.main()
