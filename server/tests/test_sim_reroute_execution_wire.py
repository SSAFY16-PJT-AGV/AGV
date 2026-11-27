"""A reserved reroute is executed by the real isolated MQTT simulator."""
import time
import unittest
import sqlite3
import threading
from contextlib import closing, contextmanager
from unittest.mock import patch

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


class RerouteExecutionWireTest(unittest.TestCase):
    def test_real_mqtt_robot_executes_single_reserved_detour(self):
        self._exercise(mode='duplicate')

    def test_conflicting_duplicate_stops_motion_and_preserves_both_holds(self):
        self._exercise(mode='tampered')

    def test_robot_disconnect_stops_motion_without_releasing_holds(self):
        self._exercise(mode='disconnect')

    def _exercise(self, *, mode):
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
                # QoS1 can redeliver an already installed request. Exercise
                # the real broker and wait for the worker, not just PUBACK.
                with closing(sqlite3.connect(runtime.path)) as db:
                    payload = db.execute('SELECT payload FROM sim_reroute_execution_attempts WHERE task_id=?', ('T1',)).fetchone()[0]
                if mode == 'disconnect':
                    before = robot.executor.snapshot()
                    robot._client.disconnect()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and robot.ready: time.sleep(.02)
                    self.assertFalse(robot.ready)
                    with self.assertRaises(RuntimeError):
                        robot.tick(1)
                    self.assertEqual(robot.executor.snapshot(), before)
                    self.assertEqual(runtime.store.list_occupancy(), old_and_new)
                    with self.assertRaises((ValueError, RuntimeError)):
                        publish_execution(runtime, 'T1', guard, execution_receiver)
                    return
                if mode == 'tampered':
                    altered = payload.replace(b'"generation":1', b'"generation":true')
                    self.assertNotEqual(altered, payload)
                    before = robot.executor.snapshot()
                    info = execution_receiver.endpoint._client.publish(execution_receiver._topic, altered, qos=1)
                    info.wait_for_publish(timeout=5)
                    self.assertTrue(info.is_published())
                    self.assertTrue(execution_receiver.endpoint._failed.wait(5))
                    with self.assertRaises(RuntimeError):
                        robot.tick(1)
                    self.assertEqual(robot.executor.snapshot(), before)
                    self.assertEqual(runtime.store.list_occupancy(), old_and_new)
                    return
                processed, entered, release = threading.Event(), threading.Event(), threading.Event()
                processor = execution_receiver.endpoint.processor
                def observe_duplicate(topic, message):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('test did not release duplicate')
                    response = processor(topic, message)
                    processed.set()
                    return response
                execution_receiver.endpoint.processor = observe_duplicate
                info = execution_receiver.endpoint._client.publish(execution_receiver._topic, payload, qos=1)
                info.wait_for_publish(timeout=5)
                self.assertTrue(info.is_published())
                self.assertTrue(entered.wait(5), 'duplicate did not reach the worker')
                tick_result, tick_errors, tick_entered = [], [], threading.Event()
                operation = runtime._runtime_read_operation
                @contextmanager
                def observe_runtime_entry():
                    if threading.current_thread().name == 'concurrent-reroute-tick':
                        tick_entered.set()
                    with operation() as result:
                        yield result
                def concurrent_tick():
                    try:
                        tick_result.append(robot.tick(.01))
                    except Exception as exc:
                        tick_errors.append(exc)
                tick_thread = threading.Thread(target=concurrent_tick, name='concurrent-reroute-tick')
                with patch.object(runtime, '_runtime_read_operation', observe_runtime_entry):
                    tick_thread.start()
                    try:
                        self.assertTrue(tick_entered.wait(5), 'tick did not enter its server guard')
                    finally:
                        release.set()
                    self.assertTrue(processed.wait(5), 'duplicate execute was not processed')
                    tick_thread.join(5)
                self.assertFalse(tick_thread.is_alive(), 'duplicate and tick deadlocked')
                self.assertEqual(tick_errors, [])
                self.assertEqual(tick_result[0]['status'], 'moving')
                self.assertFalse(execution_receiver.endpoint._failed.is_set())
                self.assertTrue(robot.ready)
                state = robot.tick(10)
                self.assertEqual((state['status'], state['current_node']), ('stopped', 'D1'))
                self.assertEqual(runtime.store.list_occupancy(), old_and_new)
                with self.assertRaises((ValueError, RuntimeError)):
                    publish_execution(runtime, 'T1', guard, execution_receiver)
            finally:
                execution_receiver.stop(); activation_receiver.stop(); activation_observer.stop()
                proposal.stop(); observer.stop()
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)


if __name__ == '__main__':
    unittest.main()
