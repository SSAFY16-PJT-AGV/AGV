"""A reserved reroute is executed by the real isolated MQTT simulator."""
import time
import unittest
import sqlite3
import threading
import json
from contextlib import closing
from unittest.mock import patch

from server.sim_reroute_finish import finish_reroute
from server.sim_reroute_activation import stage_activation
from server.sim_reroute_activate_wire import publish_activation, SimRerouteActivationGuard
from server.sim_reroute_activate_receipt import ActivationReceiptInbox, ActivationReceiptReceiver
from server.sim_reroute_execute_wire import (prepare_execution, publish_execution, SimRerouteExecutionGuard)
from server.sim_reroute_receipt import SimRerouteReceiptInbox, SimRerouteReceiptReceiver
from server.sim_reroute_wire import SimRerouteProposalGuard, publish_proposal
from server.tests.test_sim_reroute_reservation import RerouteReservationTest
from sim.reroute_acceptance import SimRerouteAcceptance
from sim.reroute_activation import SimRerouteActivation
from sim.reroute_activation_mqtt import SimRerouteActivationMqttReceiver
from sim.reroute_execution import SimRerouteExecution
from sim.reroute_execute_mqtt import SimRerouteExecutionMqttReceiver
from sim.reroute_mqtt import SimRerouteMqttReceiver
from server.segments import plan_segment
from server.allocation_store import _verify_field_global, AllocationConflict


class RerouteFinishWireTest(unittest.TestCase):
    def test_new_clearance_releases_union_and_unblocks_second_robot(self):
        def after_prepare(runtime, robots):
            key, robot = bytes(range(32)), robots['sim-r1']
            inbox = SimRerouteReceiptInbox(runtime, {'sim-r1': key}); inbox.initialize()
            observer = SimRerouteReceiptReceiver(inbox)
            acceptor = SimRerouteAcceptance(robot.executor, robot_session='boot-1')
            proposal = SimRerouteMqttReceiver(robot, SimRerouteProposalGuard(runtime, robot_id='sim-r1'),
                                              acceptor, receipt_key=key)
            activation_inbox = ActivationReceiptInbox(runtime, inbox, proposal); activation_inbox.initialize()
            activation_observer = ActivationReceiptReceiver(activation_inbox)
            activation = SimRerouteActivation(acceptor)
            activation_guard = SimRerouteActivationGuard(runtime, receipt_inbox=inbox, robot_receiver=proposal)
            activation_receiver = SimRerouteActivationMqttReceiver(proposal, activation_guard, activation, receipt_key=key)
            execution = SimRerouteExecution(activation)
            guard = SimRerouteExecutionGuard(runtime, receipt_inbox=inbox, robot_receiver=proposal,
                                              activation_inbox=activation_inbox, activation_receiver=activation_receiver)
            execution_receiver = SimRerouteExecutionMqttReceiver(guard, execution, receipt_key=key)
            try:
                observer.start(); proposal.start(); activation_observer.start(); activation_receiver.start()
                execution_receiver.start()
                old_and_new = runtime.store.list_occupancy()
                publish_proposal(runtime, 'T1')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and inbox.get('T1') is None: time.sleep(.02)
                self.assertIsNotNone(inbox.get('T1'))
                stage_activation(runtime, 'T1', inbox, proposal)
                publish_activation(runtime, 'T1', inbox, proposal)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and activation_inbox.get('T1') is None: time.sleep(.02)
                self.assertIsNotNone(activation_inbox.get('T1'))
                result = prepare_execution(runtime, 'T1', guard, execution_receiver)
                self.assertEqual(result['state'], 'ready_not_published')
                self.assertEqual(publish_execution(runtime, 'T1', guard, execution_receiver)['state'], 'uncertain')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and robot.executor.snapshot()['status'] != 'moving': time.sleep(.02)
                self.assertEqual(robot.executor.snapshot()['status'], 'moving')
                self.assertEqual(runtime.store.list_occupancy(), old_and_new)
                state = robot.tick(10)
                self.assertEqual((state['status'], state['current_node']), ('stopped', 'D1'))
                self.assertEqual(runtime.store.list_occupancy(), old_and_new)
                held = {resource for resource, owner in old_and_new.items() if owner == 'sim-r1'}
                event = {'type': 'rear_clear', 'event_id': 'reroute-T1', 'robot_session': 'boot-1',
                         'order_id': 'T1', 'seq': 2, 'map_id': 'wait-test@v1', 'stopped_at': 'D1',
                         'cleared_resources': [list(item) for item in sorted(held - {('node', 'D1')})]}
                robot.publish_event(event)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and ('sim-r1', 'reroute-T1') not in runtime.receiver._occupied_events: time.sleep(.02)
                self.assertEqual(runtime.inbox.get('sim-r1', 'reroute-T1')['state'], 'pending')
                from server.sim_reroute_finish import finished_resources
                def fail_after_finish_insert(db, *args):
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sim_reroute_finishes'").fetchone()
                    if exists and db.execute('SELECT 1 FROM sim_reroute_finishes WHERE task_id=?', ('T1',)).fetchone():
                        raise RuntimeError('injected after clearance changes')
                    return finished_resources(db, *args)
                with patch('server.sim_reroute_finish.finished_resources', side_effect=fail_after_finish_insert):
                    with self.assertRaises(RuntimeError):
                        finish_reroute(runtime, 'T1', 'reroute-T1', guard)
                self.assertEqual(runtime.store.list_occupancy(), old_and_new)
                self.assertEqual(runtime.inbox.get('sim-r1', 'event-T1')['state'], 'pending')
                self.assertEqual(runtime.inbox.get('sim-r1', 'reroute-T1')['state'], 'pending')
                self.assertEqual(finish_reroute(runtime, 'T1', 'reroute-T1', guard)['state'], 'reached_goal_held')
                self.assertEqual(runtime.inbox.get('sim-r1', 'event-T1')['state'], 'rejected')
                self.assertEqual(runtime.inbox.get('sim-r1', 'event-T1')['result']['status'], 'superseded_by_reroute')
                self.assertNotIn(('sim-r1', 'event-T1'), runtime.receiver._occupied_events)
                self.assertNotIn(('sim-r1', 'reroute-T1'), runtime.receiver._occupied_events)
                duplicate_seen, duplicate_result = threading.Event(), []
                original_store = runtime.inbox.store
                def observe_inbox(robot_id, incoming):
                    result = original_store(robot_id, incoming)
                    if robot_id == 'sim-r1' and result.get('event_id') == 'reroute-T1':
                        duplicate_result.append(result)
                        duplicate_seen.set()
                    return result
                with patch.object(runtime.inbox, 'store', observe_inbox):
                    duplicate = json.dumps(event, separators=(',', ':')).encode('utf-8')
                    info = robot._client.publish(robot.event_topic, duplicate, qos=1, retain=False)
                    info.wait_for_publish(timeout=5)
                    self.assertTrue(info.is_published())
                    self.assertTrue(duplicate_seen.wait(5))
                self.assertEqual(duplicate_result[0]['status'], 'duplicate')
                self.assertEqual(duplicate_result[0]['state'], 'applied')
                self.assertEqual(runtime.store.list_occupancy()[('node', 'D1')], 'sim-r1')
                with closing(sqlite3.connect(runtime.path, isolation_level=None)) as db:
                    corruptions = (
                        "UPDATE sim_event_inbox SET result='{}' WHERE robot_id='sim-r1' AND event_id='reroute-T1'",
                        "UPDATE sim_event_inbox SET result='{}' WHERE robot_id='sim-r1' AND event_id='event-T1'",
                        "UPDATE sim_reroute_activation_receipts SET digest='" + '0' * 64 + "' WHERE task_id='T1'",
                        "DELETE FROM allocation_occupancy WHERE task_id='T1' AND kind='node' AND resource_id='D1'",
                        "INSERT INTO allocation_occupancy(kind,resource_id,task_id) VALUES('zone','ghost','T1')",
                    )
                    for sql in corruptions:
                        db.execute('BEGIN IMMEDIATE')
                        db.execute(sql)
                        with self.assertRaises(AllocationConflict, msg=sql):
                            _verify_field_global(db, field_required=True)
                        db.execute('ROLLBACK')
                self.assertEqual(runtime.store.list_occupancy()[('node', 'D1')], 'sim-r1')
                self.assertEqual(runtime.receiver.retry_occupied_event('sim-r2', 'event-T2')['status'], 'advanced')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and robots['sim-r2']._queue.empty(): time.sleep(.02)
                for index in range(4):
                    state2 = robots['sim-r2'].tick(10)
                    self.assertEqual(state2['status'], 'stopped')
                    order, receipt = state2['order'], state2['receipt']
                    challenge = runtime.begin('T2', 'boot-2')['challenge']
                    runtime.complete('T2', 'boot-2', challenge, 'sim-r2', 'T2', receipt['seq'],
                                     'wait-test@v1', state2['current_node'], receipt['digest'], True)
                    held2 = {item for item, owner in runtime.store.list_occupancy().items() if owner == 'sim-r2'}
                    stop = state2['current_node']
                    offset = order['nodes'].index(stop)
                    keep2 = ({('node', stop)} if stop == 'D2' else
                             set(plan_segment(runtime.progress._map, order['nodes'][offset:]).resources))
                    event2 = {'type': 'rear_clear', 'event_id': 'next-T2-' + str(index),
                              'robot_session': 'boot-2', 'order_id': 'T2', 'seq': receipt['seq'],
                              'map_id': 'wait-test@v1', 'stopped_at': stop,
                              'cleared_resources': [list(item) for item in sorted(held2 - keep2)]}
                    robots['sim-r2'].publish_event(event2)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        saved = runtime.inbox.get('sim-r2', event2['event_id'])
                        if saved and saved['state'] == 'applied': break
                        time.sleep(.02)
                    self.assertEqual(runtime.inbox.get('sim-r2', event2['event_id'])['state'], 'applied')
                    if stop == 'D2':
                        self.assertEqual(saved['result']['status'], 'finished')
                        break
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and robots['sim-r2']._queue.empty(): time.sleep(.02)
                    self.assertFalse(robots['sim-r2']._queue.empty())
                else:
                    self.fail('second robot did not reach its destination')
                with self.assertRaises((ValueError, RuntimeError)):
                    publish_execution(runtime, 'T1', guard, execution_receiver)
            finally:
                execution_receiver.stop(); activation_receiver.stop(); activation_observer.stop()
                proposal.stop(); observer.stop()
        for handoff_store in (False, True):
            with self.subTest(handoff_store=handoff_store):
                RerouteReservationTest(methodName='runTest')._exercise(handoff_store, after_prepare=after_prepare)


if __name__ == '__main__':
    unittest.main()
