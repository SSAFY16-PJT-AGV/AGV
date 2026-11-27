"""Real MQTT intermediate stop, exact one-shot next generation command."""
import time
import unittest
import sqlite3
from contextlib import closing
from unittest.mock import patch
import paho.mqtt.client as mqtt

from server.sim_reroute_activation import stage_activation
from server.sim_reroute_activate_wire import publish_activation
from server.sim_reroute_execute_wire import prepare_execution, publish_execution
from server.sim_reroute_multi_progress import stage_next, publish_next
from server.sim_reroute_finish import finish_reroute
from server.segments import plan_segment
from server.allocation_store import AllocationConflict, _verify_field_global
from server.sim_reroute_automatic import SimRerouteAutoSession
from server.sim_reroute_wire import publish_proposal
from server.tests.test_sim_reroute_multi_reservation import RerouteMultiReservationTest


class RerouteMultiWireTest(unittest.TestCase):
    def test_fresh_intermediate_event_authorizes_exact_next_order_once(self):
        self._exercise('success')

    def test_unknown_next_publish_is_not_retried_and_reservations_survive_restart(self):
        self._exercise('lost_publish')

    def test_intermediate_event_cannot_claim_reservation_release(self):
        self._exercise('bad_clearance')

    def _exercise(self, mode):
        def exercise(runtime, robots):
            robot = robots['sim-r1']; original = runtime.store.list_occupancy()
            session = SimRerouteAutoSession(runtime, robot, 'boot-1', key=bytes(range(32)), simulation_enabled=True).start()
            def wait(predicate):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if predicate(): return
                    time.sleep(.02)
                self.fail('timed out waiting for isolated multi-segment MQTT')
            try:
                publish_proposal(runtime, 'T1'); wait(lambda: session.receipts.get('T1') is not None)
                stage_activation(runtime, 'T1', session.receipts, session.proposal)
                publish_activation(runtime, 'T1', session.receipts, session.proposal)
                wait(lambda: session.activation_receipts.get('T1') is not None)
                prepare_execution(runtime, 'T1', session.guard, session.execution_receiver)
                publish_execution(runtime, 'T1', session.guard, session.execution_receiver)
                wait(lambda: robot.executor.snapshot()['status'] == 'moving')
                state = robot.tick(20)
                self.assertEqual((state['status'], state['current_node'], state['receipt']['seq']), ('stopped', 'N01', 2))
                event = {'type': 'rear_clear', 'event_id': 'multi-stop-2', 'robot_session': 'boot-1',
                         'order_id': 'T1', 'seq': 2, 'map_id': 'mat_ssafy_v3@v0',
                         'stopped_at': 'N01', 'cleared_resources': []}
                if mode == 'bad_clearance':
                    event['cleared_resources'] = [['node', 'N07']]
                robot.publish_event(event)
                wait(lambda: ('sim-r1', event['event_id']) in runtime.receiver._occupied_events)
                if mode == 'bad_clearance':
                    with self.assertRaises(ValueError): stage_next(runtime, 'T1', event['event_id'], session.guard)
                    self.assertEqual(runtime.store.list_occupancy(), original)
                    self.assertEqual(runtime.inbox.get('sim-r1', event['event_id'])['state'], 'pending')
                    return
                self.assertEqual(stage_next(runtime, 'T1', event['event_id'], session.guard)['next_seq'], 3)
                self.assertEqual(runtime.store.list_occupancy(), original)
                if mode == 'lost_publish':
                    with patch.object(mqtt.Client, 'publish', side_effect=RuntimeError('injected unknown publish')):
                        with self.assertRaises(RuntimeError): publish_next(runtime, 'T1', session.guard)
                    with self.assertRaises(ValueError): publish_next(runtime, 'T1', session.guard)
                    self.assertEqual(runtime.store.list_occupancy(), original)
                    return
                self.assertEqual(publish_next(runtime, 'T1', session.guard)['state'], 'uncertain')
                wait(lambda: not robot._queue.empty())
                state = robot.tick(20)
                self.assertEqual((state['status'], state['current_node'], state['receipt']['seq']), ('stopped', 'J07', 3))
                self.assertEqual(runtime.store.list_occupancy(), original)
                with self.assertRaises((ValueError, RuntimeError)):
                    publish_next(runtime, 'T1', session.guard)
                event2 = dict(event, event_id='multi-stop-3', seq=3, stopped_at='J07')
                robot.publish_event(event2)
                wait(lambda: ('sim-r1', event2['event_id']) in runtime.receiver._occupied_events)
                self.assertEqual(stage_next(runtime, 'T1', event2['event_id'], session.guard)['next_seq'], 4)
                self.assertEqual(runtime.store.list_occupancy(), original)
                publish_next(runtime, 'T1', session.guard)
                wait(lambda: not robot._queue.empty())
                state = robot.tick(20)
                self.assertEqual((state['status'], state['current_node'], state['receipt']['seq']), ('stopped', 'N03', 4))
                held = {item for item, owner in original.items() if owner == 'sim-r1'}
                final = dict(event, event_id='multi-finish-4', seq=4, stopped_at='N03',
                             cleared_resources=[list(item) for item in sorted(held - {('node', 'N03')})])
                robot.publish_event(final)
                wait(lambda: ('sim-r1', final['event_id']) in runtime.receiver._occupied_events)
                self.assertEqual(finish_reroute(runtime, 'T1', final['event_id'], session.guard)['state'], 'reached_goal_held')
                with closing(sqlite3.connect(runtime.path, isolation_level=None)) as db:
                    corruptions = (
                        "UPDATE sim_reroute_multi_steps SET state='staged' WHERE task_id='T1' AND seq=3",
                        "DELETE FROM sim_reroute_multi_steps WHERE task_id='T1' AND seq=2",
                        "DELETE FROM sim_reroute_multi_steps WHERE task_id='T1' AND seq=3",
                        "UPDATE sim_reroute_multi_steps SET next_digest='wrong' WHERE task_id='T1' AND seq=2",
                        "UPDATE sim_event_inbox SET result='{}' WHERE robot_id='sim-r1' AND event_id='multi-stop-3'",
                    )
                    for sql in corruptions:
                        db.execute('BEGIN IMMEDIATE'); db.execute(sql)
                        with self.assertRaises(AllocationConflict, msg=sql):
                            _verify_field_global(db, field_required=True)
                        db.execute('ROLLBACK')
                self.assertEqual(runtime.receiver.retry_occupied_event('sim-r2', 'event-T2')['status'], 'advanced')
                wait(lambda: not robots['sim-r2']._queue.empty())
                for index in range(10):
                    second = robots['sim-r2'].tick(20)
                    order, receipt = second['order'], second['receipt']; stop = second['current_node']
                    self.assertEqual(second['status'], 'stopped')
                    challenge = runtime.begin('T2', 'boot-2')['challenge']
                    runtime.complete('T2', 'boot-2', challenge, 'sim-r2', 'T2', receipt['seq'],
                                     'mat_ssafy_v3@v0', stop, receipt['digest'], True)
                    occupied = {item for item, owner in runtime.store.list_occupancy().items() if owner == 'sim-r2'}
                    offset = order['nodes'].index(stop)
                    keep = ({('node', stop)} if stop == 'N05' else
                            set(plan_segment(runtime.progress._map, order['nodes'][offset:]).resources))
                    fresh = {'type': 'rear_clear', 'event_id': 'multi-T2-' + str(index),
                             'robot_session': 'boot-2', 'order_id': 'T2', 'seq': receipt['seq'],
                             'map_id': 'mat_ssafy_v3@v0', 'stopped_at': stop,
                             'cleared_resources': [list(item) for item in sorted(occupied - keep)]}
                    robots['sim-r2'].publish_event(fresh)
                    wait(lambda: runtime.inbox.get('sim-r2', fresh['event_id']) is not None and
                         runtime.inbox.get('sim-r2', fresh['event_id'])['state'] == 'applied')
                    if stop == 'N05':
                        self.assertEqual(runtime.inbox.get('sim-r2', fresh['event_id'])['result']['status'], 'finished')
                        break
                    wait(lambda: not robots['sim-r2']._queue.empty())
                else:
                    self.fail('second virtual AGV did not reach its destination')
            finally:
                session.stop()
        RerouteMultiReservationTest(methodName='runTest')._exercise(after_prepare=exercise)


if __name__ == '__main__':
    unittest.main()
