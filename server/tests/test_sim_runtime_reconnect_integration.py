"""Receiver reconnection against a dedicated loopback broker, never real AGVs."""
from contextlib import ExitStack
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from server.sim_runtime import SimRuntime
from server.sim_order_authorization import SimOrderAuthorizer
from server.tests.test_segment_recovery_mqtt import two_segment_map
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class SimRuntimeReconnectTest(unittest.TestCase):
    def test_individual_robot_disconnect_blocks_only_its_tasks(self):
        port = free_port()
        env = isolated_environment(os.environ)
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='runtime-robot-fault-') as folder, ExitStack() as cleanup, \
                open(os.devnull, 'w') as log, patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            ready = str(Path(folder) / 'broker.ready')
            process = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                                        BROKER, str(port), ready], env=env, stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, process)
            wait_for_owned_broker(self, process, ready)
            runtime = SimRuntime(Path(folder) / 'server.sqlite3', two_segment_map(),
                                 {'sim-r1': {'T1': 'boot-1', 'T2': 'boot-1'},
                                  'sim-r2': {'T3': 'boot-2'}}, port=port, client_id='robot-fault-test')
            runtime.start()
            cleanup.callback(runtime.stop)
            runtime.invalidate_robot('sim-r1')
            self.assertEqual(runtime.gate._blocked, {'T1', 'T2'})
            self.assertTrue(runtime.status()['ready'])
            with self.assertRaises(ValueError):
                runtime.invalidate_robot('sim-r3')
            self.assertEqual(runtime.gate._blocked, {'T1', 'T2'})
            runtime.create_task('T1', reset_unallocated=True)
            self.assertNotIn('T1', runtime.gate._blocked)
            self.assertIn('T2', runtime.gate._blocked)
            runtime.invalidate_robot('sim-r1')
            with self.assertRaises(Exception):
                runtime.create_task('T1', reset_unallocated=True)
            self.assertIn('T1', runtime.gate._blocked)
            runtime.create_task('T2')
            self.assertIn('T2', runtime.gate._blocked)
            with self.assertRaises(Exception):
                runtime.create_task('T2', reset_unallocated=True)
            self.assertIn('T2', runtime.gate._blocked)

    def test_moving_resume_requires_matching_live_checkpoint_and_retains_gate_block(self):
        port = free_port()
        env = isolated_environment(os.environ)
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='runtime-motion-reconnect-') as folder, ExitStack() as cleanup, \
                open(os.devnull, 'w') as log, patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            def broker(name):
                ready = str(Path(folder) / (name + '.ready'))
                process = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                                            BROKER, str(port), ready], env=env, stdout=log, stderr=log,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                cleanup.callback(stop, process)
                wait_for_owned_broker(self, process, ready)
                return process
            first = broker('first')
            sim_map = two_segment_map()
            runtime = SimRuntime(Path(folder) / 'server.sqlite3', sim_map,
                                 {'sim-r1': {'T1': 'boot-1'}}, port=port, client_id='resume-test')
            runtime.start()
            cleanup.callback(runtime.stop)
            inbox = SimOrderInbox(Path(folder) / 'robot.sqlite3', 'sim-r1', 'integration@v1')
            inbox.initialize()
            authorizer = SimOrderAuthorizer(runtime, robot_id='sim-r1')
            robot = SimMqttOrderRobot(SimOrderExecutor(inbox, sim_map, 'N1'), port,
                                      authorization_guard=authorizer.guard)
            robot.start()
            cleanup.callback(robot.stop)
            runtime.create_task('T1')
            runtime.prepare('T1', 'N5', [{'robot_id': 'sim-r1', 'node': 'N1', 'available': True}],
                            {'type': 'load'})
            runtime.publish('T1')
            deadline = time.monotonic() + 5
            while robot._queue.empty() and time.monotonic() < deadline:
                time.sleep(.01)
            state = robot.tick(.005)
            self.assertEqual(state['status'], 'moving')
            self.assertEqual(state['pose']['x'], 15.0)
            stop(first)
            deadline = time.monotonic() + 10
            while (robot.ready or runtime.status()['ready']) and time.monotonic() < deadline:
                time.sleep(.02)
            checkpoint = robot.recovery_checkpoint()
            before = runtime.store.list_occupancy()
            broker('second')
            runtime.reconnect()
            robot.reconnect(authorization_guard=authorizer.guard)
            with self.assertRaises(Exception):
                runtime.reconcile_moving('sim-r1', 'T1', 'boot-1', robot, ('changed',))
            self.assertEqual(runtime.store.list_occupancy(), before)
            original_guard = robot._authorization_guard
            robot._authorization_guard = lambda _payload: None
            with self.assertRaises(ValueError):
                runtime.reconcile_moving('sim-r1', 'T1', 'boot-1', robot, checkpoint)
            robot._authorization_guard = original_guard
            runtime.reconcile_moving('sim-r1', 'T1', 'boot-1', robot, checkpoint)
            self.assertIn('T1', runtime.gate._blocked)
            self.assertEqual(robot.tick(.005)['pose']['x'], 20.0)
            self.assertEqual(runtime.store.list_occupancy(), before)
            reached = robot.tick(.01)
            self.assertEqual((reached['status'], reached['current_node']), ('stopped', 'N3'))
            challenge = runtime.begin('T1', 'boot-1')['challenge']
            self.assertNotIn('T1', runtime.gate._moving_permits)
            receipt = reached['receipt']
            runtime.complete('T1', 'boot-1', challenge, 'sim-r1', 'T1', receipt['seq'],
                             'integration@v1', 'N3', receipt['digest'], True)
            self.assertNotIn('T1', runtime.gate._blocked)
            runtime.gate.invalidate('T1')
            payload = json.dumps(robot.executor.snapshot()['order']).encode()
            with self.assertRaises(ValueError):
                with authorizer.guard(payload):
                    pass

    def test_reconnect_keeps_gate_and_blocked_tasks_and_ignores_old_disconnect(self):
        port = free_port()
        env = isolated_environment(os.environ)
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='runtime-reconnect-') as folder, ExitStack() as cleanup, \
                open(os.devnull, 'w') as log, patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            def broker(name):
                ready = str(Path(folder) / (name + '.ready'))
                process = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                                            BROKER, str(port), ready], env=env, stdout=log, stderr=log,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                cleanup.callback(stop, process)
                wait_for_owned_broker(self, process, ready)
                return process
            first = broker('first')
            runtime = SimRuntime(Path(folder) / 'server.sqlite3', two_segment_map(),
                                 {'sim-r1': {'T1': 'boot-1'}}, port=port, client_id='reconnect-test')
            runtime.start()
            cleanup.callback(runtime.stop)
            gate, store, old_receiver = runtime.gate, runtime.store, runtime.receiver
            epoch = gate._epoch
            stop(first)
            deadline = time.monotonic() + 10
            while runtime.status()['ready'] and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(runtime.status()['ready'])
            self.assertIn('T1', gate._blocked)
            with self.assertRaises(Exception):
                runtime.reconnect()
            self.assertFalse(runtime.status()['ready'])
            self.assertIn('T1', gate._blocked)
            broker('second')
            runtime.reconnect()
            self.assertTrue(runtime.status()['ready'])
            self.assertIs(runtime.gate, gate)
            self.assertIs(runtime.store, store)
            self.assertEqual(runtime.gate._epoch, epoch)
            self.assertIn('T1', gate._blocked)
            self.assertIsNot(runtime.receiver, old_receiver)
            old_receiver._on_disconnect(old_receiver.client, None, None, None, None)
            self.assertTrue(runtime.status()['ready'])
            self.assertIn('T1', gate._blocked)
