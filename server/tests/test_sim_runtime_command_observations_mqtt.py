"""Opt-in response observations on the actual isolated simulation runtime."""
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
from queue import Queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import paho.mqtt.client as mqtt

from server.sim_runtime import SimRuntime
from server.tests.test_occurrence_allocation_lab import laboratory_map
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port,
                                                       stop, stop_publisher, wait_for_owned_broker)
from server.tools.twin_multi_test import isolated_environment


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class RuntimeCommandObservationMqttTests(unittest.TestCase):
    def setUp(self):
        self.cleanup = ExitStack()
        self.addCleanup(self.cleanup.close)
        self.root = Path(self.cleanup.enter_context(tempfile.TemporaryDirectory(prefix='runtime-observation-')))
        self.port = free_port()
        broker_env = isolated_environment(os.environ)
        broker_env['PYTHONPATH'] = str(Path(os.environ['AMQTT_TEST_PATH']).resolve())
        self.cleanup.enter_context(patch.dict(os.environ, isolated_environment(os.environ), clear=True))
        ready = str(self.root / 'ready')
        log = self.cleanup.enter_context((self.root / 'broker.log').open('w'))
        broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', BROKER,
                                   str(self.port), ready], env=broker_env, stdout=log, stderr=log,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.cleanup.callback(stop, broker)
        wait_for_owned_broker(self, broker, ready)
        self.path = self.root / 'server.sqlite3'
        self.map_data = laboratory_map()

    def runtime(self, **options):
        return SimRuntime(self.path, self.map_data, {'sim-r1': {'T1': 'boot-1'}},
                          port=self.port, client_id='runtime-observation-test',
                          field_simulation=True, **options)

    def peer(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='runtime-observation-peer',
                             clean_session=True, reconnect_on_failure=False)
        orders, ready = Queue(), threading.Event()
        def connect(c, _u, _f, reason, _p):
            if not reason.is_failure:
                c.subscribe('agv/sim-r1/order', qos=1)
        def subscribed(_c, _u, _mid, reasons, _p):
            if len(reasons) == 1 and not reasons[0].is_failure and reasons[0].value == 1:
                ready.set()
        client.on_connect, client.on_subscribe = connect, subscribed
        client.on_message = lambda _c, _u, message: orders.put(bytes(message.payload))
        connect_publisher(client, self.port)
        client.loop_start()
        self.cleanup.callback(stop_publisher, client)
        self.assertTrue(ready.wait(10))
        return client, orders

    def wait_for(self, predicate, label):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail('timed out waiting for ' + label)

    def prepare(self, runtime, orders):
        runtime.create_task('T1')
        runtime.prepare_field('T1', 'D', [{'robot_id': 'sim-r1', 'node': 'A',
                                          'incoming_edge': 'E0', 'available': True}], {'type': 'none'})
        occupancy = runtime.store.list_occupancy()
        runtime.publish('T1')
        wire = orders.get(timeout=10)
        order = json.loads(wire)
        response = {'event_id': 'response-1', 'robot_session': 'boot-1',
                    'order_id': order['order_id'], 'seq': order['seq'], 'map_id': order['map_id'],
                    'order_digest': hashlib.sha256(wire).hexdigest(), 'outcome': 'accepted'}
        return response, occupancy

    def test_later_startup_failure_cleans_already_started_observer(self):
        runtime = self.runtime(command_observations=True)
        with patch.object(runtime, '_new_client', side_effect=RuntimeError('injected after observer')):
            with self.assertRaisesRegex(RuntimeError, 'injected after observer'):
                runtime.start()
        self.assertIsNotNone(runtime.command_observation_receiver)
        self.assertTrue(runtime.command_observation_receiver._closed)
        self.assertFalse(runtime.command_observation_receiver.ready)
        self.assertFalse(runtime.command_observation_receiver._client.is_connected())
        self.assertIsNone(runtime._owner.file)
        self.assertFalse(runtime.status()['ready'])
        self.assertFalse(runtime.status()['command_observation_ready'])

    def test_handoff_store_remains_bound_across_failed_opt_in_restart(self):
        from server.sim_handoff_store import SimHandoffStore
        _peer, orders = self.peer()
        store = SimHandoffStore.create_new(self.path, simulation_enabled=True,
                                           idle_positions={'sim-r1': 'A'})
        runtime = self.runtime(command_observations=True, simulation_store=store,
                               field_initial_positions={'sim-r1': 'A'})
        runtime.start()
        self.cleanup.callback(runtime.stop)
        _response, occupied = self.prepare(runtime, orders)
        self.assertEqual(store.get_order('T1')['state'], 'acked')
        identity = store.creation_id
        runtime.stop()
        reopened_store = SimHandoffStore.open_existing(self.path, expected_creation_id=identity,
            expected_idle_enabled=True, simulation_enabled=True)
        old_epoch = self.runtime(command_observations=True, simulation_store=reopened_store,
                                 field_initial_positions={'sim-r1': 'A'})
        with self.assertRaises(ValueError):
            old_epoch.start()
        self.assertIsNone(old_epoch._owner.file)
        reopened_store.validate_bound(self.path, check_schema=True)
        self.assertEqual(reopened_store.list_occupancy(), occupied)
        resumed = self.runtime(simulation_store=reopened_store, field_initial_positions={'sim-r1': 'A'})
        resumed.start()
        self.cleanup.callback(resumed.stop)
        self.assertEqual(resumed.store.list_occupancy(), occupied)
        self.assertIn('T1', resumed.gate._blocked)

    def test_response_disconnect_and_restart_preserve_existing_order_and_reservations(self):
        peer, orders = self.peer()
        runtime = self.runtime(command_observations=True)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        self.assertTrue(runtime.status()['ready'])
        self.assertTrue(runtime.status()['command_observation_ready'])
        response, before = self.prepare(runtime, orders)
        publish = peer.publish('sim-observation/sim-r1/order-response', json.dumps(response), qos=1)
        publish.wait_for_publish(timeout=5)
        self.assertTrue(publish.is_published())
        self.wait_for(lambda: runtime.command_observation_receiver.last_result is not None, 'response recording')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT robot_id,event_id,outcome FROM sim_command_observations').fetchall(),
                             [('sim-r1', 'response-1', 'accepted')])
        self.assertEqual(runtime.store.list_occupancy(), before)
        self.assertEqual(runtime.store.get_order('T1')['state'], 'acked')

        # A conflicting response closes only the opt-in observation receiver.
        response['outcome'] = 'rejected'
        publish = peer.publish('sim-observation/sim-r1/order-response', json.dumps(response), qos=1)
        publish.wait_for_publish(timeout=5)
        self.wait_for(lambda: not runtime.status()['command_observation_ready'], 'observer failure')
        self.assertTrue(runtime.status()['ready'])
        self.assertEqual(runtime.store.list_occupancy(), before)
        runtime.stop()
        self.assertFalse(runtime.status()['command_observation_ready'])

        # A new gate cannot silently attach the previous observation epoch.
        reopened = self.runtime(command_observations=True)
        with self.assertRaises(ValueError):
            reopened.start()
        self.assertIsNone(reopened._owner.file)
        self.assertEqual(runtime.store.list_occupancy(), before)
        self.assertEqual(runtime.store.get_order('T1')['state'], 'acked')
        # Default mode remains available, but existing physical-like work is
        # still blocked pending the existing simulation recovery protocol.
        fallback = self.runtime()
        fallback.start()
        self.cleanup.callback(fallback.stop)
        self.assertNotIn('command_observation_ready', fallback.status())
        self.assertIn('T1', fallback.gate._blocked)
        self.assertEqual(fallback.store.list_occupancy(), before)

    def test_stop_waits_for_inflight_observation_without_deadlock(self):
        peer, orders = self.peer()
        runtime = self.runtime(command_observations=True)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        response, before = self.prepare(runtime, orders)
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = runtime.command_observations.record
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test failed to release recording')
            return original(*args, **kwargs)
        def shutdown():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        with patch.object(runtime.command_observations, 'record', side_effect=delayed):
            peer.publish('sim-observation/sim-r1/order-response', json.dumps(response), qos=1)
            self.assertTrue(entered.wait(10))
            worker = threading.Thread(target=shutdown)
            worker.start()
            try:
                self.assertFalse(done.wait(.15))
            finally:
                release.set()
                worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(done.is_set())
        self.assertEqual(runtime.store.list_occupancy(), before)
        self.assertFalse(runtime.status()['ready'])
        self.assertFalse(runtime.status()['command_observation_ready'])

    def test_slow_observation_status_does_not_hold_runtime_lock(self):
        peer, orders = self.peer()
        runtime = self.runtime(command_observations=True)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        response, _before = self.prepare(runtime, orders)
        entered, release, status_waiting, read_done = (threading.Event() for _ in range(4))
        errors = []
        original_record = runtime.command_observations.record
        receiver_type = type(runtime.command_observation_receiver)
        original_ready = receiver_type.ready.fget

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release observation recording')
            return original_record(*args, **kwargs)

        def observed_ready(receiver):
            if threading.current_thread().name == 'status-reader':
                status_waiting.set()
            return original_ready(receiver)

        def read_status():
            try:
                runtime.status()
            except BaseException as exc:
                errors.append(exc)

        def read_task():
            try:
                runtime.task_status('T1')
                read_done.set()
            except BaseException as exc:
                errors.append(exc)

        with patch.object(runtime.command_observations, 'record', side_effect=delayed), \
                patch.object(receiver_type, 'ready', property(observed_ready)):
            peer.publish('sim-observation/sim-r1/order-response', json.dumps(response), qos=1)
            self.assertTrue(entered.wait(10))
            status_thread = threading.Thread(target=read_status, name='status-reader')
            task_thread = threading.Thread(target=read_task)
            status_thread.start()
            try:
                self.assertTrue(status_waiting.wait(5))
                task_thread.start()
                self.assertTrue(read_done.wait(2), 'status must not block unrelated runtime operations')
            finally:
                release.set()
                status_thread.join(10)
                if task_thread.ident is not None:
                    task_thread.join(10)
            self.assertFalse(status_thread.is_alive())
            self.assertFalse(task_thread.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
