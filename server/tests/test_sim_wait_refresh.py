"""Fresh normal waiting proof requires the same live, classified virtual stop."""
from contextlib import closing
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from server.sim_wait_refresh import refresh_wait, retry_wait
from server.tests.test_sim_reroute_multi_reservation import RerouteMultiReservationTest


class VirtualWaitRefreshTest(unittest.TestCase):
    def test_live_wait_refreshes_only_before_expiry_and_preserves_reservations(self):
        def exercise(runtime, robots):
            before = runtime.store.list_occupancy()
            with closing(sqlite3.connect(runtime.path)) as db:
                old_token, old_expiry = db.execute('SELECT challenge,expires FROM recovery_challenges WHERE task_id=?', ('T2',)).fetchone()
            with patch.object(runtime.gate, '_clock', return_value=old_expiry - 1):
                outcome = refresh_wait(runtime, 'T2', robots['sim-r2'], 'event-T2', simulation_enabled=True)
                self.assertEqual(outcome['state'], 'refreshed')
                self.assertGreater(outcome['expires'], old_expiry)
                self.assertEqual(refresh_wait(runtime, 'T2', robots['sim-r2'], 'event-T2', simulation_enabled=True)['state'], 'not_due')
            with closing(sqlite3.connect(runtime.path)) as db:
                new_token, new_expiry, status = db.execute('SELECT challenge,expires,status FROM recovery_challenges WHERE task_id=?', ('T2',)).fetchone()
            self.assertNotEqual(new_token, old_token); self.assertEqual(status, 'verified')
            self.assertEqual(runtime.inbox.get('sim-r2', 'event-T2')['state'], 'pending')
            self.assertEqual(runtime.store.list_occupancy(), before)
            with patch.object(runtime.gate, '_clock', return_value=new_expiry + 1):
                with self.assertRaises(ValueError):
                    refresh_wait(runtime, 'T2', robots['sim-r2'], 'event-T2', simulation_enabled=True)
            self.assertEqual(runtime.store.list_occupancy(), before)
            with self.assertRaises(ValueError):
                refresh_wait(runtime, 'T1', robots['sim-r1'], 'event-T1', simulation_enabled=True)
        RerouteMultiReservationTest(methodName='runTest')._exercise(after_prepare=exercise)

    def test_clock_rollback_is_permanently_blocked_even_when_not_due(self):
        def exercise(runtime, robots):
            held = runtime.store.list_occupancy()
            with closing(sqlite3.connect(runtime.path)) as db:
                expires = db.execute('SELECT expires FROM recovery_challenges WHERE task_id=?', ('T2',)).fetchone()[0]
            with patch.object(runtime.gate, '_clock', return_value=expires - 15):
                self.assertEqual(refresh_wait(runtime, 'T2', robots['sim-r2'], 'event-T2', simulation_enabled=True)['state'], 'not_due')
            with patch.object(runtime.gate, '_clock', return_value=expires - 20):
                with self.assertRaises(ValueError): refresh_wait(runtime, 'T2', robots['sim-r2'], 'event-T2', simulation_enabled=True)
            with patch.object(runtime.gate, '_clock', return_value=expires - 14):
                with self.assertRaises(ValueError): refresh_wait(runtime, 'T2', robots['sim-r2'], 'event-T2', simulation_enabled=True)
            self.assertIn('T2', runtime.gate._blocked)
            self.assertEqual(runtime.store.list_occupancy(), held)
            self.assertEqual(runtime.inbox.get('sim-r2', 'event-T2')['state'], 'pending')
        RerouteMultiReservationTest(methodName='runTest')._exercise(after_prepare=exercise)

    def test_retry_internal_clock_rollback_blocks_pending_event_and_occupancy(self):
        def exercise(runtime, robots):
            held = runtime.store.list_occupancy()
            with closing(sqlite3.connect(runtime.path)) as db:
                expires = db.execute('SELECT expires FROM recovery_challenges WHERE task_id=?', ('T2',)).fetchone()[0]
            # The first read is retry precheck; the next read occurs inside the
            # receiver's real progress transaction, after its occupied lookup.
            readings = iter((expires - 10, expires - 11, expires - 9))
            with patch.object(runtime.gate, '_clock', side_effect=lambda: next(readings)):
                with self.assertRaises(ValueError):
                    retry_wait(runtime, 'T2', robots['sim-r2'], 'event-T2', simulation_enabled=True)
            self.assertIn('T2', runtime.gate._blocked)
            self.assertEqual(runtime.store.list_occupancy(), held)
            self.assertEqual(runtime.inbox.get('sim-r2', 'event-T2')['state'], 'pending')
        RerouteMultiReservationTest(methodName='runTest')._exercise(after_prepare=exercise)

    def test_wrong_stopped_direction_is_rejected_without_refresh(self):
        def exercise(runtime, robots):
            held = runtime.store.list_occupancy(); robot = robots['sim-r2']
            with closing(sqlite3.connect(runtime.path)) as db:
                original, expires = db.execute('SELECT challenge,expires FROM recovery_challenges WHERE task_id=?', ('T2',)).fetchone()
            with patch.object(runtime.gate, '_clock', return_value=expires - 1), \
                    patch.object(robot.executor, 'field_stop_direction', return_value=('N02', 'wrong-edge')):
                with self.assertRaisesRegex(ValueError, 'direction'):
                    refresh_wait(runtime, 'T2', robot, 'event-T2', simulation_enabled=True)
            with closing(sqlite3.connect(runtime.path)) as db:
                self.assertEqual(db.execute('SELECT challenge FROM recovery_challenges WHERE task_id=?', ('T2',)).fetchone(), (original,))
            self.assertEqual(runtime.store.list_occupancy(), held)
        RerouteMultiReservationTest(methodName='runTest')._exercise(after_prepare=exercise)

    def test_executor_and_inbox_lock_order_does_not_deadlock(self):
        def exercise(runtime, robots):
            robot = robots['sim-r2']; gate = runtime.gate
            with closing(sqlite3.connect(runtime.path)) as db:
                expires = db.execute('SELECT expires FROM recovery_challenges WHERE task_id=?', ('T2',)).fetchone()[0]
            entered, go, inbox_acquired, finished = (threading.Event() for _ in range(4))
            errors = []
            def executor_worker():
                try:
                    with robot.executor._lock:
                        entered.set()
                        if not go.wait(8): raise RuntimeError('barrier not released')
                        with robot.executor._inbox._lock: inbox_acquired.set()
                except Exception as exc: errors.append(exc)
            def refresh_worker():
                try: refresh_wait(runtime, 'T2', robot, 'event-T2', simulation_enabled=True)
                except Exception as exc: errors.append(exc)
                finally: finished.set()
            with patch.object(gate, '_clock', return_value=expires - 1):
                worker = threading.Thread(target=executor_worker); worker.start()
                self.assertTrue(entered.wait(5))
                refresher = threading.Thread(target=refresh_worker); refresher.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not robot._motion_fence.locked(): time.sleep(.01)
                try:
                    self.assertTrue(robot._motion_fence.locked())
                    go.set()
                    self.assertTrue(inbox_acquired.wait(5), 'executor/inbox lock order deadlocked')
                    self.assertTrue(finished.wait(10), 'wait refresh did not finish')
                finally:
                    go.set(); worker.join(5); refresher.join(5)
                self.assertFalse(worker.is_alive()); self.assertFalse(refresher.is_alive()); self.assertEqual(errors, [])
        RerouteMultiReservationTest(methodName='runTest')._exercise(after_prepare=exercise)

    def test_failed_new_proof_revokes_it_without_losing_pending_or_occupancy(self):
        def exercise(runtime, robots):
            before = runtime.store.list_occupancy()
            with closing(sqlite3.connect(runtime.path)) as db:
                expires = db.execute('SELECT expires FROM recovery_challenges WHERE task_id=?', ('T2',)).fetchone()[0]
            with patch.object(runtime.gate, '_clock', return_value=expires - 1), \
                    patch.object(runtime.gate, 'complete', side_effect=RuntimeError('injected loss')):
                with self.assertRaises(RuntimeError):
                    refresh_wait(runtime, 'T2', robots['sim-r2'], 'event-T2', simulation_enabled=True)
            self.assertEqual(runtime.store.list_occupancy(), before)
            self.assertEqual(runtime.inbox.get('sim-r2', 'event-T2')['state'], 'pending')
            with closing(sqlite3.connect(runtime.path)) as db:
                self.assertEqual(db.execute('SELECT status FROM recovery_challenges WHERE task_id=?', ('T2',)).fetchone(), ('revoked',))
            self.assertIn('T2', runtime.gate._blocked)
        RerouteMultiReservationTest(methodName='runTest')._exercise(after_prepare=exercise)


if __name__ == '__main__':
    unittest.main()
