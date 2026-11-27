"""Only a server-guarded virtual generation may change the active path."""
from contextlib import contextmanager, closing
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

import sim.tests.test_reroute_execution as fixture
from server.field_map_planner import plan_field_route
from server.segments import plan_segment
from sim.order_inbox import OrderRejected


class FakeServerGuard:
    """Test-only stand-in: production requires the actual server guard type."""
    def __init__(self, execution, *, allow=True):
        self.robot_receiver = types.SimpleNamespace(robot=types.SimpleNamespace(executor=execution.executor))
        self.allow = allow
        self.outer_lock = None

    @contextmanager
    def guard(self, _payload):
        if not self.allow:
            raise ValueError('server did not authorize this generation')
        if self.outer_lock is None:
            yield
        else:
            with self.outer_lock, self.robot_receiver.robot._lock:
                yield

    @contextmanager
    def guard_next(self, _payload, _order_payload):
        if not self.allow:
            raise ValueError('server did not authorize the next segment')
        yield

    @contextmanager
    def guard_duplicate(self, _payload, _order_payload):
        if not self.allow:
            raise ValueError('server did not authorize the duplicate')
        yield


class RerouteMotionTest(unittest.TestCase):
    def setUp(self):
        self.base = fixture.RerouteExecutionTest(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.base.send_commit(self.base.base.commit)
        self.base.send(self.base.request)
        self.execution = self.base.execution
        self.executor = self.base.base.executor
        self.payload = fixture.fixture.canonical(self.base.request)
        self.guard = FakeServerGuard(self.execution)
        module = types.ModuleType('server.sim_reroute_execute_wire')
        module.SimRerouteExecutionGuard = FakeServerGuard
        self.module_patch = patch.dict(sys.modules, {'server.sim_reroute_execute_wire': module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def test_untrusted_or_denied_guard_does_not_start_motion(self):
        before = self.executor.snapshot()
        for guard in (None, object(), FakeServerGuard(self.execution, allow=False)):
            with self.subTest(guard=guard), self.assertRaises((ValueError, OrderRejected)):
                self.execution.start(self.payload, guard=guard)
            self.assertEqual(self.executor.tick(10), before)
        data = self.base.request['data']
        order = {'order_id': data['task_id'], 'seq': data['parent_seq'] + 1, 'map_id': data['map_id'],
                 'nodes': data['nodes'], 'released_until': data['released_until'], 'action_at_end': data['action_at_end']}
        with self.assertRaises(OrderRejected):
            self.executor._begin_reroute(fixture.fixture.canonical(order), transition_id=data['transition_id'],
                incoming_edge=data['incoming_edge'], edge_ids=data['edge_ids'])
        self.assertEqual(self.executor.snapshot(), before)

    def test_server_guard_starts_exact_new_generation_and_preserves_parent_history(self):
        old = self.executor.snapshot()
        with closing(sqlite3.connect(self.execution.inbox.path)) as db:
            parent = db.execute("SELECT * FROM sim_inbox_orders WHERE order_id='T1' AND seq=1").fetchone()
        started = self.execution.start(self.payload, guard=self.guard)
        self.assertTrue(started['new'])
        self.assertEqual(started['state'], 'moving')
        current = self.executor.snapshot()
        self.assertEqual(current['status'], 'moving')
        self.assertEqual(current['order']['seq'], 2)
        self.assertEqual(current['order']['nodes'], ['W1', 'X', 'Y', 'D1'])
        with closing(sqlite3.connect(self.execution.inbox.path)) as db:
            self.assertEqual(db.execute("SELECT * FROM sim_inbox_orders WHERE order_id='T1' AND seq=1").fetchone(), parent)
        self.assertNotEqual(current['order']['nodes'], old['order']['nodes'])
        self.assertFalse(self.execution.start(self.payload, guard=self.guard)['new'])
        self.assertEqual(self.executor.snapshot()['pose'], current['pose'])
        with self.assertRaises(OrderRejected):
            self.executor.tick(1)
        self.assertEqual(self.executor.snapshot()['pose'], current['pose'])
        with self.assertRaises(ValueError):
            self.execution.tick(self.payload, 1, guard=FakeServerGuard(self.execution, allow=False))
        self.assertEqual(self.executor.snapshot()['pose'], current['pose'])
        finished = self.execution.tick(self.payload, 10, guard=self.guard)
        self.assertEqual(finished['status'], 'stopped')
        self.assertEqual(finished['current_node'], 'D1')
        self.assertEqual(finished['receipt']['state'], 'completed')
        with closing(sqlite3.connect(self.execution.inbox.path)) as db:
            self.assertEqual(db.execute("SELECT * FROM sim_inbox_orders WHERE order_id='T1' AND seq=1").fetchone(), parent)

    def test_multiple_segments_then_new_normal_task_clear_generation_flag(self):
        map_data = fixture.fixture.fixture.cycle_map()
        map_data['zones'].append({'id': 'wait-X', 'type': 'waiting', 'node': 'X'})
        for node in map_data['nodes']:
            if node['id'] == 'D1':
                node.update(kind='patch', uturn=True)
        other = fixture.RerouteExecutionTest(methodName='runTest')
        with patch.object(fixture.fixture.fixture, 'cycle_map', return_value=map_data):
            other.setUp()
        self.addCleanup(other.doCleanups)
        other.base.send_commit(other.base.commit)
        other.send(other.request)
        execution, executor = other.execution, other.base.executor
        payload, guard = fixture.fixture.canonical(other.request), FakeServerGuard(execution)
        self.assertTrue(execution.start(payload, guard=guard)['new'])
        first = execution.tick(payload, 10, guard=guard)
        self.assertEqual((first['status'], first['current_node']), ('stopped', 'X'))
        order = dict(first['order'], seq=3, released_until='D1')
        next_payload = fixture.fixture.canonical(order)
        with self.assertRaises(OrderRejected):
            executor.accept(next_payload)
        with self.assertRaises(ValueError):
            execution.accept_next(payload, next_payload, guard=FakeServerGuard(execution, allow=False))
        self.assertTrue(execution.accept_next(payload, next_payload, guard=guard).new)
        final = execution.tick(payload, 10, guard=guard)
        self.assertEqual((final['status'], final['current_node']), ('stopped', 'D1'))
        incoming = executor.field_stop_direction()[1]
        route = plan_field_route(executor._field_map, map_ref='wait-test@v1', incoming_edge=incoming, goal='X')
        following = {'order_id': 'T2', 'seq': 1, 'map_id': 'wait-test@v1', 'nodes': list(route.nodes),
                     'released_until': plan_segment(executor._field_map, list(route.nodes)).released_until,
                     'action_at_end': {'type': 'none'}}
        self.assertTrue(executor.accept(fixture.fixture.canonical(following)).new)
        self.assertIsNone(executor._reroute_transition)
        self.assertEqual(executor.tick(10)['status'], 'stopped')
        with closing(sqlite3.connect(execution.inbox.path)) as db:
            self.assertEqual(db.execute("SELECT seq,state FROM sim_inbox_orders WHERE order_id='T1' ORDER BY seq")
                             .fetchall(), [(1, 'completed'), (2, 'completed'), (3, 'completed')])

    def test_generation_is_not_allowed_by_normal_order_acceptance(self):
        prior = self.executor.snapshot()
        data = self.base.request['data']
        forged = {'order_id': data['task_id'], 'seq': data['parent_seq'] + 1, 'map_id': data['map_id'],
                  'nodes': data['nodes'], 'released_until': data['released_until'],
                  'action_at_end': data['action_at_end']}
        with self.assertRaises(OrderRejected):
            self.executor.accept(fixture.fixture.canonical(forged))
        self.assertEqual(self.executor.tick(10), prior)

    def test_proof_corruption_stops_before_subsequent_motion(self):
        self.execution.start(self.payload, guard=self.guard)
        prior = self.executor.snapshot()['pose']
        with closing(sqlite3.connect(self.execution.inbox.path)) as db, db:
            db.execute("UPDATE sim_reroute_motion_starts SET order_digest=? WHERE order_id='T1'", ('0' * 64,))
        with self.assertRaises(OrderRejected):
            self.execution.tick(self.payload, 1, guard=self.guard)
        self.assertEqual(self.executor.snapshot()['pose'], prior)

    def test_failure_after_durable_acceptance_never_restarts_motion(self):
        before = self.executor.snapshot()['pose']
        verify = self.executor._verify_field_execution
        def fail_new_generation():
            if self.executor._reroute_transition is not None:
                raise OrderRejected('injected post-commit verification failure')
            return verify()
        with patch.object(self.executor, '_verify_field_execution', side_effect=fail_new_generation):
            with self.assertRaises(OrderRejected):
                self.execution.start(self.payload, guard=self.guard)
        self.assertEqual(self.executor.snapshot()['status'], 'error')
        self.assertEqual(self.executor.snapshot()['pose'], before)
        with closing(sqlite3.connect(self.execution.inbox.path)) as db:
            self.assertEqual(db.execute("SELECT state FROM sim_inbox_orders WHERE order_id='T1' AND seq=2")
                             .fetchone(), ('accepted',))
        self.execution.inbox.initialize()  # a new boot loses in-process ownership
        with self.assertRaises(OrderRejected):
            self.execution.start(self.payload, guard=self.guard)
        with self.assertRaises(OrderRejected):
            self.execution.tick(self.payload, 1, guard=self.guard)
        self.assertEqual(self.executor.snapshot()['pose'], before)

    def test_inbox_reinitialization_revokes_in_process_motion_ownership(self):
        self.execution.start(self.payload, guard=self.guard)
        before = self.executor.snapshot()['pose']
        self.execution.inbox.initialize()
        with self.assertRaises(OrderRejected):
            self.execution.tick(self.payload, 1, guard=self.guard)
        self.assertEqual(self.executor.snapshot()['pose'], before)


if __name__ == '__main__':
    unittest.main()
