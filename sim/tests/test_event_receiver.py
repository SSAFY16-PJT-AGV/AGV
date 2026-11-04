import importlib
import importlib.util
import json
from types import SimpleNamespace
import unittest
from unittest.mock import create_autospec

from server.recovery import RecoveryGate


class FakeClient:
    def __init__(self, *, reconnect=False):
        self._reconnect_on_failure = reconnect
        self.subscriptions = []
        self.network_calls = []
        self._manual_ack = False
        self._clean_session = False
        self._client_id = b'sim-observer'
        self.acks = []
        self.ack_result = 0

    def manual_ack_set(self, enabled):
        self._manual_ack = enabled

    def ack(self, mid, qos):
        self.acks.append((mid, qos))
        return self.ack_result

    def disconnect(self):
        self.network_calls.append('disconnect')

    def subscribe(self, topics):
        self.subscriptions.append(topics)
        return 0, 13

    def connect(self, *args):
        self.network_calls.append('connect')

    def publish(self, *args):
        self.network_calls.append('publish')

    def reconnect(self):
        self.network_calls.append('reconnect')


class FakeProgress:
    def __init__(self, gate):
        self._gate = gate
        self.calls = []
        self.failure = None

    def advance(self, robot_id, event, *, retained):
        self.calls.append((robot_id, event, retained))
        if self.failure:
            raise self.failure
        return {'status': 'advanced', 'next_seq': 2}


class FakeInbox:
    def __init__(self, progress, robot_tasks=None):
        self.progress = progress
        self.path = progress._gate.path
        self.robot_tasks = robot_tasks or {'sim-r1': frozenset(('T1',))}
        self.stored = []
        self.fail = None

    def store(self, robot_id, event):
        if self.fail:
            raise self.fail
        self.stored.append((robot_id, event))
        return {'status': 'stored', 'event_id': 'E1', 'state': 'pending'}

    def retry(self, robot_id, event_id, progress):
        return progress.advance(robot_id, self.stored[-1][1], retained=False)


def message(topic='agv/sim-r1/event', payload=b'{"type":"rear_clear","order_id":"T1"}', qos=1, retain=False):
    return SimpleNamespace(topic=topic, payload=payload, qos=qos, retain=retain, mid=42)


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('sim.event_receiver'), 'event receiver not implemented')
        self.module = importlib.import_module('sim.event_receiver')
        self.gate = create_autospec(RecoveryGate, instance=True, spec_set=True)
        object.__setattr__(self.gate, 'path', 'test-db')
        self.client, self.progress = FakeClient(), FakeProgress(self.gate)
        self.results, self.errors = [], []
        self.inbox = FakeInbox(self.progress)
        self.receiver = self.module.SimEventReceiver(self.client, self.progress, inbox=self.inbox, recovery_gate=self.gate,
            robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True,
            on_result=self.results.append, on_error=self.errors.append)

    def ready(self):
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.client.on_subscribe(self.client, None, 13,
                                 [SimpleNamespace(is_failure=False, value=1)] * 2, None)

    def test_disabled_and_automatic_reconnect_client_are_rejected(self):
        with self.assertRaises(ValueError):
            self.module.SimEventReceiver(FakeClient(), self.progress, inbox=self.inbox, recovery_gate=self.gate,
                                         robot_tasks={'sim-r1': ('T1',)})
        with self.assertRaises(ValueError):
            self.module.SimEventReceiver(FakeClient(reconnect=True), self.progress, inbox=self.inbox, recovery_gate=self.gate,
                                         robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True)

    def test_persistent_session_with_stable_client_id_is_required(self):
        client = FakeClient()
        client._clean_session = True
        with self.assertRaises(ValueError):
            self.module.SimEventReceiver(client, self.progress, inbox=self.inbox, recovery_gate=self.gate,
                                         robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True)
        client._clean_session = False
        client._client_id = b''
        with self.assertRaises(ValueError):
            self.module.SimEventReceiver(client, self.progress, inbox=self.inbox, recovery_gate=self.gate,
                                         robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True)

    def test_ack_occurs_after_store_but_before_retry(self):
        self.ready()
        phases = []
        original_store, original_retry, original_ack = self.inbox.store, self.inbox.retry, self.client.ack
        def store(*args):
            phases.append('store')
            return original_store(*args)
        def ack(*args):
            phases.append('ack')
            return original_ack(*args)
        def retry(*args):
            phases.append('retry')
            return original_retry(*args)
        self.inbox.store, self.client.ack, self.inbox.retry = store, ack, retry
        self.client.on_message(self.client, None, message())
        self.assertEqual(phases, ['store', 'ack', 'retry'])

    def test_store_failure_closes_revokes_and_never_acks(self):
        self.ready()
        self.inbox.fail = RuntimeError('database failure')
        self.client.on_message(self.client, None, message())
        self.assertFalse(self.receiver.ready)
        self.assertEqual(self.client.acks, [])
        self.assertEqual(self.progress.calls, [])
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))
        self.assertIn('disconnect', self.client.network_calls)
        self.assertTrue(self.errors)

    def test_duplicate_pending_is_acked_without_automatic_retry(self):
        self.ready()
        self.inbox.store = lambda robot, event: {'status': 'duplicate', 'event_id': 'E1', 'state': 'pending'}
        self.client.on_message(self.client, None, message())
        self.assertEqual(self.client.acks, [(42, 1)])
        self.assertFalse(self.progress.calls)
        self.assertEqual(self.results[0]['status'], 'duplicate')

    def test_ack_failure_closes_and_revokes_after_store(self):
        self.ready()
        self.client.ack_result = 4
        self.client.on_message(self.client, None, message())
        self.assertEqual(len(self.inbox.stored), 1)
        self.assertFalse(self.receiver.ready)
        self.assertFalse(self.progress.calls)
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))
        self.assertIn('disconnect', self.client.network_calls)
        self.assertTrue(self.errors)

    def test_retry_failure_after_ack_remains_stored_for_explicit_retry(self):
        self.ready()
        self.progress.failure = ValueError('not reconciled')
        self.client.on_message(self.client, None, message())
        self.assertEqual(len(self.inbox.stored), 1)
        self.assertEqual(self.client.acks, [(42, 1)])
        self.assertTrue(self.errors)
        self.progress.failure = None
        result = self.inbox.retry('sim-r1', 'E1', self.progress)
        self.assertEqual(result['status'], 'advanced')

    def test_offline_qos_one_is_acked_only_after_invalidation(self):
        self.ready()
        observed = []
        self.gate.invalidate_many.side_effect = lambda tasks: observed.append(list(self.client.acks))
        self.client.on_message(self.client, None, message(topic='agv/sim-r1/conn', payload=b'offline'))
        self.assertEqual(observed, [[]])
        self.assertEqual(self.client.acks, [(42, 1)])

    def test_offline_invalidation_failure_does_not_ack(self):
        self.ready()
        self.gate.invalidate_many.side_effect = RuntimeError('bad db')
        self.client.on_message(self.client, None, message(topic='agv/sim-r1/conn', payload=b'offline'))
        self.assertEqual(self.client.acks, [])
        self.assertFalse(self.receiver.ready)
        self.assertTrue(self.errors)

    def test_task_list_and_gate_must_be_explicit_and_match_progress(self):
        with self.assertRaises(ValueError):
            self.module.SimEventReceiver(FakeClient(), self.progress, inbox=self.inbox, recovery_gate=self.gate,
                                         robot_tasks={}, simulation_enabled=True)
        with self.assertRaises(ValueError):
            self.module.SimEventReceiver(FakeClient(), self.progress, inbox=self.inbox,
                                         recovery_gate=create_autospec(RecoveryGate, instance=True),
                                         robot_tasks={'sim-r1': ('T1',)}, simulation_enabled=True)

    def test_subscribes_qos_one_and_forwards_with_valid_topic(self):
        self.ready()
        self.assertTrue(self.receiver.ready)
        self.assertEqual(self.client.subscriptions, [[('agv/+/event', 1), ('agv/+/conn', 1)]])
        self.client.on_message(self.client, None, message())
        self.assertEqual(self.progress.calls, [('sim-r1', {'type': 'rear_clear', 'order_id': 'T1'}, False)])
        self.assertEqual(self.results, [{'status': 'advanced', 'next_seq': 2}])
        self.assertTrue(self.client._manual_ack)
        self.assertEqual(self.client.acks, [(42, 1)])
        self.assertEqual(len(self.inbox.stored), 1)
        self.assertEqual(self.client.network_calls, [])

    def test_disconnected_unacknowledged_or_failed_subscribe_never_forwards(self):
        self.client.on_message(self.client, None, message())
        self.assertEqual(self.progress.calls, [])
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.client.on_subscribe(self.client, None, 13,
                                 [SimpleNamespace(is_failure=False, value=0),
                                  SimpleNamespace(is_failure=False, value=1)], None)
        self.client.on_message(self.client, None, message())
        self.assertFalse(self.receiver.ready)
        self.assertEqual(self.progress.calls, [])
        self.assertTrue(self.errors)

    def test_invalid_payload_retained_qos_zero_and_topics_do_not_advance(self):
        self.ready()
        invalid = [message(qos=0), message(retain=True), message(topic='agv/real-r1/event'),
                   message(topic='agv/sim-r1/event/extra'), message(topic='agv/sim-/event'),
                   message(topic='agv/sim-unknown/event'),
                   message(payload=b'{"type":"rear_clear","order_id":"other"}'),
                   message(payload=b'\xff'), message(payload=b'[]'),
                   message(payload=b'{"x":1,"x":2}'), message(payload=b'{"x":NaN}'),
                   message(payload=b'{"x":1e999}'), message(payload=b'{"x":"\\ud800"}'),
                   message(payload=b' ' * (256 * 1024 + 1))]
        for item in invalid:
            self.client.on_message(self.client, None, item)
        self.assertEqual(self.progress.calls, [])
        self.assertEqual(len(self.errors), len(invalid))

    def test_bad_wire_payload_does_not_escape_in_error_object(self):
        self.ready()
        for payload in (b'{"sensitive":"private"', b'private-\xff'):
            self.client.on_message(self.client, None, message(payload=payload))
            error = self.errors[-1]
            self.assertNotIn('private', str(error))
            self.assertIsNone(error.__context__)
            self.assertIsNone(error.__traceback__)
            self.assertIsNone(getattr(error, 'doc', None))
            self.assertIsNone(getattr(error, 'object', None))

    def test_progress_failure_is_observable_and_does_not_escape_callback(self):
        self.ready()
        self.progress.failure = ValueError('bad event')
        self.client.on_message(self.client, None, message())
        self.assertEqual(self.results, [])
        self.assertEqual(len(self.errors), 1)
        self.assertIsNotNone(self.receiver.last_error)

    def test_disconnect_closes_receiver_and_never_resubscribes(self):
        self.ready()
        self.client.on_disconnect(self.client, None, None, SimpleNamespace(is_failure=True), None)
        self.assertFalse(self.receiver.ready)
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.client.on_message(self.client, None, message())
        self.assertEqual(len(self.client.subscriptions), 1)
        self.assertEqual(self.progress.calls, [])
        self.assertTrue(self.errors)
        self.assertEqual(self.client.network_calls, [])
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))

    def test_invalidation_failure_is_observable_on_disconnect(self):
        self.ready()
        self.gate.invalidate_many.side_effect = RuntimeError('invalidation failed')
        self.client.on_disconnect(self.client, None, None, SimpleNamespace(is_failure=True), None)
        self.assertFalse(self.receiver.ready)
        self.assertTrue(any('invalidation' in str(error) for error in self.errors))
        self.assertIsNotNone(self.receiver.last_error)

    def test_offline_including_retained_and_qos_zero_invalidates_robot_proof(self):
        self.ready()
        self.client.on_message(self.client, None, message(topic='agv/sim-r1/conn', payload=b'offline', qos=0, retain=True))
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))
        self.assertEqual(self.progress.calls, [])
        self.assertTrue(self.receiver.ready)

    def test_online_or_unknown_robot_never_grants_or_invalidates_proof(self):
        self.ready()
        self.client.on_message(self.client, None, message(topic='agv/sim-r1/conn', payload=b'online'))
        self.client.on_message(self.client, None, message(topic='agv/sim-unknown/conn', payload=b'offline'))
        self.gate.invalidate_many.assert_not_called()
        self.assertTrue(self.errors)

    def test_invalid_connection_message_revokes_its_robot_proof(self):
        self.ready()
        self.client.on_message(self.client, None, message(topic='agv/sim-r1/conn', payload=b'bad'))
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))
        self.assertTrue(self.errors)

    def test_mapping_is_copied_and_cannot_be_mutated(self):
        with self.assertRaises(TypeError):
            self.receiver.robot_tasks['sim-r2'] = frozenset(('T2',))

    def test_failed_suback_and_rejected_connection_invalidate_and_close(self):
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.client.on_subscribe(self.client, None, 13, None, None)
        self.assertFalse(self.receiver.ready)
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))
        self.assertTrue(self.errors)

    def test_disconnect_invalidates_all_tasks_before_error_observer_runs(self):
        gate = create_autospec(RecoveryGate, instance=True, spec_set=True)
        object.__setattr__(gate, 'path', 'test-db')
        client, progress = FakeClient(), FakeProgress(gate)
        observed = []
        def on_error(error):
            observed.append(gate.invalidate_many.call_args.args[0])
        receiver = self.module.SimEventReceiver(client, progress, inbox=FakeInbox(progress, {'sim-r1': frozenset(('T1',)), 'sim-r2': frozenset(('T2',))}), recovery_gate=gate,
            robot_tasks={'sim-r1': ('T1',), 'sim-r2': ('T2',)}, simulation_enabled=True, on_error=on_error)
        client.on_disconnect(client, None, None, SimpleNamespace(is_failure=True), None)
        gate.invalidate_many.assert_called_once_with(frozenset(('T1', 'T2')))
        self.assertEqual(observed, [frozenset(('T1', 'T2'))])
        self.assertFalse(receiver.ready)

    def test_invalidation_failure_still_attempts_all_tasks_atomically(self):
        gate = create_autospec(RecoveryGate, instance=True, spec_set=True)
        gate.invalidate_many.side_effect = RuntimeError('invalidation failed')
        object.__setattr__(gate, 'path', 'test-db')
        client, progress = FakeClient(), FakeProgress(gate)
        observations = []
        self.module.SimEventReceiver(client, progress, inbox=FakeInbox(progress, {'sim-r1': frozenset(('T1',)), 'sim-r2': frozenset(('T2',))}), recovery_gate=gate,
            robot_tasks={'sim-r1': ('T1',), 'sim-r2': ('T2',)}, simulation_enabled=True,
            on_error=lambda error: observations.append(gate.invalidate_many.call_args.args[0]))
        client.on_disconnect(client, None, None, SimpleNamespace(is_failure=True), None)
        gate.invalidate_many.assert_called_once_with(frozenset(('T1', 'T2')))
        self.assertTrue(all(calls == frozenset(('T1', 'T2')) for calls in observations))
        self.assertEqual(len(observations), 2)

    def test_message_before_suback_is_stored_but_not_acked_or_processed(self):
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.client.on_message(self.client, None, message())
        self.assertEqual(len(self.inbox.stored), 1)
        self.assertEqual(self.client.acks, [])
        self.assertFalse(self.progress.calls)
        self.client.on_subscribe(self.client, None, 13,
                                 [SimpleNamespace(is_failure=False, value=1)] * 2, None)
        self.assertTrue(self.receiver.ready)
        self.assertEqual(self.client.acks, [(42, 1)])
        self.assertFalse(self.progress.calls)

    def test_offline_before_suback_invalidates_immediately_and_ack_waits(self):
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.client.on_message(self.client, None, message(topic='agv/sim-r1/conn', payload=b'offline'))
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))
        self.assertEqual(self.client.acks, [])
        self.client.on_subscribe(self.client, None, 13,
                                 [SimpleNamespace(is_failure=False, value=1)] * 2, None)
        self.assertEqual(self.client.acks, [(42, 1)])

    def test_before_suback_acks_in_delivery_order_after_success(self):
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        for mid in (3, 8, 5):
            item = message()
            item.mid = mid
            self.client.on_message(self.client, None, item)
        self.assertEqual(self.client.acks, [])
        self.client.on_subscribe(self.client, None, 13,
                                 [SimpleNamespace(is_failure=False, value=1)] * 2, None)
        self.assertEqual(self.client.acks, [(3, 1), (8, 1), (5, 1)])
        self.assertFalse(self.progress.calls)

    def test_before_suback_failure_leaves_stored_without_ack(self):
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.client.on_message(self.client, None, message())
        self.client.on_subscribe(self.client, None, 13,
                                 [SimpleNamespace(is_failure=False, value=0)] * 2, None)
        self.assertEqual(self.client.acks, [])
        self.assertEqual(len(self.inbox.stored), 1)
        self.assertFalse(self.receiver.ready)
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))

    def test_before_suback_overflow_closes_without_any_ack(self):
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        for mid in range(1, self.module.MAX_PENDING_ACKS + 2):
            item = message()
            item.mid = mid
            self.client.on_message(self.client, None, item)
        self.assertFalse(self.receiver.ready)
        self.assertEqual(self.client.acks, [])
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))
        self.assertTrue(self.errors)

    def test_unexpected_suback_closes_and_invalidates(self):
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.client.on_subscribe(self.client, None, 999,
                                 [SimpleNamespace(is_failure=False, value=1)] * 2, None)
        self.assertFalse(self.receiver.ready)
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))
        self.assertTrue(self.errors)

    def test_before_suback_invalid_message_closes_without_ack(self):
        self.client.on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.client.on_message(self.client, None, message(payload=b'not JSON'))
        self.assertFalse(self.receiver.ready)
        self.assertEqual(self.client.acks, [])
        self.gate.invalidate_many.assert_called_once_with(frozenset(('T1',)))
        self.assertTrue(self.errors)


if __name__ == '__main__':
    unittest.main()
