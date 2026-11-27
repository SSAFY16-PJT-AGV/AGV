"""Isolated activation messages cannot move a simulated robot by themselves."""
import json
import queue
import threading
import time
import unittest
from unittest.mock import patch

import paho.mqtt.client as mqtt

from server.sim_reroute_activation import stage_activation
from server.sim_reroute_activate_wire import publish_activation, SimRerouteActivationGuard
from server.sim_reroute_activate_receipt import ActivationReceiptInbox, ActivationReceiptReceiver
from server.sim_reroute_receipt import SimRerouteReceiptInbox, SimRerouteReceiptReceiver
from server.sim_reroute_wire import SimRerouteProposalGuard, publish_proposal
from server.tests.test_sim_reroute_reservation import RerouteReservationTest
from sim.reroute_acceptance import SimRerouteAcceptance
from sim.reroute_activation import SimRerouteActivation
from sim.reroute_activation_mqtt import SimRerouteActivationMqttReceiver
from sim.reroute_mqtt import SimRerouteMqttReceiver


class RerouteActivationWireTest(unittest.TestCase):
    def test_activation_is_sent_once_and_durably_staged_at_robot(self):
        def after_prepare(runtime, robots):
            key = bytes(range(32))
            inbox = SimRerouteReceiptInbox(runtime, {'sim-r1': key})
            inbox.initialize()
            observer = SimRerouteReceiptReceiver(inbox)
            acceptor = SimRerouteAcceptance(robots['sim-r1'].executor, robot_session='boot-1')
            robot = SimRerouteMqttReceiver(robots['sim-r1'], SimRerouteProposalGuard(runtime, robot_id='sim-r1'),
                                           acceptor, receipt_key=key)
            ready, received = threading.Event(), queue.Queue()
            peer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311,
                               reconnect_on_failure=False)
            peer.on_connect = lambda c, _u, _f, r, _p: c.subscribe('agv/sim-r1/reroute/activate', qos=1) if not r.is_failure else None
            peer.on_subscribe = lambda _c, _u, _m, rs, _p: ready.set() if len(rs) == 1 and not rs[0].is_failure else None
            peer.on_message = lambda _c, _u, msg: received.put(msg.payload)
            peer.connect('127.0.0.1', runtime.port)
            peer.loop_start()
            try:
                self.assertTrue(ready.wait(5))
                observer.start()
                robot.start()
                held = runtime.store.list_occupancy()
                publish_proposal(runtime, 'T1')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and inbox.get('T1') is None:
                    time.sleep(.02)
                self.assertIsNotNone(inbox.get('T1'))
                stage_activation(runtime, 'T1', inbox, robot)
                result = publish_activation(runtime, 'T1', inbox, robot)
                self.assertEqual(result['state'], 'uncertain')
                payload = received.get(timeout=5)
                guard = SimRerouteActivationGuard(runtime, receipt_inbox=inbox, robot_receiver=robot)
                activation = SimRerouteActivation(acceptor)
                activation.initialize()
                with guard.guard(payload):
                    recorded = activation.record(payload)
                self.assertEqual(recorded['state'], 'recorded_not_executed')
                with guard.guard(payload):
                    self.assertFalse(activation.record(payload)['new'])
                forged = json.loads(payload)
                forged['data']['nodes'] = ['W1', 'W2', 'D1']
                with self.assertRaises(ValueError):
                    with guard.guard(json.dumps(forged).encode()):
                        pass
                self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
                self.assertEqual(runtime.store.list_occupancy(), held)
                with self.assertRaises((ValueError, RuntimeError)):
                    publish_activation(runtime, 'T1', inbox, robot)
                with self.assertRaises(queue.Empty):
                    received.get(timeout=.2)
            finally:
                robot.stop()
                observer.stop()
                peer.disconnect()
                peer.loop_stop()
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)

    def test_activation_publish_failure_is_uncertain_and_never_retried(self):
        def after_prepare(runtime, robots):
            key = bytes(range(32))
            inbox = SimRerouteReceiptInbox(runtime, {'sim-r1': key})
            inbox.initialize()
            observer = SimRerouteReceiptReceiver(inbox)
            acceptor = SimRerouteAcceptance(robots['sim-r1'].executor, robot_session='boot-1')
            robot = SimRerouteMqttReceiver(robots['sim-r1'], SimRerouteProposalGuard(runtime, robot_id='sim-r1'),
                                           acceptor, receipt_key=key)
            try:
                observer.start()
                robot.start()
                held = runtime.store.list_occupancy()
                publish_proposal(runtime, 'T1')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and inbox.get('T1') is None:
                    time.sleep(.02)
                self.assertIsNotNone(inbox.get('T1'))
                stage_activation(runtime, 'T1', inbox, robot)
                with patch.object(mqtt.Client, 'publish', side_effect=RuntimeError('connection lost')):
                    with self.assertRaises(RuntimeError):
                        publish_activation(runtime, 'T1', inbox, robot)
                with self.assertRaises((ValueError, RuntimeError)):
                    publish_activation(runtime, 'T1', inbox, robot)
                self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
                self.assertEqual(runtime.store.list_occupancy(), held)
            finally:
                robot.stop()
                observer.stop()
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)

    def test_robot_and_server_exchange_signed_activation_receipt_without_motion(self):
        def after_prepare(runtime, robots):
            key = bytes(range(32))
            inbox = SimRerouteReceiptInbox(runtime, {'sim-r1': key})
            inbox.initialize()
            observer = SimRerouteReceiptReceiver(inbox)
            acceptor = SimRerouteAcceptance(robots['sim-r1'].executor, robot_session='boot-1')
            robot = SimRerouteMqttReceiver(robots['sim-r1'], SimRerouteProposalGuard(runtime, robot_id='sim-r1'),
                                           acceptor, receipt_key=key)
            activation_inbox = ActivationReceiptInbox(runtime, inbox, robot)
            activation_inbox.initialize()
            activation_observer = ActivationReceiptReceiver(activation_inbox)
            activation_guard = SimRerouteActivationGuard(runtime, receipt_inbox=inbox, robot_receiver=robot)
            activation_robot = SimRerouteActivationMqttReceiver(robot, activation_guard,
                                                                   SimRerouteActivation(acceptor), receipt_key=key)
            try:
                observer.start()
                robot.start()
                activation_observer.start()
                activation_robot.start()
                held = runtime.store.list_occupancy()
                publish_proposal(runtime, 'T1')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and inbox.get('T1') is None:
                    time.sleep(.02)
                self.assertIsNotNone(inbox.get('T1'))
                stage_activation(runtime, 'T1', inbox, robot)
                self.assertEqual(publish_activation(runtime, 'T1', inbox, robot)['state'], 'uncertain')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and activation_inbox.get('T1') is None:
                    time.sleep(.02)
                result = activation_inbox.get('T1')
                self.assertIsNotNone(result)
                self.assertEqual(result['state'], 'recorded_not_executed')
                self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
                self.assertEqual(runtime.store.list_occupancy(), held)
                self.assertFalse(activation_inbox.record(result['payload'])['new'])
                corrupted = bytearray(result['payload'])
                corrupted[-3] ^= 1
                with self.assertRaises(ValueError):
                    activation_inbox.record(bytes(corrupted))
                self.assertEqual(runtime.store.list_occupancy(), held)
            finally:
                activation_robot.stop()
                activation_observer.stop()
                robot.stop()
                observer.stop()
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)


if __name__ == '__main__':
    unittest.main()
