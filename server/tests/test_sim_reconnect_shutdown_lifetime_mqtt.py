"""Reconnect ownership and stop fencing with a real isolated MQTT broker."""
import json
import inspect
import os
from queue import Empty
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server.recovery import _digest_json
from server.segments import plan_segment
from server.sim_runtime import SimRuntime, _DatabaseOwner
from server.tests import test_sim_runtime_command_observations_mqtt as fixture
from server.tests.test_segment_recovery_mqtt import two_segment_map


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class ReconnectShutdownLifetimeMqttTests(unittest.TestCase):
    setUp = fixture.RuntimeCommandObservationMqttTests.setUp
    peer = fixture.RuntimeCommandObservationMqttTests.peer
    wait_for = fixture.RuntimeCommandObservationMqttTests.wait_for

    def test_stop_after_reconnect_pin_before_admission_keeps_single_cleanup_owner(self):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reconnect-admission-race')
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'disconnect')
        old_client, old_receiver = runtime._client, runtime.receiver
        source, first_line = inspect.getsourcelines(SimRuntime.reconnect)
        pin_line = first_line + next(i for i, line in enumerate(source)
                                    if 'with self._runtime_read_operation()' in line)
        admission_line = first_line + next(i for i, line in enumerate(source)
                                          if first_line + i > pin_line and 'with self._lock:' in line)
        pinned, release_pin, close_entered, release_close = (threading.Event() for _ in range(4))
        reconnect_done, competing_close, errors = threading.Event(), threading.Event(), []
        original_close = runtime._close_client
        def close(client):
            if client is old_client and threading.current_thread().name == 'stopper':
                close_entered.set()
                if not release_close.wait(10):
                    raise RuntimeError('test did not release stop cleanup')
            if client is old_client and threading.current_thread().name == 'reconnector':
                competing_close.set()
            return original_close(client)
        def trace(frame, event, _arg):
            if (frame.f_code is SimRuntime.reconnect.__code__ and event == 'line'
                    and frame.f_lineno == admission_line):
                pinned.set()
                if not release_pin.wait(10):
                    raise RuntimeError('test did not release reconnect admission')
            return trace
        def reconnect():
            sys.settrace(trace)
            try:
                runtime.reconnect()
            except RuntimeError:
                pass
            except BaseException as exc:
                errors.append(exc)
            finally:
                sys.settrace(None)
                reconnect_done.set()
        def shutdown():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
        with patch.object(runtime, '_close_client', side_effect=close):
            worker = threading.Thread(target=reconnect, name='reconnector')
            stopper = threading.Thread(target=shutdown, name='stopper')
            worker.start()
            try:
                self.assertTrue(pinned.wait(5))
                stopper.start()
                self.assertTrue(close_entered.wait(5))
                self.assertTrue(runtime._stopped)
                release_pin.set()
                self.assertTrue(reconnect_done.wait(3))
                self.assertFalse(competing_close.is_set(), 'both stop and rejected reconnect closed old client')
                self.assertIs(runtime.receiver, old_receiver)
            finally:
                release_pin.set()
                release_close.set()
                for thread in (worker, stopper):
                    if thread.ident is not None:
                        thread.join(10)
        self.assertFalse(worker.is_alive() or stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(runtime._owner.file)

    def test_stop_between_reconnect_success_and_handoff_closes_client_before_owner_release(self):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reconnect-handoff-race')
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'disconnect')
        source, first_line = inspect.getsourcelines(SimRuntime.reconnect)
        success_line = first_line + next(i for i, line in enumerate(source) if 'success = True' in line)
        finally_line = first_line + next(i for i, line in enumerate(source)
                                        if first_line + i > success_line and 'with self._lock:' in line)
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original_factory = runtime._new_client
        clients = []
        def factory(*args, **kwargs):
            client = original_factory(*args, **kwargs)
            clients.append(client)
            return client
        def trace(frame, event, _arg):
            if (frame.f_code is SimRuntime.reconnect.__code__ and event == 'line'
                    and frame.f_lineno == finally_line and frame.f_locals.get('success') is True):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test did not release reconnect handoff')
            return trace
        def reconnect():
            sys.settrace(trace)
            try:
                runtime.reconnect()
            except BaseException as exc:
                errors.append(exc)
            finally:
                sys.settrace(None)
        def shutdown():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                stopped.set()
        with patch.object(runtime, '_new_client', side_effect=factory):
            worker = threading.Thread(target=reconnect)
            stopper = threading.Thread(target=shutdown)
            worker.start()
            try:
                self.assertTrue(entered.wait(8), 'reconnect did not reach success handoff')
                stopper.start()
                self.wait_for(lambda: runtime._stopped, 'stop before handoff')
                self.assertFalse(stopped.is_set())
                self.assertIsNotNone(runtime._owner.file)
            finally:
                release.set()
                worker.join(10)
                if stopper.ident is not None:
                    stopper.join(10)
        try:
            self.assertFalse(worker.is_alive() or stopper.is_alive())
            self.assertTrue(stopped.is_set())
            self.assertIsNone(runtime._owner.file)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RuntimeError)
            self.assertEqual(len(clients), 1)
            self.assertFalse(clients[0].is_connected(), 'provisional client leaked past owner release')
            thread = clients[0]._thread
            self.assertTrue(thread is None or not thread.is_alive(), 'MQTT network thread leaked')
        finally:
            if clients:
                clients[0].disconnect()
                clients[0].loop_stop()

    def test_stop_before_success_handoff_preserves_owner_if_client_cleanup_fails(self):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='handoff-cleanup-failure')
        runtime.start()
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'disconnect')
        source, first_line = inspect.getsourcelines(SimRuntime.reconnect)
        success_line = first_line + next(i for i, line in enumerate(source) if 'success = True' in line)
        finally_line = first_line + next(i for i, line in enumerate(source)
                                        if first_line + i > success_line and 'with self._lock:' in line)
        entered, release = threading.Event(), threading.Event()
        reconnect_errors, stop_errors, clients = [], [], []
        original_factory, original_close = runtime._new_client, runtime._close_client
        def factory(*args, **kwargs):
            client = original_factory(*args, **kwargs)
            clients.append(client)
            return client
        def close(client):
            original_close(client)
            if clients and client is clients[0]:
                raise RuntimeError('injected cleanup failure after disconnect')
        def trace(frame, event, _arg):
            if (frame.f_code is SimRuntime.reconnect.__code__ and event == 'line'
                    and frame.f_lineno == finally_line and frame.f_locals.get('success') is True):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test did not release reconnect handoff')
            return trace
        def reconnect():
            sys.settrace(trace)
            try:
                runtime.reconnect()
            except BaseException as exc:
                reconnect_errors.append(exc)
            finally:
                sys.settrace(None)
        def shutdown():
            try:
                runtime.stop()
            except BaseException as exc:
                stop_errors.append(exc)
        with patch.object(runtime, '_new_client', side_effect=factory), \
             patch.object(runtime, '_close_client', side_effect=close):
            worker, stopper = threading.Thread(target=reconnect), threading.Thread(target=shutdown)
            worker.start()
            try:
                self.assertTrue(entered.wait(8))
                stopper.start()
                self.wait_for(lambda: runtime._stopped, 'stop before handoff')
            finally:
                release.set()
                worker.join(10)
                if stopper.ident is not None:
                    stopper.join(10)
        try:
            self.assertFalse(worker.is_alive() or stopper.is_alive())
            self.assertEqual(len(reconnect_errors), 1)
            self.assertEqual(len(stop_errors), 1)
            self.assertTrue(runtime._reconnect_cleanup_failed)
            self.assertIsNotNone(runtime._owner.file)
            with self.assertRaises(RuntimeError):
                _DatabaseOwner(self.path).acquire()
            self.assertFalse(clients[0].is_connected())
        finally:
            if clients:
                clients[0].disconnect()
                clients[0].loop_stop()
            runtime._owner.release()

    def test_stop_before_handoff_keeps_owner_if_gate_invalidation_fails(self):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='handoff-gate-failure')
        runtime.start()
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'disconnect')
        source, first_line = inspect.getsourcelines(SimRuntime.reconnect)
        success_line = first_line + next(i for i, line in enumerate(source) if 'success = True' in line)
        finally_line = first_line + next(i for i, line in enumerate(source)
                                        if first_line + i > success_line and 'with self._lock:' in line)
        entered, release = threading.Event(), threading.Event()
        reconnect_errors, stop_errors = [], []
        def trace(frame, event, _arg):
            if (frame.f_code is SimRuntime.reconnect.__code__ and event == 'line'
                    and frame.f_lineno == finally_line and frame.f_locals.get('success') is True):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test did not release handoff')
            return trace
        def reconnect():
            sys.settrace(trace)
            try:
                runtime.reconnect()
            except BaseException as exc:
                reconnect_errors.append(exc)
            finally:
                sys.settrace(None)
        def shutdown():
            try:
                runtime.stop()
            except BaseException as exc:
                stop_errors.append(exc)
        worker, stopper = threading.Thread(target=reconnect), threading.Thread(target=shutdown)
        worker.start()
        try:
            self.assertTrue(entered.wait(8))
            with patch.object(runtime.gate, 'invalidate_many', side_effect=RuntimeError('injected gate failure')):
                stopper.start()
                try:
                    self.wait_for(lambda: runtime._stopped, 'stop before handoff')
                finally:
                    release.set()
                    worker.join(10)
                    if stopper.ident is not None:
                        stopper.join(10)
            self.assertFalse(worker.is_alive() or stopper.is_alive())
            self.assertEqual(len(reconnect_errors), 1)
            self.assertEqual(len(stop_errors), 1)
            self.assertTrue(runtime._reconnect_cleanup_failed)
            self.assertIsNotNone(runtime._owner.file)
            self.assertFalse(runtime._client.is_connected())
        finally:
            release.set()
            worker.join(10)
            if stopper.ident is not None:
                stopper.join(10)
            runtime._owner.release()

    def test_stop_fences_stalled_connect_preserves_owner_and_cancels_attempt(self):
        robot, orders = self.peer()
        data = two_segment_map()
        runtime = SimRuntime(self.path, data, {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reconnect-stop-fence')
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        runtime.prepare('T1', 'N5', [{'robot_id': 'sim-r1', 'node': 'N1', 'available': True}],
                        {'type': 'load'})
        runtime.publish('T1')
        order = json.loads(orders.get(timeout=5))
        challenge = runtime.begin('T1', 'boot-1')['challenge']
        runtime.complete('T1', 'boot-1', challenge, 'sim-r1', 'T1', 1,
                         'integration@v1', 'N3', _digest_json(order), True)
        occupancy = runtime.store.list_occupancy()
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'disconnect')
        self.assertIn('T1', runtime.gate._blocked)
        old = set(plan_segment(data, ['N1', 'N2', 'N3', 'N4', 'N5']).resources)
        new = set(plan_segment(data, ['N3', 'N4', 'N5']).resources)
        event = {'type': 'rear_clear', 'event_id': 'stop-during-connect', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'integration@v1', 'stopped_at': 'N3',
                 'cleared_resources': [list(item) for item in sorted(old - new)]}
        entered, release, stopped, second_stopped, duplicate_done = (threading.Event() for _ in range(5))
        overlapped_close, loop_started = threading.Event(), threading.Event()
        errors, reconnect_errors, duplicate_errors, created = [], [], [], []
        original_factory = runtime._new_client
        def factory(*args, **kwargs):
            client = original_factory(*args, **kwargs)
            created.append(client)
            connect, disconnect, loop_start = client.connect, client.disconnect, client.loop_start
            def delayed_connect(*a, **k):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test did not release connection')
                return connect(*a, **k)
            def checked_disconnect(*a, **k):
                if not release.is_set():
                    overlapped_close.set()
                return disconnect(*a, **k)
            def checked_loop_start(*a, **k):
                loop_started.set()
                return loop_start(*a, **k)
            client.connect, client.disconnect, client.loop_start = delayed_connect, checked_disconnect, checked_loop_start
            return client
        def reconnect():
            try:
                runtime.reconnect()
            except BaseException as exc:
                reconnect_errors.append(exc)
        def shutdown(done):
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        def duplicate():
            try:
                runtime.reconnect()
            except BaseException as exc:
                duplicate_errors.append(exc)
            finally:
                duplicate_done.set()
        with patch.object(runtime, '_new_client', side_effect=factory):
            worker = threading.Thread(target=reconnect)
            stopper = threading.Thread(target=shutdown, args=(stopped,))
            second_stopper = threading.Thread(target=shutdown, args=(second_stopped,))
            duplicate_thread = threading.Thread(target=duplicate)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                duplicate_thread.start()
                self.assertTrue(duplicate_done.wait(1), 'duplicate reconnect waited on network I/O')
                self.assertEqual(len(duplicate_errors), 1)
                self.assertIsInstance(duplicate_errors[0], RuntimeError)
                stopper.start()
                self.wait_for(lambda: runtime._stopped, 'immediate stop fence')
                second_stopper.start()
                self.assertFalse(runtime.status()['ready'])
                self.assertTrue(runtime.workflow._stopping)
                self.assertFalse(stopped.is_set())
                self.assertFalse(second_stopped.is_set())
                self.assertFalse(overlapped_close.is_set())
                self.assertIsNotNone(runtime._owner.file)
                with self.assertRaises(RuntimeError):
                    _DatabaseOwner(self.path).acquire()
                info = robot.publish('agv/sim-r1/event', json.dumps(event), qos=1)
                info.wait_for_publish(timeout=5)
                self.assertTrue(info.is_published())
            finally:
                release.set()
                worker.join(10)
                for thread in (stopper, second_stopper, duplicate_thread):
                    if thread.ident is not None:
                        thread.join(10)
        self.assertFalse(worker.is_alive() or stopper.is_alive() or second_stopper.is_alive()
                         or duplicate_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(reconnect_errors), 1)
        self.assertIsInstance(reconnect_errors[0], RuntimeError)
        self.assertTrue(stopped.is_set())
        self.assertTrue(second_stopped.is_set())
        self.assertFalse(overlapped_close.is_set())
        self.assertFalse(loop_started.is_set())
        self.assertIsNone(runtime._owner.file)
        self.assertFalse(created[0].is_connected())
        self.assertIn('T1', runtime.gate._blocked)
        self.assertEqual(runtime.store.list_occupancy(), occupancy)
        with self.assertRaises(Empty):
            orders.get(timeout=.3)

    def test_stop_during_loop_start_fences_provisional_receiver(self):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reconnect-loop-fence')
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'disconnect')
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        overlap, errors, reconnect_errors = threading.Event(), [], []
        original_factory = runtime._new_client
        def factory(*args, **kwargs):
            client = original_factory(*args, **kwargs)
            start, disconnect = client.loop_start, client.disconnect
            def delayed_start(*a, **k):
                result = start(*a, **k)
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test did not release loop start')
                return result
            def checked_disconnect(*a, **k):
                if not release.is_set():
                    overlap.set()
                return disconnect(*a, **k)
            client.loop_start, client.disconnect = delayed_start, checked_disconnect
            return client
        def reconnect():
            try:
                runtime.reconnect()
            except BaseException as exc:
                reconnect_errors.append(exc)
        def shutdown():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                stopped.set()
        with patch.object(runtime, '_new_client', side_effect=factory):
            worker, stopper = threading.Thread(target=reconnect), threading.Thread(target=shutdown)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.wait_for(lambda: runtime.receiver.ready, 'provisional SUBACK')
                self.assertFalse(runtime.status()['ready'])
                with self.assertRaises(RuntimeError):
                    runtime.create_task('T1')
                stopper.start()
                for _ in range(100):
                    if runtime._stopped:
                        break
                    stopped.wait(.01)
                self.assertTrue(runtime._stopped, 'stop fence waited on loop start')
                self.assertTrue(runtime.workflow._stopping)
                self.assertFalse(runtime.status()['ready'])
                self.assertFalse(stopped.is_set())
                self.assertFalse(overlap.is_set())
                self.assertIsNotNone(runtime._owner.file)
                message = SimpleNamespace(topic='agv/sim-r1/event', qos=1, retain=False, mid=17,
                    payload=json.dumps({'type': 'rear_clear', 'event_id': 'late-provisional',
                        'robot_session': 'boot-1', 'order_id': 'T1', 'seq': 1,
                        'map_id': 'integration@v1', 'stopped_at': 'N3',
                        'cleared_resources': []}).encode())
                with patch.object(runtime.inbox, 'store') as store:
                    runtime._client.on_message(runtime._client, None, message)
                    store.assert_not_called()
            finally:
                release.set()
                worker.join(10)
                if stopper.ident is not None:
                    stopper.join(10)
        self.assertFalse(worker.is_alive() or stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(reconnect_errors), 1)
        self.assertIsInstance(reconnect_errors[0], RuntimeError)
        self.assertFalse(overlap.is_set())
        self.assertIsNone(runtime._owner.file)

    def test_network_callback_cannot_release_its_own_owner(self):
        robot, _orders = self.peer()
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='network-reentrant-stop')
        runtime.start()
        self.cleanup.callback(runtime.stop)
        done, errors = threading.Event(), []
        def callback(_error):
            try:
                runtime.stop()
            except RuntimeError:
                pass
            except BaseException as exc:
                errors.append(exc)
            else:
                errors.append(AssertionError('network callback released its own owner'))
            finally:
                done.set()
        runtime.receiver.on_error = callback
        info = robot.publish('agv/sim-r1/event', b'{invalid', qos=1)
        info.wait_for_publish(timeout=5)
        self.assertTrue(done.wait(5))
        self.assertEqual(errors, [])
        self.assertFalse(runtime._stopped)
        self.assertIsNotNone(runtime._owner.file)
        runtime.receiver.on_error = None

    def test_failed_client_cleanup_blocks_reconnect_and_owner_release(self):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reconnect-cleanup-failure')
        runtime.start()
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'disconnect')
        original_factory = runtime._new_client
        clients = []
        def factory(*args, **kwargs):
            client = original_factory(*args, **kwargs)
            clients.append(client)
            def bad_connect(*_args, **_kwargs):
                raise RuntimeError('injected connect failure')
            def bad_disconnect(*_args, **_kwargs):
                raise RuntimeError('injected cleanup failure')
            client.connect, client.disconnect = bad_connect, bad_disconnect
            return client
        try:
            with patch.object(runtime, '_new_client', side_effect=factory):
                with self.assertRaises(RuntimeError):
                    runtime.reconnect()
            self.assertTrue(runtime._reconnect_cleanup_failed)
            self.assertFalse(runtime.status()['ready'])
            with patch.object(runtime, '_new_client', side_effect=AssertionError('must not reconnect')):
                with self.assertRaises(RuntimeError):
                    runtime.reconnect()
            with self.assertRaises(RuntimeError):
                runtime.create_task('T1')
            with self.assertRaises(RuntimeError):
                runtime.stop()
            self.assertIsNotNone(runtime._owner.file)
            with self.assertRaises(RuntimeError):
                _DatabaseOwner(self.path).acquire()
        finally:
            # This test client never connected or started a network thread.
            self.assertEqual(len(clients), 1)
            self.assertFalse(clients[0].is_connected())
            runtime._owner.release()

    def test_reconnect_rejects_missing_local_gate_block_before_new_client(self):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reconnect-missing-block')
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'disconnect')
        with runtime.gate._state_lock:
            runtime.gate._blocked.clear()
        with patch.object(runtime, '_new_client', side_effect=AssertionError('new client must not start')):
            with self.assertRaises(RuntimeError):
                runtime.reconnect()

    def test_opt_in_observer_and_reference_survive_reconnect(self):
        runtime = SimRuntime(self.path, self.map_data, {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reconnect-opt-in', field_simulation=True,
                             command_observations=True, investigation_notes=True)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        observer = runtime.command_observation_receiver
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'disconnect')
        result = runtime.reconnect()
        self.assertTrue(result['ready'])
        self.assertTrue(result['command_observation_ready'])
        self.assertIs(runtime.command_observation_receiver, observer)
        self.assertIn('T1', runtime.gate._blocked)
        note = runtime.record_investigation_note('T1', 'f' * 32, 'connection')
        self.assertEqual(note['note']['category'], 'connection')

    def test_previous_workflow_cleanup_failure_blocks_retry_and_keeps_owner(self):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='old-workflow-failure')
        runtime.start()
        old_workflow = runtime.workflow
        runtime._client.disconnect()
        self.wait_for(lambda: runtime.receiver._closed, 'disconnect')
        original_stop = old_workflow.stop
        def fail_after_stop():
            original_stop()
            raise RuntimeError('injected workflow cleanup failure')
        try:
            with patch.object(old_workflow, 'stop', side_effect=fail_after_stop):
                with self.assertRaises(RuntimeError):
                    runtime.reconnect()
            self.assertTrue(runtime._reconnect_cleanup_failed)
            self.assertFalse(runtime.status()['ready'])
            with self.assertRaises(RuntimeError):
                runtime.reconnect()
            with self.assertRaises(RuntimeError):
                runtime.stop()
            self.assertIsNotNone(runtime._owner.file)
        finally:
            runtime._owner.release()


if __name__ == '__main__':
    unittest.main()
