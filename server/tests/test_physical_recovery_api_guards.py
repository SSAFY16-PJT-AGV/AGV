"""Manual simulation recovery must not assert facts about a physical robot."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from server.sim_runtime import SimRuntime


def runtime_for(robot):
    runtime = SimRuntime.__new__(SimRuntime)
    runtime._lock = threading.RLock()
    runtime.receiver = SimpleNamespace(_dispatch_lock=threading.RLock())
    runtime._ready = lambda: None
    runtime._tasks = {'T1': (robot, 'session')}
    runtime.gate = Mock()
    runtime.gate._state_lock = threading.RLock()
    runtime.gate._blocked = {'T1'}
    runtime.store = Mock()
    return runtime


class PhysicalRecoveryApiGuardTests(unittest.TestCase):
    def test_manual_recovery_does_not_assert_physical_stop(self):
        runtime = runtime_for('physical-1')
        with self.assertRaisesRegex(ValueError, 'physical'):
            runtime.begin('T1', 'session')
        with self.assertRaisesRegex(ValueError, 'physical'):
            runtime.complete('T1', 'session', 'challenge', 'physical-1', 'T1', 1,
                             'map@v1', 'A', 'digest', True)
        with self.assertRaisesRegex(ValueError, 'physical'):
            runtime.reset_unallocated('T1', 'session')
        runtime.gate.begin.assert_not_called()
        runtime.gate.complete.assert_not_called()
        runtime.gate.reset_unallocated.assert_not_called()

    def test_create_and_reset_cannot_unblock_physical_task(self):
        runtime = runtime_for('physical-1')
        with self.assertRaisesRegex(ValueError, 'physical'):
            runtime.create_task('T1', reset_unallocated=True)
        runtime.store.create_task.assert_not_called()
        runtime.gate.reset_unallocated.assert_not_called()

    def test_simulation_manual_recovery_is_unchanged(self):
        runtime = runtime_for('sim-r1')
        runtime.gate.begin.return_value = 'challenge'
        self.assertEqual(runtime.begin('T1', 'session'), {'challenge': 'challenge'})
        self.assertEqual(runtime.reset_unallocated('T1', 'session'), {'task_id': 'T1', 'reset': True})
        runtime.complete('T1', 'session', 'challenge', 'sim-r1', 'T1', 1,
                         'map@v1', 'A', 'digest', True)
        runtime.gate.complete.assert_called_once()


if __name__ == '__main__':
    unittest.main()
