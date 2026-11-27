"""Fresh waiting proof extends only the bounded virtual observation period."""
import unittest
from server.order_pickup_workflow import PickupWorkflow
from server.tests.test_order_pickup_workflow import Runtime, Robot, map_data, state


class WaitingSession:
    def __init__(self): self.result = 'not_due'; self.calls = 0
    def retry_waiting(self, task, event): return {'status': 'occupied'}
    def refresh_waiting(self, task, event):
        self.calls += 1
        return {'state': self.result}
    def begin(self, task): return False


class BlockedReceiver:
    def retry_occupied_event(self, robot, event): return {'status': 'occupied'}


class WaitingWorkflowLimitTest(unittest.TestCase):
    def make_workflow(self, *, timeout):
        clock = [10.0]; runtime, robot = Runtime(), Robot(); runtime.receiver = BlockedReceiver()
        wf = PickupWorkflow(runtime, robot, map_data(), {'red': {'pickup_node': 'C'}},
                            simulation_enabled=True, clock=lambda: clock[0], timeout_seconds=timeout,
                            wait_for_reservation=True)
        session = WaitingSession(); wf.reroute_session = session
        wf.submit('red'); robot.states = [state('stopped', 'B', 1, 'B')]
        self.assertEqual(wf.tick(0)['status'], 'waiting_progress')
        event = robot.events[0]; runtime.inbox.rows[('sim-r1', event['event_id'])] = {'state': 'pending'}
        return clock, wf, session, robot

    def test_only_verified_refresh_moves_the_observation_deadline(self):
        clock, wf, session, robot = self.make_workflow(timeout=5)
        clock[0] = 14.0
        self.assertEqual(wf.tick(0)['status'], 'waiting_reservation'); self.assertEqual(wf._deadline, 15)
        clock[0] = 14.5; session.result = 'refreshed'
        self.assertEqual(wf.tick(0)['status'], 'waiting_reservation'); self.assertEqual(wf._deadline, 19.5)
        clock[0] = 19.4; session.result = 'not_due'
        self.assertEqual(wf.tick(0)['status'], 'waiting_reservation'); self.assertEqual(wf._deadline, 19.5)
        clock[0] = 19.6
        self.assertEqual(wf.tick(0)['status'], 'unknown'); self.assertEqual(len(robot.events), 1)

    def test_total_wait_is_bounded_even_when_new_proofs_are_verified(self):
        clock, wf, session, robot = self.make_workflow(timeout=300)
        session.result = 'refreshed'; clock[0] = 10.3
        self.assertEqual(wf.tick(0)['status'], 'waiting_reservation')
        clock[0] = 129.9
        self.assertEqual(wf.tick(0)['status'], 'waiting_reservation')
        clock[0] = 130.3
        self.assertEqual(wf.tick(0)['status'], 'unknown'); self.assertEqual(len(robot.events), 1)


if __name__ == '__main__': unittest.main()
