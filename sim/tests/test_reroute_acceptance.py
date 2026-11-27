"""Virtual robot records a reroute proposal without executing it."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from server.field_map_planner import plan_field_route
from server.segments import plan_segment
from server.tests.test_sim_reroute_mqtt import cycle_map
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import OrderRejected, SimOrderInbox
from sim.reroute_acceptance import SimRerouteAcceptance


class RerouteAcceptanceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='robot-reroute-receipt-')
        self.addCleanup(tmp.cleanup)
        self.data = cycle_map()
        self.data['schema'] = 'agv-map-v1'
        self.data['nodes'].append({'id': 'P1', 'pos': [-100, 0]})
        self.data['edges'].append({'id': 'IN1', 'from': 'P1', 'to': 'S1',
            'shape': [[-100, 0], [0, 0]], 'bidirectional': True, 'lanes': 1})
        self.inbox = SimOrderInbox(Path(tmp.name) / 'robot.sqlite3', 'sim-r1', 'wait-test@v1')
        self.inbox.initialize()
        self.executor = SimOrderExecutor(self.inbox, self.data, 'S1', experimental_field_map=True,
                                         field_incoming_edge='IN1')
        self.original = {'order_id': 'T1', 'seq': 1, 'map_id': 'wait-test@v1',
                         'nodes': ['S1', 'W1', 'W2', 'D1'], 'released_until': 'W1',
                         'action_at_end': {'type': 'none'}}
        self.executor.accept(json.dumps(self.original).encode('utf-8'))
        state = self.executor.tick(10)
        self.assertEqual(state['status'], 'stopped')
        route = plan_field_route(self.data, map_ref='wait-test@v1', incoming_edge='E0', goal='D1',
                                 blocked_resources={('node', 'W2')})
        self.message = {'type': 'sim.reroute.prepare.v1', 'data': {
            'transition_id': '1' * 32, 'generation': 1, 'robot_id': 'sim-r1', 'robot_session': 'boot-1',
            'task_id': 'T1', 'parent_seq': 1, 'parent_digest': state['receipt']['digest'],
            'map_id': 'wait-test@v1', 'stopped_at': 'W1', 'incoming_edge': 'E0',
            'nodes': list(route.nodes), 'edge_ids': list(route.edge_ids),
            'blocked_resources': [['node', 'W2']],
            'released_until': plan_segment(self.data, list(route.nodes)).released_until,
            'action_at_end': {'type': 'none'}}}
        self.receiver = SimRerouteAcceptance(self.executor, robot_session='boot-1')
        self.receiver.initialize()

    def send(self, message):
        return self.receiver.accept(json.dumps(message).encode('utf-8'))

    def test_persists_exact_receipt_and_duplicate_does_not_move(self):
        before = self.executor.snapshot()
        receipt = self.send(self.message)
        self.assertTrue(receipt['new'])
        self.assertEqual(receipt['transition_id'], '1' * 32)
        self.assertEqual(receipt['state'], 'accepted_not_executed')
        self.assertEqual(self.executor.snapshot(), before)
        duplicate = self.send(self.message)
        self.assertFalse(duplicate['new'])
        self.assertEqual(self.executor.tick(1), before)
        # Reopening the durable inbox does not make acceptance an execution.
        self.receiver.initialize()
        self.assertFalse(self.send(self.message)['new'])
        self.assertEqual(self.executor.snapshot(), before)

    def test_stale_parent_direction_session_and_modified_plan_are_rejected(self):
        for key, value in [('parent_seq', 2), ('parent_digest', '0' * 64), ('stopped_at', 'W2'),
                           ('incoming_edge', 'E0~r'), ('robot_session', 'other'), ('generation', 2),
                           ('action_at_end', {'type': 'unload'}), ('nodes', ['W1', 'W2', 'D1'])]:
            altered = copy.deepcopy(self.message)
            altered['data'][key] = value
            with self.subTest(key=key), self.assertRaises((ValueError, OrderRejected)):
                self.send(altered)
        self.assertTrue(self.send(self.message)['new'])
        conflict = copy.deepcopy(self.message)
        conflict['data']['transition_id'] = '2' * 32
        with self.assertRaises((ValueError, OrderRejected)):
            self.send(conflict)
        self.assertEqual(self.executor.snapshot()['status'], 'stopped')

    def test_corrupted_durable_receipt_and_duplicate_json_keys_are_rejected(self):
        self.assertTrue(self.send(self.message)['new'])
        with closing(sqlite3.connect(self.inbox.path)) as db, db:
            db.execute("UPDATE sim_reroute_receipts SET digest=?", ('0' * 64,))
        with self.assertRaises(OrderRejected):
            self.send(self.message)
        with self.assertRaises(OrderRejected):
            self.receiver.initialize()
        with self.assertRaises(OrderRejected):
            self.receiver.accept(b'{"type":"x","type":"sim.reroute.prepare.v1","data":{}}')
        self.assertEqual(self.executor.snapshot()['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
