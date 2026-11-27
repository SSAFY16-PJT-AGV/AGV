"""A robot must durably stage a verified generation before any motion."""
import copy
import hashlib
import json
from contextlib import closing
import sqlite3
import unittest

import sim.tests.test_reroute_acceptance as fixture
from sim.order_inbox import OrderRejected
from sim.reroute_activation import SimRerouteActivation


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


class RerouteActivationTest(fixture.RerouteAcceptanceTest):
    def setUp(self):
        super().setUp()
        self.activation = SimRerouteActivation(self.receiver)
        self.activation.initialize()
        self.commit = {'type': 'sim.reroute.activate.v1', 'data': dict(self.message['data'],
            proposal_digest=hashlib.sha256(canonical(self.message)).hexdigest())}

    def send_commit(self, message):
        return self.activation.record(canonical(message))

    def test_activation_requires_a_durably_accepted_matching_proposal(self):
        before = self.executor.snapshot()
        with self.assertRaises(OrderRejected):
            self.send_commit(self.commit)
        with closing(sqlite3.connect(self.inbox.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_receipts').fetchone(), (0,))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_activations').fetchone(), (0,))
        self.send(self.message)
        first = self.send_commit(self.commit)
        self.assertTrue(first['new'])
        self.assertEqual(first['state'], 'recorded_not_executed')
        self.assertEqual(first['transition_id'], self.commit['data']['transition_id'])
        self.assertEqual(self.executor.tick(10), before)
        self.assertFalse(self.send_commit(self.commit)['new'])
        self.activation.initialize()
        self.assertFalse(self.send_commit(self.commit)['new'])
        self.assertEqual(self.executor.snapshot(), before)

    def test_activation_rejects_changed_parent_session_route_and_digest(self):
        self.send(self.message)
        for key, value in [('parent_seq', 2), ('parent_digest', '0' * 64), ('stopped_at', 'W2'),
                           ('incoming_edge', 'E0~r'), ('robot_session', 'other'), ('generation', 2),
                           ('action_at_end', {'type': 'unload'}), ('nodes', ['W1', 'W2', 'D1']),
                           ('proposal_digest', '0' * 64), ('transition_id', '2' * 32)]:
            altered = copy.deepcopy(self.commit)
            altered['data'][key] = value
            with self.subTest(key=key), self.assertRaises(OrderRejected):
                self.send_commit(altered)
        self.assertEqual(self.executor.snapshot()['status'], 'stopped')
        self.assertTrue(self.send_commit(self.commit)['new'])

    def test_failed_activation_insert_rolls_back_and_preserves_parent(self):
        self.send(self.message)
        with closing(sqlite3.connect(self.inbox.path)) as db, db:
            db.execute("CREATE TRIGGER reject_activation BEFORE INSERT ON sim_reroute_activations "
                       "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(OrderRejected):
            self.send_commit(self.commit)
        self.assertEqual(self.executor.snapshot()['status'], 'stopped')
        with closing(sqlite3.connect(self.inbox.path)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_activations').fetchone(), (0,))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_receipts').fetchone(), (1,))
            db.execute('DROP TRIGGER reject_activation')
        self.assertTrue(self.send_commit(self.commit)['new'])

    def test_corrupted_activation_record_fails_closed(self):
        self.send(self.message)
        self.send_commit(self.commit)
        with closing(sqlite3.connect(self.inbox.path)) as db, db:
            db.execute("UPDATE sim_reroute_activations SET digest=?", ('0' * 64,))
        with self.assertRaises(OrderRejected):
            self.send_commit(self.commit)
        with self.assertRaises(OrderRejected):
            self.activation.initialize()
        self.assertEqual(self.executor.snapshot()['status'], 'stopped')

    def test_parent_command_change_or_motion_rejects_activation(self):
        self.send(self.message)
        next_order = dict(self.original, seq=2, released_until='W2')
        self.executor.accept(json.dumps(next_order).encode('utf-8'))
        self.assertEqual(self.executor.snapshot()['status'], 'moving')
        with self.assertRaises(OrderRejected):
            self.send_commit(self.commit)
        with closing(sqlite3.connect(self.inbox.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_activations').fetchone(), (0,))

    def test_corrupted_parent_history_never_records_activation(self):
        self.send(self.message)
        with closing(sqlite3.connect(self.inbox.path)) as db, db:
            db.execute('UPDATE sim_inbox_orders SET digest=? WHERE order_id=? AND seq=?',
                       ('0' * 64, 'T1', 1))
        with self.assertRaises(OrderRejected):
            self.send_commit(self.commit)
        with closing(sqlite3.connect(self.inbox.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_activations').fetchone(), (0,))
        self.assertEqual(self.executor.snapshot()['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
