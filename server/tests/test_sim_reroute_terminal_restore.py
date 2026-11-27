"""Terminal virtual reroute restores the actual route, not its old parent."""
import os
import sqlite3
import unittest
from unittest.mock import patch

from server.sim_runtime import SimRuntime
from server.sim_reroute_restore import verify_saved_reroute_robot
from server.tests import test_sim_reroute_finish_wire as fixture
from sim.order_executor import SimOrderExecutor


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class TerminalRerouteRestoreTest(unittest.TestCase):
    def test_actual_detour_terminal_pose_and_incoming_edge_are_restored(self):
        real_prepare = SimRuntime.prepare_field
        real_finish = fixture.finish_reroute
        real_exercise = fixture.RerouteReservationTest._exercise
        seen = []

        def prepare(runtime, task, dest, candidates, action, **kwargs):
            return real_prepare(runtime, task, dest, candidates,
                                {'type': 'unload'} if task == 'T1' else action, **kwargs)

        def only_handoff(test, handoff, after_prepare=None):
            if handoff:
                return real_exercise(test, handoff, after_prepare=after_prepare)

        class ValidatedTeamJournals:
            def validate(self, store, db):
                return True  # The separate team journal tests exercise this boundary.

        def finish(runtime, task, event, guard):
            result = real_finish(runtime, task, event, guard)
            original = guard.robot_receiver.robot.executor
            expected = original.snapshot()
            fresh = SimOrderExecutor(original._inbox, runtime.map_data, 'S1', experimental_field_map=True,
                                     field_incoming_edge='IN1')
            old_reopened, old_validator = runtime.store._reopened, runtime._team_shelf_recovery
            runtime.store._reopened = True
            runtime._team_shelf_recovery = ValidatedTeamJournals()
            try:
                ack = {'order_id': task, 'action_id': 'place-test', 'agv_id': 'AGV_01',
                       'action': 'place', 'applied': True}
                evidence = runtime.restore_completed_executor(fresh, task_id=task, session='boot-1',
                    event_id=event, action_ack=ack, expected_twin_id='AGV_01')
                actual = fresh.snapshot()
                self.assertEqual(actual['pose'], expected['pose'])
                self.assertEqual(fresh.field_stop_direction(), original.field_stop_direction())
                self.assertEqual(actual['order'], expected['order'])
                self.assertEqual(evidence['seq'], expected['receipt']['seq'])
                self.assertEqual(evidence['digest'], expected['receipt']['digest'])
                with sqlite3.connect(runtime.path) as db:
                    self.assertTrue(verify_saved_reroute_robot(db, runtime.map_data, original._inbox.path,
                                                               task, 'sim-r1', 'boot-1'))
                # Neither a corrupted stop event nor a changed robot inbox
                # may produce a reconstructed or moving executor.
                for path, select_sql, update_sql, value in (
                    (runtime.path, 'SELECT result FROM sim_event_inbox WHERE robot_id=? AND event_id=?',
                     'UPDATE sim_event_inbox SET result=? WHERE robot_id=? AND event_id=?', '{}'),
                    (original._inbox.path, 'SELECT digest FROM sim_inbox_orders WHERE order_id=? AND seq=?',
                     'UPDATE sim_inbox_orders SET digest=? WHERE order_id=? AND seq=?', '0' * 64),
                ):
                    keys = ('sim-r1', event) if path == runtime.path else (task, evidence['seq'])
                    with sqlite3.connect(path) as db:
                        saved = db.execute(select_sql, keys).fetchone()[0]
                        db.execute(update_sql, (value, *keys))
                    try:
                        rejected = SimOrderExecutor(original._inbox, runtime.map_data, 'S1',
                            experimental_field_map=True, field_incoming_edge='IN1')
                        with self.assertRaises((ValueError, RuntimeError)):
                            runtime.restore_completed_executor(rejected, task_id=task, session='boot-1',
                                event_id=event, action_ack=ack, expected_twin_id='AGV_01')
                        if path != runtime.path:
                            with sqlite3.connect(runtime.path) as db:
                                with self.assertRaises((ValueError, RuntimeError)):
                                    verify_saved_reroute_robot(db, runtime.map_data, original._inbox.path,
                                                               task, 'sim-r1', 'boot-1')
                        self.assertEqual(rejected.snapshot()['status'], 'idle')
                    finally:
                        with sqlite3.connect(path) as db:
                            db.execute(update_sql, (saved, *keys))
                seen.append(True)
            finally:
                runtime.store._reopened = old_reopened
                runtime._team_shelf_recovery = old_validator
            return result

        with patch.object(SimRuntime, 'prepare_field', prepare), \
                patch.object(fixture.RerouteReservationTest, '_exercise', only_handoff), \
                patch.object(fixture, 'finish_reroute', side_effect=finish):
            fixture.RerouteFinishWireTest(
                methodName='test_new_clearance_releases_union_and_unblocks_second_robot'
            ).test_new_clearance_releases_union_and_unblocks_second_robot()
        self.assertEqual(seen, [True])
