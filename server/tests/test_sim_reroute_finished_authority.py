"""A finished generation requires live authority, not only durable history."""
import unittest
from unittest.mock import patch

from server.sim_reroute_execute_wire import _execution
from server.sim_reroute_finish import finish_reroute
from server.tests import test_sim_reroute_finish_wire as fixture


class FinishedAuthorityTest(unittest.TestCase):
    def test_finished_generation_requires_same_live_session_and_key(self):
        def finish_and_verify(runtime, task, event, guard):
            result = finish_reroute(runtime, task, event, guard)
            robot = guard.robot_receiver.robot
            receiver = runtime.receiver
            with guard.robot_receiver._lifecycle_lock, guard.robot_receiver._connection_fence, \
                    guard.activation_receiver.endpoint._connection_fence, \
                    guard._execution_receiver.endpoint._connection_fence, robot._lock, runtime._lock, \
                    receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                with runtime.progress._transaction() as db:
                    payload, data = _execution(db, guard, task, robot, state='finished')
                    self.assertTrue(payload)
                    self.assertEqual(data['nodes'][-1], 'D1')
                    runtime.gate._local.db = None
                    try:
                        with self.assertRaises(ValueError):
                            _execution(db, guard, task, robot, state='finished')
                    finally:
                        runtime.gate._local.db = db
                    original = guard.receipt_inbox._keys.pop(robot.robot_id)
                    try:
                        with self.assertRaises(ValueError):
                            _execution(db, guard, task, robot, state='finished')
                    finally:
                        guard.receipt_inbox._keys[robot.robot_id] = original
                    original = runtime._tasks[task]
                    try:
                        runtime._tasks[task] = (robot.robot_id, 'different-session')
                        with self.assertRaises(ValueError):
                            _execution(db, guard, task, robot, state='finished')
                    finally:
                        runtime._tasks[task] = original
                    with runtime.gate._state_lock:
                        generation = runtime.gate._generations.get(task, 0)
                        runtime.gate._generations[task] = generation + 1
                    try:
                        with self.assertRaises(ValueError):
                            _execution(db, guard, task, robot, state='finished')
                    finally:
                        with runtime.gate._state_lock:
                            runtime.gate._generations[task] = generation
                    db.execute("UPDATE recovery_challenges SET status='revoked' WHERE task_id=?", (task,))
                    try:
                        with self.assertRaises(ValueError):
                            _execution(db, guard, task, robot, state='finished')
                    finally:
                        db.execute("UPDATE recovery_challenges SET status='verified' WHERE task_id=?", (task,))
                    snapshot = robot.executor.snapshot()
                    snapshot['current_node'] = 'wrong-stop'
                    with patch.object(robot.executor, 'snapshot', return_value=snapshot):
                        with self.assertRaises(ValueError):
                            _execution(db, guard, task, robot, state='finished')
                    parent_snapshot = {'status': 'stopped', 'reroute_transition_id': None,
                        'receipt': {'order_id': task, 'seq': data['parent_seq'],
                                    'digest': data['parent_digest'], 'state': 'completed'}}
                    with patch.object(robot.executor, 'snapshot', return_value=parent_snapshot), \
                            patch.object(robot.executor, 'field_stop_direction',
                                         return_value=(data['stopped_at'], data['incoming_edge'])):
                        with self.assertRaises(ValueError):
                            _execution(db, guard, task, robot, state='finished')
            return result

        with patch.object(fixture, 'finish_reroute', side_effect=finish_and_verify):
            fixture.RerouteFinishWireTest(
                methodName='test_new_clearance_releases_union_and_unblocks_second_robot'
            ).test_new_clearance_releases_union_and_unblocks_second_robot()
