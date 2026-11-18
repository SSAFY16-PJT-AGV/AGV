"""Only proposed, isolated simulated response observation; no physical motion."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server.allocation_store import AllocationStore
from server.coordinator import Coordinator
from server.field_dispatch import FieldCandidate
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress
from server.tests.test_coordinator import FakePublisher
from server.tests.test_occurrence_allocation_lab import laboratory_map


class FakeClient:
    def __init__(self, *args, **kwargs):
        self._manual_ack = False
        self.acks = []
        self.subscriptions = []
        self.disconnected = False
        self.reject_subscribe = False
        self.ack_rc = 0
        self.connected_host = None
        self.on_connect = self.on_subscribe = self.on_message = self.on_disconnect = None

    def manual_ack_set(self, value): self._manual_ack = value
    def connect(self, host, port, keepalive=60):
        self.connected_host = host
        return 0
    def subscribe(self, topics):
        self.subscriptions = topics
        return 0, 7
    def loop_start(self):
        self.on_connect(self, None, None, SimpleNamespace(is_failure=False), None)
        self.on_subscribe(self, None, 7, [SimpleNamespace(is_failure=self.reject_subscribe,
                                                         value=128 if self.reject_subscribe else 1)], None)
        return 0
    def loop_stop(self): return 0
    def disconnect(self): self.disconnected = True; return 0
    def ack(self, mid, qos): self.acks.append((mid, qos)); return self.ack_rc


class SimCommandObservationReceiverTests(unittest.TestCase):
    def setUp(self):
        from server.sim_command_observations import SimCommandObservations
        self.folder = tempfile.TemporaryDirectory(prefix='sim-command-receiver-')
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'allocation.sqlite3'
        self.data = laboratory_map()
        self.store = AllocationStore(self.path, field_required=True)
        self.store.initialize()
        self.store.create_task('T1')
        self.coordinator = Coordinator(self.store, self.data, FakePublisher(), field_simulation=True)
        gate = RecoveryGate(self.path, field_required=True)
        gate.initialize()
        self.progress = SegmentProgress(self.store, self.data, simulation_enabled=True,
                                        field_simulation=True, recovery_gate=gate)
        self.progress.initialize()
        self.observations = SimCommandObservations(self.progress, {'sim-r1': {'T1': 'boot-1'}}, simulation=True)
        self.observations.initialize()
        self.coordinator.prepare_field('T1', 'D', [FieldCandidate('sim-r1', 'A', 'E0', True)], {'type': 'none'})
        self.coordinator.publish('T1')
        self.order = self.store.get_order('T1')
        self.response = {'event_id': 'resp-1', 'robot_session': 'boot-1', 'order_id': 'T1', 'seq': 1,
                         'map_id': 'lab@v1', 'outcome': 'accepted',
                         'order_digest': hashlib.sha256(self.order['payload'].encode('utf-8')).hexdigest()}

    def create(self, *, client=None, start=True):
        from server.sim_command_observation_receiver import SimCommandObservationReceiver
        fake = client or FakeClient()
        with patch('server.sim_command_observation_receiver.mqtt.Client', return_value=fake), \
                patch('server.sim_command_observation_receiver._configure_client'), \
                patch('server.sim_command_observation_receiver._verify_no_proxy'):
            receiver = SimCommandObservationReceiver(self.observations, ['sim-r1'], 18883, simulation=True)
        self.addCleanup(receiver.stop)
        if start:
            receiver.start()
        return receiver, fake

    def message(self, response=None, *, topic='sim-observation/sim-r1/order-response', qos=1, retain=False):
        payload = json.dumps(self.response if response is None else response, separators=(',', ':')).encode()
        return SimpleNamespace(topic=topic, qos=qos, retain=retain, payload=payload, mid=12)

    def stored(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT robot_id,event_id,task_id,seq,outcome FROM sim_command_observations').fetchall()

    def test_loopback_only_and_ack_after_record_or_identical_duplicate(self):
        receiver, client = self.create()
        self.assertEqual(client.connected_host, '127.0.0.1')
        self.assertEqual(client.subscriptions, [('sim-observation/sim-r1/order-response', 1)])
        original = client.ack
        def assert_durable_before_ack(mid, qos):
            self.assertEqual(self.stored(), [('sim-r1', 'resp-1', 'T1', 1, 'accepted')])
            return original(mid, qos)
        client.ack = assert_durable_before_ack
        before = self.store.list_occupancy()
        client.on_message(client, None, self.message())
        client.on_message(client, None, self.message())
        self.assertEqual(client.acks, [(12, 1), (12, 1)])
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertEqual(self.store.get_order('T1'), self.order)
        self.assertTrue(receiver.ready)

    def test_rejected_response_is_observation_not_recovery_permission(self):
        receiver, client = self.create()
        client.on_message(client, None, self.message(dict(self.response, outcome='rejected')))
        self.assertEqual(self.stored(), [('sim-r1', 'resp-1', 'T1', 1, 'rejected')])
        self.assertEqual(client.acks, [(12, 1)])
        self.assertEqual(self.store.get_order('T1'), self.order)

    def test_conflicting_duplicate_fails_without_ack_and_closes(self):
        receiver, client = self.create()
        client.on_message(client, None, self.message())
        client.on_message(client, None, self.message(dict(self.response, outcome='rejected')))
        self.assertEqual(client.acks, [(12, 1)])
        self.assertFalse(receiver.ready)
        self.assertTrue(client.disconnected)
        self.assertEqual(self.stored(), [('sim-r1', 'resp-1', 'T1', 1, 'accepted')])

    def test_bad_flags_topic_and_json_are_not_acknowledged(self):
        for mutation in ('qos0', 'qos2', 'retained', 'other-topic', 'invalid-json'):
            with self.subTest(mutation=mutation):
                receiver, client = self.create()
                message = self.message(qos={'qos0': 0, 'qos2': 2}.get(mutation, 1),
                                       retain=mutation == 'retained',
                                       topic='agv/sim-r1/event' if mutation == 'other-topic'
                                       else 'sim-observation/sim-r1/order-response')
                if mutation == 'invalid-json': message.payload = b'{'
                client.on_message(client, None, message)
                self.assertFalse(receiver.ready)
                self.assertEqual(client.acks, [])
                self.assertTrue(client.disconnected)
        self.assertEqual(self.stored(), [])

    def test_pre_suback_message_is_not_recorded_or_application_acknowledged(self):
        receiver, client = self.create(start=False)
        client.on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
        client.on_message(client, None, self.message())
        self.assertEqual(self.stored(), [])
        self.assertEqual(client.acks, [])
        self.assertFalse(receiver.ready)
        self.assertTrue(client.disconnected)
        client.on_subscribe(client, None, 7, [SimpleNamespace(is_failure=False, value=1)], None)
        self.assertFalse(receiver.ready)

    def test_stop_waits_for_slow_record_and_finishes_after_it_is_released(self):
        receiver, client = self.create()
        entered, release, stop_entered, stop_done = (threading.Event() for _ in range(4))
        errors = []
        original = self.observations.record

        class ObservedLock:
            def __init__(self):
                self.inner = threading.RLock()
            def __enter__(self):
                if threading.current_thread().name == 'observation-stop':
                    stop_entered.set()
                self.inner.acquire()
                return self
            def __exit__(self, *args):
                self.inner.release()

        receiver._lock = ObservedLock()

        def slow_record(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test did not release observation recording')
            return original(*args, **kwargs)

        def deliver():
            try:
                client.on_message(client, None, self.message())
            except BaseException as exc:
                errors.append(exc)

        def stop_receiver():
            try:
                receiver.stop()
                stop_done.set()
            except BaseException as exc:
                errors.append(exc)

        with patch.object(self.observations, 'record', side_effect=slow_record):
            delivery = threading.Thread(target=deliver)
            stopper = threading.Thread(target=stop_receiver, name='observation-stop')
            delivery.start()
            try:
                self.assertTrue(entered.wait(5), 'delivery must enter record')
                stopper.start()
                self.assertTrue(stop_entered.wait(5), 'stop must be attempted')
                self.assertFalse(stop_done.wait(.1), 'stop must wait for the active record callback')
            finally:
                release.set()
                delivery.join(5)
                if stopper.ident is not None:
                    stopper.join(5)
            self.assertFalse(delivery.is_alive())
            self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(stop_done.is_set())
        self.assertFalse(receiver.ready)
        self.assertEqual(self.stored(), [('sim-r1', 'resp-1', 'T1', 1, 'accepted')])

    def test_database_failure_never_acknowledges(self):
        receiver, client = self.create()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_obs BEFORE INSERT ON sim_command_observations "
                       "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        client.on_message(client, None, self.message())
        self.assertEqual(client.acks, [])
        self.assertFalse(receiver.ready)
        self.assertTrue(client.disconnected)
        self.assertEqual(self.stored(), [])

    def test_subscription_refusal_or_downgrade_is_not_ready(self):
        client = FakeClient()
        client.reject_subscribe = True
        receiver, _ = self.create(client=client, start=False)
        with self.assertRaises(RuntimeError):
            receiver.start()
        self.assertFalse(receiver.ready)
        self.assertTrue(client.disconnected)
        self.assertEqual(client.acks, [])
        receiver2, client2 = self.create(start=False)
        client2.on_connect(client2, None, None, SimpleNamespace(is_failure=False), None)
        client2.on_subscribe(client2, None, 7, [SimpleNamespace(is_failure=False, value=0)], None)
        self.assertFalse(receiver2.ready)
        self.assertTrue(client2.disconnected)

    def test_repeated_connect_callback_does_not_restore_readiness(self):
        receiver, client = self.create()
        client.on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
        self.assertFalse(receiver.ready)
        self.assertTrue(client.disconnected)

    def test_malformed_connack_is_rejected_without_raising_in_callback(self):
        receiver, client = self.create(start=False)
        client.on_connect(client, None, None, None, None)
        self.assertFalse(receiver.ready)
        self.assertTrue(client.disconnected)

    def test_ack_error_preserves_record_and_duplicate_after_fresh_receiver(self):
        receiver, client = self.create()
        client.ack_rc = 1
        client.on_message(client, None, self.message())
        self.assertFalse(receiver.ready)
        self.assertEqual(self.stored(), [('sim-r1', 'resp-1', 'T1', 1, 'accepted')])
        self.assertIsNone(receiver.last_result)
        receiver.stop()
        fresh, next_client = self.create()
        next_client.on_message(next_client, None, self.message())
        self.assertEqual(next_client.acks, [(12, 1)])
        self.assertEqual(fresh.last_result, {'status': 'duplicate', 'outcome': 'accepted'})
        self.assertEqual(self.stored(), [('sim-r1', 'resp-1', 'T1', 1, 'accepted')])

    def test_disconnect_or_stop_cannot_accept_late_message(self):
        receiver, client = self.create()
        client.on_disconnect(client, None, None, None, None)
        self.assertFalse(receiver.ready)
        client.on_message(client, None, self.message())
        self.assertEqual(client.acks, [])
        self.assertEqual(self.stored(), [])
        with self.assertRaises(RuntimeError):
            receiver.start()

    def test_intentional_stop_is_not_reported_as_connection_failure(self):
        receiver, client = self.create()
        receiver.stop()
        client.on_disconnect(client, None, None, None, None)
        self.assertIsNone(receiver.last_error)
        self.assertFalse(receiver.ready)

    def test_stale_recovery_epoch_never_acknowledges_late_response(self):
        receiver, client = self.create()
        newer = RecoveryGate(self.path, field_required=True)
        newer.initialize()
        client.on_message(client, None, self.message())
        self.assertEqual(client.acks, [])
        self.assertFalse(receiver.ready)
        self.assertEqual(self.stored(), [])


if __name__ == '__main__':
    unittest.main()
