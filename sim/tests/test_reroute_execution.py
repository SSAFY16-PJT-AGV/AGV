"""Execution intent is recorded once and does not itself authorize motion."""
import copy
import hashlib
import json
from contextlib import closing
import sqlite3
import unittest

import sim.tests.test_reroute_activation as fixture
from sim.order_inbox import OrderRejected
from sim.reroute_execution import SimRerouteExecution


class RerouteExecutionTest(unittest.TestCase):
    def setUp(self):
        self.base = fixture.RerouteActivationTest(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.send(self.base.message)
        self.execution = SimRerouteExecution(self.base.activation)
        self.execution.initialize()
        self.request = {'type': 'sim.reroute.execute.v1', 'data': dict(self.base.commit['data'],
            activation_digest=hashlib.sha256(fixture.canonical(self.base.commit)).hexdigest())}

    def send(self, message):
        return self.execution.record(fixture.canonical(message))

    def test_requires_prior_activation_and_never_starts_motion(self):
        before = self.base.executor.snapshot()
        with self.assertRaises(OrderRejected):
            self.send(self.request)
        with closing(sqlite3.connect(self.base.inbox.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_activations').fetchone(), (0,))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_executions').fetchone(), (0,))
        self.base.send_commit(self.base.commit)
        self.assertTrue(self.send(self.request)['new'])
        self.assertEqual(self.send(self.request)['state'], 'recorded_not_executed')
        self.execution.initialize()
        self.assertFalse(self.send(self.request)['new'])
        self.assertEqual(self.base.executor.tick(10), before)

    def test_modified_generation_session_parent_and_plan_are_rejected(self):
        self.base.send_commit(self.base.commit)
        for key, value in [('parent_seq', 2), ('parent_digest', '0' * 64), ('stopped_at', 'W2'),
                           ('incoming_edge', 'E0~r'), ('robot_session', 'other'), ('generation', 2),
                           ('action_at_end', {'type': 'unload'}), ('nodes', ['W1', 'W2', 'D1']),
                           ('proposal_digest', '0' * 64), ('activation_digest', '0' * 64),
                           ('transition_id', '2' * 32)]:
            altered = copy.deepcopy(self.request)
            altered['data'][key] = value
            with self.subTest(key=key), self.assertRaises(OrderRejected):
                self.send(altered)
        self.assertEqual(self.base.executor.snapshot()['status'], 'stopped')
        self.assertTrue(self.send(self.request)['new'])

    def test_database_failure_and_corruption_fail_closed(self):
        self.base.send_commit(self.base.commit)
        with closing(sqlite3.connect(self.base.inbox.path)) as db, db:
            db.execute("CREATE TRIGGER reject_execution BEFORE INSERT ON sim_reroute_executions "
                       "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(OrderRejected):
            self.send(self.request)
        with closing(sqlite3.connect(self.base.inbox.path)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_executions').fetchone(), (0,))
            db.execute('DROP TRIGGER reject_execution')
        self.assertTrue(self.send(self.request)['new'])
        with closing(sqlite3.connect(self.base.inbox.path)) as db, db:
            db.execute("UPDATE sim_reroute_executions SET digest=?", ('0' * 64,))
        with self.assertRaises(OrderRejected):
            self.send(self.request)
        with self.assertRaises(OrderRejected):
            self.execution.initialize()
        self.assertEqual(self.base.executor.snapshot()['status'], 'stopped')

    def test_old_route_motion_makes_execution_intent_stale(self):
        self.base.send_commit(self.base.commit)
        order = dict(self.base.original, seq=2, released_until='W2')
        self.base.executor.accept(json.dumps(order).encode('utf-8'))
        with self.assertRaises(OrderRejected):
            self.send(self.request)
        with closing(sqlite3.connect(self.base.inbox.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_executions').fetchone(), (0,))


if __name__ == '__main__':
    unittest.main()
