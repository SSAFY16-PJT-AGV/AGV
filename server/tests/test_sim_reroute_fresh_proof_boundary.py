"""Process-local stop proof clock and lock ownership are fail-closed."""
from contextlib import contextmanager
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from server.sim_reroute_fresh_proof import FreshStopProof


class FreshStopProofBoundaryTest(unittest.TestCase):
    def test_clock_rollback_permanently_invalidates_the_lease(self):
        value = [20.0]
        proof = object.__new__(FreshStopProof)
        proof.guard = SimpleNamespace(runtime=SimpleNamespace(gate=SimpleNamespace(_now=lambda: value[0])))
        proof._last_now = None; proof._lease = object(); proof._clock_failed = False
        self.assertEqual(proof._now(), 20.0)
        value[0] = 19.0
        with self.assertRaises(ValueError): proof._now()
        self.assertIsNone(proof._lease)
        value[0] = 21.0
        with self.assertRaises(ValueError): proof._now()

    def test_motion_fence_requires_actual_calling_thread_owner(self):
        class Owned:
            def _is_owned(self): return True
        db = SimpleNamespace(in_transaction=True)
        gate = SimpleNamespace(_local=SimpleNamespace(db=db))
        robot = SimpleNamespace(_motion_fence=threading.Lock(), _lock=Owned())
        receiver = SimpleNamespace(_dispatch_lock=Owned())
        runtime = SimpleNamespace(receiver=receiver, gate=gate, _lock=Owned(), inbox=SimpleNamespace(_lock=Owned()),
                                  _ready=lambda: None)
        proposal = SimpleNamespace(robot=robot, _lifecycle_lock=Owned(), _connection_fence=Owned())
        endpoint = SimpleNamespace(_connection_fence=Owned())
        guard = SimpleNamespace(runtime=runtime, robot_receiver=proposal,
                                activation_receiver=SimpleNamespace(endpoint=endpoint),
                                _execution_receiver=SimpleNamespace(endpoint=endpoint))
        proof = object.__new__(FreshStopProof)
        proof.guard = guard; proof._motion_owner = None; proof._motion_lock = None
        entered, release = threading.Event(), threading.Event()
        failures = []
        def worker():
            try:
                with proof.motion_fence(robot):
                    entered.set()
                    if not release.wait(5): failures.append('timeout')
            except Exception as error:
                failures.append(repr(error))
        thread = threading.Thread(target=worker); thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(ValueError, 'fences'):
                proof._check_fences(db, robot)
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(failures, [])
        self.assertIsNone(proof._motion_owner)
        with proof.motion_fence(robot):
            proof._check_fences(db, robot)
            with self.assertRaises(RuntimeError):
                with proof.motion_fence(robot): pass

    def test_later_stop_must_revalidate_previous_committed_proof(self):
        proof = object.__new__(FreshStopProof)
        proof._lease = SimpleNamespace(task='T1', final=False)
        with patch.object(proof, '_check_fences'), \
                patch.object(proof, 'authorize', side_effect=ValueError('expired prior')) as prior, \
                patch('server.sim_reroute_execute_wire._execution') as execute:
            with self.assertRaisesRegex(ValueError, 'expired prior'):
                proof.issue(object(), 'T1', object(), 'event', final=False)
        prior.assert_called_once()
        execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
