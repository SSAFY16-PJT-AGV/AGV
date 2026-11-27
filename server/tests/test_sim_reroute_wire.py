"""Isolated MQTT publication must preserve uncertain reroute reservations."""
import queue
import copy
import gc
import threading
import unittest
from unittest.mock import patch

import paho.mqtt.client as mqtt

from server.sim_reroute_wire import publish_proposal, SimRerouteProposalGuard
from server.sim_reroute_receipt import SimRerouteReceiptInbox, SimRerouteReceiptReceiver
from server.tests.test_sim_reroute_reservation import RerouteReservationTest
from sim.reroute_acceptance import SimRerouteAcceptance
from sim.reroute_mqtt import SimRerouteMqttReceiver, verify_receipt


class RerouteWireTest(unittest.TestCase):
    def setUp(self):
        # Paho owns callback cycles; collect them while the test is still
        # active so its client finalizer closes the loop wakeup socket pair.
        self.addCleanup(gc.collect)

    def test_prepared_proposal_is_published_once_and_checked_by_robot(self):
        def after_prepare(runtime, robots):
            received, ready = queue.Queue(), threading.Event()
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311,
                                 reconnect_on_failure=False)
            def connected(c, _data, _flags, reason, _props):
                if not reason.is_failure:
                    c.subscribe('agv/sim-r1/reroute/prepare', qos=1)
            def subscribed(_c, _data, _mid, reasons, _props):
                if len(reasons) == 1 and not reasons[0].is_failure:
                    ready.set()
            client.on_connect = connected
            client.on_subscribe = subscribed
            client.on_message = lambda _c, _u, m: received.put((m.topic, m.payload, m.qos, m.retain))
            client.connect('127.0.0.1', runtime.port)
            client.loop_start()
            try:
                self.assertTrue(ready.wait(5))
                before = runtime.store.list_occupancy()
                result = publish_proposal(runtime, 'T1')
                self.assertEqual(result['state'], 'uncertain')
                topic, payload, qos, retained = received.get(timeout=5)
                self.assertEqual((topic, qos, retained), ('agv/sim-r1/reroute/prepare', 1, False))
                guard = SimRerouteProposalGuard(runtime, robot_id='sim-r1')
                acceptance = SimRerouteAcceptance(robots['sim-r1'].executor, robot_session='boot-1')
                acceptance.initialize()
                with guard.guard(payload):
                    reply = acceptance.accept(payload)
                self.assertEqual(reply['state'], 'accepted_not_executed')
                self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
                self.assertEqual(runtime.store.list_occupancy(), before)
                with self.assertRaises((ValueError, RuntimeError)):
                    publish_proposal(runtime, 'T1')
                with self.assertRaises(queue.Empty):
                    received.get(timeout=.2)
            finally:
                client.disconnect()
                client.loop_stop()
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)

    def test_robot_receives_and_signs_durable_proposal_over_mqtt(self):
        def after_prepare(runtime, robots):
            received, ready = queue.Queue(), threading.Event()
            monitor = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311,
                                  reconnect_on_failure=False)
            monitor.on_connect = lambda c, _u, _f, r, _p: c.subscribe('agv/sim-r1/reroute/receipt', qos=1) if not r.is_failure else None
            monitor.on_subscribe = lambda _c, _u, _m, r, _p: ready.set() if len(r) == 1 and not r[0].is_failure else None
            monitor.on_message = lambda _c, _u, m: received.put(m.payload)
            monitor.connect('127.0.0.1', runtime.port)
            monitor.loop_start()
            key = bytes(range(32))
            guard = SimRerouteProposalGuard(runtime, robot_id='sim-r1')
            acceptor = SimRerouteAcceptance(robots['sim-r1'].executor, robot_session='boot-1')
            duplicate = copy.copy(robots['sim-r1'])
            with self.assertRaises(ValueError):
                SimRerouteMqttReceiver(duplicate, guard, acceptor, receipt_key=key)
            receiver = SimRerouteMqttReceiver(robots['sim-r1'], guard, acceptor, receipt_key=key)
            try:
                self.assertTrue(ready.wait(5))
                receiver.start()
                self.assertEqual(publish_proposal(runtime, 'T1')['state'], 'uncertain')
                signed = received.get(timeout=8)
                receipt = verify_receipt(signed, key)
                self.assertEqual(receipt['state'], 'accepted_not_executed')
                self.assertEqual((receipt['robot_id'], receipt['robot_session'], receipt['task_id']),
                                 ('sim-r1', 'boot-1', 'T1'))
                self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
                with self.assertRaises(ValueError):
                    verify_receipt(signed, bytes([1]) * 32)
            finally:
                receiver.stop()
                monitor.disconnect()
                monitor.loop_stop()
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)

    def test_parallel_stop_waits_and_start_cannot_outlive_stop(self):
        def after_prepare(runtime, robots):
            key = bytes(range(32))
            guard = SimRerouteProposalGuard(runtime, robot_id='sim-r1')
            acceptor = SimRerouteAcceptance(robots['sim-r1'].executor, robot_session='boot-1')
            inbox = SimRerouteReceiptInbox(runtime, {'sim-r1': key})
            inbox.initialize()
            def make_receivers():
                return (SimRerouteMqttReceiver(robots['sim-r1'], guard, acceptor, receipt_key=key),
                        SimRerouteReceiptReceiver(inbox))
            class StuckWorker:
                def join(self, timeout=None):
                    pass
                def is_alive(self):
                    return True
            for receiver in make_receivers():
                receiver._worker = StuckWorker()
                with patch.object(receiver._client, 'disconnect'), patch.object(receiver._client, 'loop_stop'):
                    with self.assertRaises(RuntimeError):
                        receiver.stop()
                    with self.assertRaises(RuntimeError):
                        receiver.stop()
            for failing_action in ('disconnect', 'loop_stop'):
                for receiver in make_receivers():
                    with patch.object(receiver._client, failing_action, side_effect=OSError('test cleanup failure')), \
                            patch.object(receiver._client, 'loop_stop' if failing_action == 'disconnect' else 'disconnect'):
                        with self.assertRaises(RuntimeError):
                            receiver.stop()
                        with self.assertRaises(RuntimeError):
                            receiver.stop()
            for receiver in make_receivers():
                entered, release = threading.Event(), threading.Event()
                errors = []
                def disconnect():
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('disconnect barrier timed out')
                def stop_receiver():
                    try:
                        receiver.stop()
                    except Exception as error:
                        errors.append(error)
                with patch.object(receiver._client, 'disconnect', side_effect=disconnect), \
                        patch.object(receiver._client, 'loop_stop'):
                    first = threading.Thread(target=stop_receiver)
                    second = threading.Thread(target=stop_receiver)
                    try:
                        first.start()
                        self.assertTrue(entered.wait(5))
                        second.start()
                        second.join(timeout=.1)
                        self.assertTrue(second.is_alive(), 'second stop must wait for first stop')
                    finally:
                        release.set()
                        first.join(timeout=8)
                        second.join(timeout=8)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(errors, [])

            from types import SimpleNamespace
            for receiver in make_receivers():
                connected = threading.Event()
                errors = []
                def connect(*_args, **_kwargs):
                    connected.set()
                    return mqtt.MQTT_ERR_SUCCESS
                def start_receiver():
                    try:
                        receiver.start()
                    except Exception as error:
                        errors.append(error)
                with patch.object(receiver._client, 'connect', side_effect=connect), \
                        patch.object(receiver._client, 'loop_start', return_value=mqtt.MQTT_ERR_SUCCESS), \
                        patch.object(receiver._client, 'disconnect'), patch.object(receiver._client, 'loop_stop'):
                    first = threading.Thread(target=start_receiver)
                    second = threading.Thread(target=receiver.stop)
                    first.start()
                    self.assertTrue(connected.wait(5))
                    second.start()
                    second.join(timeout=.1)
                    self.assertTrue(second.is_alive(), 'stop must wait for in-progress start')
                    receiver._mid = 3
                    receiver._subscribe(None, None, 3, [SimpleNamespace(is_failure=False, value=1)], None)
                    first.join(timeout=8)
                    second.join(timeout=8)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(errors, [])
                self.assertFalse(receiver._ready.is_set())
                self.assertFalse(receiver._worker.is_alive())
                receiver._subscribe(None, None, 3, [SimpleNamespace(is_failure=False, value=1)], None)
                self.assertFalse(receiver._ready.is_set(), 'late SUBACK cannot revive stopped receiver')
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)

    def test_server_records_signed_robot_receipt_without_releasing_segments(self):
        def after_prepare(runtime, robots):
            key = bytes(range(32))
            guard = SimRerouteProposalGuard(runtime, robot_id='sim-r1')
            robot_inbox = SimRerouteAcceptance(robots['sim-r1'].executor, robot_session='boot-1')
            robot_receiver = SimRerouteMqttReceiver(robots['sim-r1'], guard, robot_inbox, receipt_key=key)
            inbox = SimRerouteReceiptInbox(runtime, {'sim-r1': key})
            inbox.initialize()
            server_receiver = SimRerouteReceiptReceiver(inbox)
            socket_pairs = ()
            try:
                server_receiver.start()
                robot_receiver.start()
                socket_pairs = (robot_receiver._client._sockpairR, robot_receiver._client._sockpairW,
                                server_receiver._client._sockpairR, server_receiver._client._sockpairW)
                held = runtime.store.list_occupancy()
                publish_proposal(runtime, 'T1')
                import time
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and inbox.get('T1') is None:
                    time.sleep(.02)
                record = inbox.get('T1')
                self.assertIsNotNone(record)
                self.assertEqual(record['state'], 'recorded_not_activated')
                self.assertEqual(record['robot_id'], 'sim-r1')
                from contextlib import closing
                import sqlite3
                from server.allocation_store import _verify_field_global
                with closing(sqlite3.connect(runtime.path, isolation_level=None)) as db:
                    db.execute('BEGIN')
                    self.assertEqual(db.execute("SELECT state FROM sim_reroute_generations WHERE task_id='T1'")
                                     .fetchone(), ('accepted',))
                    _verify_field_global(db, field_required=True)
                    db.execute('ROLLBACK')
                self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
                self.assertEqual(runtime.store.list_occupancy(), held)
                # A stored, correctly authenticated duplicate does not create
                # a second acceptance or authorize a new movement.
                self.assertFalse(inbox.record(record['payload'])['new'])
                self.assertEqual(runtime.store.list_occupancy(), held)
                altered = bytearray(record['payload'])
                altered[-3] ^= 1
                with self.assertRaises(ValueError):
                    inbox.record(bytes(altered))
                self.assertEqual(runtime.store.list_occupancy(), held)
            finally:
                robot_receiver.stop()
                server_receiver.stop()
            self.assertEqual(len(socket_pairs), 4)
            self.assertTrue(all(socket is not None and socket.fileno() == -1 for socket in socket_pairs))
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)

    def test_failed_publish_is_uncertain_and_never_retried(self):
        def after_prepare(runtime, robots):
            before = runtime.store.list_occupancy()
            with patch.object(mqtt.Client, 'publish', side_effect=RuntimeError('connection lost')):
                with self.assertRaises(RuntimeError):
                    publish_proposal(runtime, 'T1')
            with self.assertRaises((ValueError, RuntimeError)):
                publish_proposal(runtime, 'T1')
            self.assertEqual(runtime.store.list_occupancy(), before)
            self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
            from contextlib import closing
            import sqlite3
            with closing(sqlite3.connect(runtime.path)) as db:
                self.assertEqual(db.execute("SELECT state FROM sim_reroute_generations WHERE task_id='T1'")
                                 .fetchone(), ('uncertain',))
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)

    def test_receiver_shutdown_waits_for_entered_acceptance_and_sends_no_receipt(self):
        def after_prepare(runtime, robots):
            entered, release = threading.Event(), threading.Event()
            guard = SimRerouteProposalGuard(runtime, robot_id='sim-r1')
            acceptor = SimRerouteAcceptance(robots['sim-r1'].executor, robot_session='boot-1')
            original = acceptor.accept
            def blocked(payload):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test timed out')
                return original(payload)
            receiver = SimRerouteMqttReceiver(robots['sim-r1'], guard, acceptor, receipt_key=bytes(range(32)))
            try:
                receiver.start()
                with patch.object(acceptor, 'accept', side_effect=blocked):
                    publish_errors = []
                    def publish():
                        try:
                            publish_proposal(runtime, 'T1')
                        except Exception as error:
                            publish_errors.append(error)
                    publishing = threading.Thread(target=publish)
                    publishing.start()
                    self.assertTrue(entered.wait(5))
                    errors = []
                    def close():
                        try:
                            receiver.stop()
                        except Exception as error:
                            errors.append(error)
                    thread = threading.Thread(target=close)
                    thread.start()
                    thread.join(timeout=.1)
                    self.assertTrue(thread.is_alive(), 'shutdown must wait for entered acceptance')
                    release.set()
                    thread.join(timeout=8)
                    publishing.join(timeout=8)
                    self.assertFalse(thread.is_alive())
                    self.assertFalse(publishing.is_alive())
                    self.assertEqual(publish_errors, [])
                    self.assertEqual(errors, [])
                self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
                from contextlib import closing
                import sqlite3
                with closing(sqlite3.connect(runtime.path)) as db:
                    self.assertEqual(db.execute("SELECT state FROM sim_reroute_generations WHERE task_id='T1'")
                                     .fetchone(), ('uncertain',))
            finally:
                release.set()
                receiver.stop()
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)

    def test_broker_ack_without_robot_receipt_does_not_release_or_retry(self):
        def after_prepare(runtime, robots):
            before = runtime.store.list_occupancy()
            result = publish_proposal(runtime, 'T1')
            self.assertTrue(result['broker_ack'])
            self.assertEqual(result['state'], 'uncertain')
            with self.assertRaises((ValueError, RuntimeError)):
                publish_proposal(runtime, 'T1')
            self.assertEqual(runtime.store.list_occupancy(), before)
            self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
        RerouteReservationTest(methodName='runTest')._exercise(False, after_prepare=after_prepare)


if __name__ == '__main__':
    unittest.main()
