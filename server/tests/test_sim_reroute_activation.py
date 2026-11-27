"""Server stages a new virtual generation only after signed live acceptance."""
import sqlite3
import time
import unittest
from contextlib import closing
from unittest.mock import patch

from server.sim_reroute_activation import stage_activation
from server.sim_reroute_receipt import SimRerouteReceiptInbox, SimRerouteReceiptReceiver
from server.sim_reroute_wire import publish_proposal, SimRerouteProposalGuard
from server.tests.test_sim_reroute_reservation import RerouteReservationTest
from sim.reroute_acceptance import SimRerouteAcceptance
from sim.reroute_mqtt import SimRerouteMqttReceiver


class RerouteActivationTest(unittest.TestCase):
    def test_stage_requires_live_signed_receipt_and_preserves_both_holds(self):
        def after_prepare(runtime, robots):
            key = bytes(range(32))
            inbox = SimRerouteReceiptInbox(runtime, {'sim-r1': key})
            inbox.initialize()
            observer = SimRerouteReceiptReceiver(inbox)
            robot = SimRerouteMqttReceiver(robots['sim-r1'], SimRerouteProposalGuard(runtime, robot_id='sim-r1'),
                SimRerouteAcceptance(robots['sim-r1'].executor, robot_session='boot-1'), receipt_key=key)
            try:
                observer.start()
                robot.start()
                held = runtime.store.list_occupancy()
                with self.assertRaises((ValueError, RuntimeError)):
                    stage_activation(runtime, 'T1', inbox, robot)
                publish_proposal(runtime, 'T1')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and inbox.get('T1') is None:
                    time.sleep(.02)
                self.assertIsNotNone(inbox.get('T1'))
                from server.sim_reroute_activation import _insert_activation
                def fail_after_insert(*args):
                    _insert_activation(*args)
                    raise RuntimeError('injected activation staging failure')
                with patch('server.sim_reroute_activation._insert_activation', side_effect=fail_after_insert):
                    with self.assertRaises(RuntimeError):
                        stage_activation(runtime, 'T1', inbox, robot)
                with closing(sqlite3.connect(runtime.path)) as db:
                    table = db.execute("SELECT 1 FROM sqlite_master WHERE name='sim_reroute_activation_attempts'").fetchone()
                    if table:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_activation_attempts').fetchone(), (0,))
                result = stage_activation(runtime, 'T1', inbox, robot)
                self.assertEqual(result['state'], 'staged_not_published')
                self.assertFalse(result['motion_authorized'])
                self.assertEqual(stage_activation(runtime, 'T1', inbox, robot)['transition_id'], result['transition_id'])
                self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
                self.assertEqual(runtime.store.list_occupancy(), held)
                # A fresh process without this robot session's key cannot
                # synthesize an activation from the durable DB alone.
                bad = SimRerouteReceiptInbox(runtime, {'sim-r1': bytes([4]) * 32})
                with self.assertRaises(ValueError):
                    stage_activation(runtime, 'T1', bad, robot)
                robot.stop()
                with self.assertRaises(ValueError):
                    stage_activation(runtime, 'T1', inbox, robot)
            finally:
                robot.stop()
                observer.stop()
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)


if __name__ == '__main__':
    unittest.main()
