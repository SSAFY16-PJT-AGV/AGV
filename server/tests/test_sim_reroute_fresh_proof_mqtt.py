"""Fresh stop authority is bound to the same live virtual generation."""
from contextlib import closing
import sqlite3
import time
import unittest
from unittest.mock import patch

from server.sim_reroute_activation import stage_activation
from server.sim_reroute_activate_wire import publish_activation
from server.sim_reroute_execute_wire import prepare_execution, publish_execution
from server.sim_reroute_multi_progress import stage_next
from server.sim_reroute_automatic import SimRerouteAutoSession
from server.sim_reroute_wire import publish_proposal
from server.tests.test_sim_reroute_multi_reservation import RerouteMultiReservationTest


class FreshStopProofMqttTest(unittest.TestCase):
    def test_expired_parent_requires_same_live_unexpired_stop_and_history(self):
        def exercise(runtime, robots):
            robot = robots['sim-r1']; original = runtime.store.list_occupancy()
            session = SimRerouteAutoSession(runtime, robot, 'boot-1', key=bytes(range(32)), simulation_enabled=True).start()
            def wait(predicate):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if predicate(): return
                    time.sleep(.02)
                self.fail('timed out waiting for isolated proof MQTT')
            try:
                publish_proposal(runtime, 'T1'); wait(lambda: session.receipts.get('T1') is not None)
                stage_activation(runtime, 'T1', session.receipts, session.proposal)
                publish_activation(runtime, 'T1', session.receipts, session.proposal)
                wait(lambda: session.activation_receipts.get('T1') is not None)
                prepare_execution(runtime, 'T1', session.guard, session.execution_receiver)
                publish_execution(runtime, 'T1', session.guard, session.execution_receiver)
                wait(lambda: robot.executor.snapshot()['status'] == 'moving')
                state = robot.tick(20)
                self.assertEqual((state['status'], state['current_node']), ('stopped', 'N01'))
                event = {'type': 'rear_clear', 'event_id': 'proof-stop-2', 'robot_session': 'boot-1',
                         'order_id': 'T1', 'seq': 2, 'map_id': 'mat_ssafy_v3@v0',
                         'stopped_at': 'N01', 'cleared_resources': []}
                robot.publish_event(event); wait(lambda: ('sim-r1', event['event_id']) in runtime.receiver._occupied_events)
                stage_next(runtime, 'T1', event['event_id'], session.guard)
                with closing(sqlite3.connect(runtime.path)) as db:
                    payload = db.execute('SELECT payload FROM sim_reroute_execution_attempts WHERE task_id=?', ('T1',)).fetchone()[0]
                    expires, challenge = db.execute('SELECT expires,challenge FROM recovery_challenges WHERE task_id=?', ('T1',)).fetchone()
                later = expires + .01
                self.assertLess(later, session.guard._fresh_stop_proof._lease.expires)
                with patch.object(runtime.gate, '_clock', return_value=later):
                    with session.guard.guard(payload): pass
                    with closing(sqlite3.connect(runtime.path)) as db:
                        db.execute('UPDATE recovery_challenges SET challenge=? WHERE task_id=?', ('tampered', 'T1'))
                        db.commit()
                    with self.assertRaises((ValueError, RuntimeError)):
                        with session.guard.guard(payload): pass
                    with closing(sqlite3.connect(runtime.path)) as db:
                        db.execute('UPDATE recovery_challenges SET challenge=? WHERE task_id=?', (challenge, 'T1'))
                        db.commit()
                    with session.guard.guard(payload): pass
                    key = session.receipts._keys.pop('sim-r1')
                    try:
                        with self.assertRaises((ValueError, RuntimeError)):
                            with session.guard.guard(payload): pass
                    finally:
                        session.receipts._keys['sim-r1'] = key
                    original_driver = robot._reroute_driver
                    robot._reroute_driver = tuple(list(original_driver))
                    try:
                        with self.assertRaises((ValueError, RuntimeError)):
                            with session.guard.guard(payload): pass
                    finally:
                        robot._reroute_driver = original_driver
                    with patch.object(session.proposal._client, 'is_connected', return_value=False):
                        with self.assertRaises((ValueError, RuntimeError)):
                            with session.guard.guard(payload): pass
                    previous_generation = runtime.gate._generations.get('T1', 0)
                    runtime.gate._generations['T1'] = previous_generation + 1
                    try:
                        with self.assertRaises((ValueError, RuntimeError)):
                            with session.guard.guard(payload): pass
                    finally:
                        runtime.gate._generations['T1'] = previous_generation
                    with patch.object(runtime.gate, '_clock', return_value=session.guard._fresh_stop_proof._lease.expires + 1):
                        with self.assertRaises((ValueError, RuntimeError)):
                            with session.guard.guard(payload): pass
                    with patch.object(runtime.gate, '_clock', return_value=later - 1):
                        with self.assertRaises((ValueError, RuntimeError)):
                            with session.guard.guard(payload): pass
                    with self.assertRaises((ValueError, RuntimeError)):
                        with session.guard.guard(payload): pass
                self.assertEqual(runtime.store.list_occupancy(), original)
            finally:
                session.stop()
        RerouteMultiReservationTest(methodName='runTest')._exercise(after_prepare=exercise)


if __name__ == '__main__':
    unittest.main()
