"""Lifecycle failures must never be reported as successful shutdown."""
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from server.sim_reroute_automatic import SimRerouteAutoSession
from server.order_demo_ui import HTML_PAGE
from server.order_pickup_workflow import PickupWorkflow
from server.tests.test_order_pickup_workflow import Runtime, Robot, map_data


class AutomaticLifecycleTest(unittest.TestCase):
    def test_dashboard_labels_virtual_reroute_wait_and_motion(self):
        self.assertIn("reroute_wait: '우회 경로 확인 중'", HTML_PAGE)
        self.assertIn("reroute_moving: '우회 경로 이동 중'", HTML_PAGE)

    def test_unobserved_reroute_motion_is_not_inferred_as_waiting(self):
        workflow = PickupWorkflow(Runtime(), Robot(), map_data(), {'red': {'pickup_node': 'C'}},
                                  simulation_enabled=True)
        workflow._status = 'rerouting'
        for observed in (None, {}, {'status': 'idle'}, {'status': 'unexpected'}):
            with self.subTest(observed=observed):
                workflow._robot_state = observed
                self.assertEqual(workflow.snapshot()['reroute_motion'], 'unknown')
        for actual, expected in [('stopped', 'waiting'), ('moving', 'moving')]:
            workflow._robot_state = {'status': actual}
            self.assertEqual(workflow.snapshot()['reroute_motion'], expected)

    @staticmethod
    def session():
        item = object.__new__(SimRerouteAutoSession)
        item._lifecycle_lock = threading.RLock()
        item._cleanup = ExitStack()
        item._cleanup_failure = None
        item._started = False
        item._phase = 'idle'
        return item

    def test_cleanup_failure_is_repeated_and_never_mistaken_for_success(self):
        session = self.session()
        def fail():
            raise OSError('cleanup error')
        session._cleanup.callback(fail)
        for _ in range(2):
            with self.assertRaises(RuntimeError) as caught:
                session.stop()
            self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(session._phase, 'stopped')

    def test_start_keeps_original_error_when_cleanup_also_fails(self):
        session = self.session()
        def fail_cleanup():
            raise OSError('cleanup error')
        def fail_start():
            raise ValueError('first failure')
        session._cleanup.callback(fail_cleanup)
        session.receipts = SimpleNamespace(initialize=fail_start)
        with self.assertRaisesRegex(ValueError, 'first failure') as caught:
            session.start()
        self.assertTrue(any('cleanup also failed' in item for item in caught.exception.__notes__))
        with self.assertRaises(RuntimeError):
            session.stop()


if __name__ == '__main__':
    unittest.main()
