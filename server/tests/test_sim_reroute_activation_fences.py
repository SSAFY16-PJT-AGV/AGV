"""Stop/disconnect callbacks cannot overtake an entered activation effect."""
import threading
import time
import unittest
import queue
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


class ActivationFenceTest(unittest.TestCase):
    def _scenario(self, callback):
        def after_prepare(runtime, robots):
            key = bytes(range(32))
            inbox = SimRerouteReceiptInbox(runtime, {'sim-r1': key})
            inbox.initialize()
            observer = SimRerouteReceiptReceiver(inbox)
            acceptor = SimRerouteAcceptance(robots['sim-r1'].executor, robot_session='boot-1')
            proposal = SimRerouteMqttReceiver(robots['sim-r1'], SimRerouteProposalGuard(runtime, robot_id='sim-r1'),
                                              acceptor, receipt_key=key)
            try:
                observer.start()
                proposal.start()
                held = runtime.store.list_occupancy()
                publish_proposal(runtime, 'T1')
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and inbox.get('T1') is None:
                    time.sleep(.02)
                self.assertIsNotNone(inbox.get('T1'))
                callback(runtime, robots['sim-r1'], proposal, inbox, acceptor, held)
                self.assertEqual(runtime.store.list_occupancy(), held)
                self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
            finally:
                proposal.stop()
                observer.stop()
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)

    def test_proposal_stop_waits_for_entered_stage(self):
        def scenario(runtime, robot, proposal, inbox, _acceptor, held):
            entered, release = threading.Event(), threading.Event()
            from server.sim_reroute_activation import _message
            def wait_message(*args, **kwargs):
                value = _message(*args, **kwargs)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('barrier timed out')
                return value
            errors = []
            def stage():
                try:
                    stage_activation(runtime, 'T1', inbox, proposal)
                except Exception as exc:
                    errors.append(exc)
            with patch('server.sim_reroute_activation._message', side_effect=wait_message):
                worker, stop = threading.Thread(target=stage), threading.Thread(target=proposal.stop)
                try:
                    worker.start()
                    self.assertTrue(entered.wait(5))
                    stop.start()
                    stop.join(.1)
                    self.assertTrue(stop.is_alive(), 'proposal stop must wait for entered staging')
                finally:
                    release.set()
                    worker.join(8)
                    stop.join(8)
                self.assertFalse(worker.is_alive())
                self.assertFalse(stop.is_alive())
                self.assertEqual(errors, [])
            with self.assertRaises(ValueError):
                stage_activation(runtime, 'T1', inbox, proposal)
            self.assertEqual(runtime.store.list_occupancy(), held)
        self._scenario(scenario)

    def test_robot_disconnect_callback_waits_for_entered_stage(self):
        def scenario(runtime, robot, proposal, inbox, _acceptor, held):
            entered, release = threading.Event(), threading.Event()
            from server.sim_reroute_activation import _message
            def wait_message(*args, **kwargs):
                value = _message(*args, **kwargs)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('barrier timed out')
                return value
            errors = []
            def stage():
                try:
                    stage_activation(runtime, 'T1', inbox, proposal)
                except Exception as exc:
                    errors.append(exc)
            def disconnect():
                robot._on_disconnect(robot._client, None, None, None, None)
            with patch('server.sim_reroute_activation._message', side_effect=wait_message):
                worker, callback = threading.Thread(target=stage), threading.Thread(target=disconnect)
                try:
                    worker.start()
                    self.assertTrue(entered.wait(5))
                    callback.start()
                    callback.join(.1)
                    self.assertTrue(callback.is_alive(), 'disconnect callback must wait for staging')
                finally:
                    release.set()
                    worker.join(8)
                    callback.join(8)
                self.assertFalse(worker.is_alive())
                self.assertFalse(callback.is_alive())
                self.assertEqual(errors, [])
            self.assertFalse(robot.ready)
            with self.assertRaises(ValueError):
                stage_activation(runtime, 'T1', inbox, proposal)
            self.assertEqual(runtime.store.list_occupancy(), held)
        self._scenario(scenario)

    def test_proposal_disconnect_callback_waits_for_entered_stage(self):
        def scenario(runtime, robot, proposal, inbox, _acceptor, held):
            entered, release = threading.Event(), threading.Event()
            from server.sim_reroute_activation import _message
            def wait_message(*args, **kwargs):
                value = _message(*args, **kwargs)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('barrier timed out')
                return value
            errors = []
            def stage():
                try:
                    stage_activation(runtime, 'T1', inbox, proposal)
                except Exception as exc:
                    errors.append(exc)
            def disconnect():
                proposal._disconnect(proposal._client, None, None, None, None)
            with patch('server.sim_reroute_activation._message', side_effect=wait_message):
                worker, callback = threading.Thread(target=stage), threading.Thread(target=disconnect)
                try:
                    worker.start()
                    self.assertTrue(entered.wait(5))
                    callback.start()
                    callback.join(.1)
                    self.assertTrue(callback.is_alive(), 'proposal disconnect must wait for staging')
                finally:
                    release.set()
                    worker.join(8)
                    callback.join(8)
                self.assertFalse(worker.is_alive())
                self.assertFalse(callback.is_alive())
                self.assertEqual(errors, [])
            self.assertTrue(proposal._failed.is_set())
            with self.assertRaises(ValueError):
                stage_activation(runtime, 'T1', inbox, proposal)
            self.assertEqual(runtime.store.list_occupancy(), held)
        self._scenario(scenario)

    def test_activation_record_and_reply_finish_before_proposal_stop(self):
        def scenario(runtime, robot, proposal, inbox, acceptor, held):
            stage_activation(runtime, 'T1', inbox, proposal)
            activation_inbox = ActivationReceiptInbox(runtime, inbox, proposal)
            activation_inbox.initialize()
            observer = ActivationReceiptReceiver(activation_inbox)
            guard = SimRerouteActivationGuard(runtime, receipt_inbox=inbox, robot_receiver=proposal)
            activation = SimRerouteActivation(acceptor)
            receiver = SimRerouteActivationMqttReceiver(proposal, guard, activation, receipt_key=bytes(range(32)))
            entered, release = threading.Event(), threading.Event()
            original = activation.record
            def blocked(payload):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('activation barrier timed out')
                return original(payload)
            try:
                observer.start()
                receiver.start()
                with runtime._lock, self.assertRaises(RuntimeError):
                    receiver.stop()
                errors = []
                def publish():
                    try:
                        publish_activation(runtime, 'T1', inbox, proposal)
                    except Exception as exc:
                        errors.append(exc)
                with patch.object(activation, 'record', side_effect=blocked):
                    sending, stop = threading.Thread(target=publish), threading.Thread(target=proposal.stop)
                    try:
                        sending.start()
                        self.assertTrue(entered.wait(5))
                        stop.start()
                        stop.join(.1)
                        self.assertTrue(stop.is_alive(), 'proposal stop must wait for activation effect')
                    finally:
                        release.set()
                        sending.join(8)
                        stop.join(8)
                    self.assertFalse(sending.is_alive())
                    self.assertFalse(stop.is_alive())
                    self.assertEqual(errors, [])
                self.assertEqual(runtime.store.list_occupancy(), held)
            finally:
                release.set()
                receiver.stop()
                observer.stop()
        self._scenario(scenario)

    def test_activation_publish_commit_fences_stop_and_robot_disconnect(self):
        for interruption in ('proposal_stop', 'robot_disconnect'):
            with self.subTest(interruption=interruption):
                def scenario(runtime, robot, proposal, inbox, _acceptor, held):
                    stage_activation(runtime, 'T1', inbox, proposal)
                    entered, release, errors = threading.Event(), threading.Event(), []
                    original_publish = mqtt.Client.publish
                    def blocked(client, topic, *args, **kwargs):
                        if topic.endswith('/reroute/activate'):
                            entered.set()
                            if not release.wait(5):
                                raise RuntimeError('publication barrier timed out')
                        return original_publish(client, topic, *args, **kwargs)
                    def send():
                        try:
                            publish_activation(runtime, 'T1', inbox, proposal)
                        except Exception as exc:
                            errors.append(exc)
                    def interrupt():
                        if interruption == 'proposal_stop':
                            proposal.stop()
                        else:
                            robot._on_disconnect(robot._client, None, None, None, None)
                    with patch.object(mqtt.Client, 'publish', new=blocked):
                        sender, interruptor = threading.Thread(target=send), threading.Thread(target=interrupt)
                        try:
                            sender.start()
                            self.assertTrue(entered.wait(5))
                            interruptor.start()
                            interruptor.join(.1)
                            self.assertTrue(interruptor.is_alive(), 'disconnect must wait for publication fence')
                        finally:
                            release.set()
                            sender.join(8)
                            interruptor.join(8)
                    self.assertFalse(sender.is_alive())
                    self.assertFalse(interruptor.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(runtime.store.list_occupancy(), held)
                    with self.assertRaises((ValueError, RuntimeError)):
                        publish_activation(runtime, 'T1', inbox, proposal)
                self._scenario(scenario)

    def test_activation_receipt_fences_stop_and_robot_disconnect(self):
        for interruption in ('proposal_stop', 'robot_disconnect'):
            with self.subTest(interruption=interruption):
                def scenario(runtime, robot, proposal, inbox, acceptor, held):
                    key = bytes(range(32))
                    stage_activation(runtime, 'T1', inbox, proposal)
                    activation_inbox = ActivationReceiptInbox(runtime, inbox, proposal)
                    activation_inbox.initialize()
                    activation = SimRerouteActivation(acceptor)
                    guard = SimRerouteActivationGuard(runtime, receipt_inbox=inbox, robot_receiver=proposal)
                    receiver = SimRerouteActivationMqttReceiver(proposal, guard, activation, receipt_key=key)
                    payloads, ready = queue.Queue(), threading.Event()
                    monitor = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311,
                                          reconnect_on_failure=False)
                    monitor.on_connect = lambda c, _u, _f, r, _p: c.subscribe('agv/sim-r1/reroute/activation-receipt', qos=1) if not r.is_failure else None
                    monitor.on_subscribe = lambda _c, _u, _m, rs, _p: ready.set() if len(rs) == 1 and not rs[0].is_failure else None
                    monitor.on_message = lambda _c, _u, m: payloads.put(m.payload)
                    monitor.connect('127.0.0.1', runtime.port)
                    monitor.loop_start()
                    try:
                        self.assertTrue(ready.wait(5))
                        receiver.start()
                        publish_activation(runtime, 'T1', inbox, proposal)
                        payload = payloads.get(timeout=8)
                        from server.sim_reroute_activate_receipt import _activation
                        entered, release, errors = threading.Event(), threading.Event(), []
                        def blocked(*args, **kwargs):
                            value = _activation(*args, **kwargs)
                            entered.set()
                            if not release.wait(5):
                                raise RuntimeError('receipt barrier timed out')
                            return value
                        def record():
                            try:
                                activation_inbox.record(payload)
                            except Exception as exc:
                                errors.append(exc)
                        def interrupt():
                            if interruption == 'proposal_stop':
                                proposal.stop()
                            else:
                                robot._on_disconnect(robot._client, None, None, None, None)
                        with patch('server.sim_reroute_activate_receipt._activation', side_effect=blocked):
                            worker, interruptor = threading.Thread(target=record), threading.Thread(target=interrupt)
                            try:
                                worker.start()
                                self.assertTrue(entered.wait(5))
                                interruptor.start()
                                interruptor.join(.1)
                                self.assertTrue(interruptor.is_alive(), 'disconnect must wait for receipt record')
                            finally:
                                release.set()
                                worker.join(8)
                                interruptor.join(8)
                        self.assertFalse(worker.is_alive())
                        self.assertFalse(interruptor.is_alive())
                        self.assertEqual(errors, [])
                        with self.assertRaises((ValueError, RuntimeError)):
                            activation_inbox.record(payload)
                        self.assertEqual(runtime.store.list_occupancy(), held)
                    finally:
                        receiver.stop()
                        monitor.disconnect()
                        monitor.loop_stop()
                        from server.sim_mqtt_cleanup import reset_stopped_client
                        reset_stopped_client(monitor)
                self._scenario(scenario)


if __name__ == '__main__':
    unittest.main()
