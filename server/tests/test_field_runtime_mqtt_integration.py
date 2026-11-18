"""Real FastAPI/SimRuntime/MQTT integration with two isolated virtual AGVs.

The map, waiting points, incoming directions and recovery proofs are synthetic.
No real robot or shared broker is contacted.
"""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from server.sim_api import router
from server.sim_runtime import SimRuntime
from server.sim_order_authorization import SimOrderAuthorizer
from server.route_occurrence import occurrence_clearance
from server.tests.test_occurrence_allocation_lab import laboratory_map
from server.tests.test_sim_api import request
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class FieldRuntimeMqttIntegrationTests(unittest.TestCase):
    def test_repeated_route_recovers_after_broker_disconnect(self):
        port = free_port()
        broker_env = isolated_environment(os.environ)
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='field-runtime-reconnect-') as folder, ExitStack() as cleanup, \
                open(os.devnull, 'w') as log, patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            def broker(name):
                ready = str(Path(folder) / (name + '.ready'))
                proc = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                                         BROKER, str(port), ready], env=broker_env, stdout=log, stderr=log,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                cleanup.callback(stop, proc)
                wait_for_owned_broker(self, proc, ready)
                return proc

            def wait(predicate, label):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(.02)
                self.fail('timed out: ' + label)

            first = broker('first')
            data = laboratory_map()
            runtime = SimRuntime(Path(folder) / 'server.sqlite3', data,
                                 {'sim-r1': {'T1': 'boot-1'}}, port=port,
                                 client_id='field-motion-reconnect', field_simulation=True)
            runtime.start()
            cleanup.callback(runtime.stop)
            inbox = SimOrderInbox(Path(folder) / 'robot.sqlite3', 'sim-r1', 'lab@v1')
            inbox.initialize()
            authorizer = SimOrderAuthorizer(runtime, robot_id='sim-r1')
            robot = SimMqttOrderRobot(SimOrderExecutor(inbox, data, 'A', experimental_field_map=True,
                                                       field_incoming_edge='E0'), port,
                                      authorization_guard=authorizer.guard)
            robot.start()
            cleanup.callback(robot.stop)
            runtime.create_task('T1')
            route = runtime.prepare_field('T1', 'D', [{'robot_id': 'sim-r1', 'node': 'A',
                'incoming_edge': 'E0', 'available': True}], {'type': 'none'})
            self.assertEqual(route['nodes'], ['A', 'B', 'C', 'B', 'D'])
            runtime.publish('T1')
            wait(lambda: not robot._queue.empty(), 'first order')

            def prove_and_report(state, start_index, event_id):
                receipt = state['receipt']
                challenge = runtime.begin('T1', 'boot-1')['challenge']
                runtime.complete('T1', 'boot-1', challenge, 'sim-r1', 'T1', receipt['seq'],
                                 'lab@v1', state['current_node'], receipt['digest'], True)
                robot.publish_event({'type': 'rear_clear', 'event_id': event_id,
                    'robot_session': 'boot-1', 'order_id': 'T1', 'seq': state['order']['seq'],
                    'map_id': 'lab@v1', 'stopped_at': state['current_node'],
                    'cleared_resources': [list(x) for x in occurrence_clearance(
                        data, state['order']['nodes'], start_index)]})

            first_stop = robot.tick(1)
            self.assertEqual((first_stop['current_node'], first_stop['visit_index']), ('B', 1))
            prove_and_report(first_stop, 0, 'first')
            wait(lambda: not robot._queue.empty(), 'repeated segment')
            moving = robot.tick(.0005)
            self.assertEqual(moving['status'], 'moving')
            stop(first)
            wait(lambda: not robot.ready and not runtime.status()['ready'], 'disconnect')
            checkpoint = robot.recovery_checkpoint()
            occupancy = runtime.store.list_occupancy()
            broker('second')
            runtime.reconnect()
            robot.reconnect(authorization_guard=authorizer.guard)
            with self.assertRaises(Exception):
                runtime.reconcile_moving('sim-r1', 'T1', 'boot-1', robot, ('changed',))
            self.assertEqual(runtime.store.list_occupancy(), occupancy)
            runtime.reconcile_moving('sim-r1', 'T1', 'boot-1', robot, checkpoint)
            self.assertIn('T1', runtime.gate._blocked)
            second_stop = robot.tick(1)
            self.assertEqual((second_stop['current_node'], second_stop['visit_index']), ('B', 3))
            prove_and_report(second_stop, 1, 'second')
            wait(lambda: not robot._queue.empty(), 'final segment')
            final = robot.tick(1)
            self.assertEqual((final['current_node'], final['visit_index']), ('D', 4))
            prove_and_report(final, 3, 'final')
            wait(lambda: runtime.store.list_occupancy() == {('node', 'D'): 'sim-r1'}, 'final occupancy')

    def test_restart_keeps_occupancy_and_blocks_old_moving_order(self):
        port = free_port()
        broker_env = isolated_environment(os.environ)
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='field-runtime-restart-') as folder, ExitStack() as cleanup, \
                open(os.devnull, 'w') as log, patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            ready = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                                       BROKER, str(port), ready], env=broker_env, stdout=log, stderr=log,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready)
            data = laboratory_map()
            path = Path(folder) / 'server.sqlite3'
            mapping = {'sim-r1': {'T1': 'boot-1'}}
            runtime = SimRuntime(path, data, mapping, port=port, client_id='field-restart-a',
                                 field_simulation=True)
            runtime.start()
            cleanup.callback(runtime.stop)
            inbox = SimOrderInbox(Path(folder) / 'robot.sqlite3', 'sim-r1', 'lab@v1')
            inbox.initialize()
            robot = SimMqttOrderRobot(SimOrderExecutor(inbox, data, 'A', experimental_field_map=True,
                                                       field_incoming_edge='E0'), port,
                authorization_guard=SimOrderAuthorizer(runtime, robot_id='sim-r1').guard)
            robot.start()
            cleanup.callback(robot.stop)
            runtime.create_task('T1')
            runtime.prepare_field('T1', 'D', [{'robot_id': 'sim-r1', 'node': 'A',
                'incoming_edge': 'E0', 'available': True}], {'type': 'none'})
            runtime.publish('T1')
            deadline = time.monotonic() + 8
            while robot._queue.empty() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(robot._queue.empty())
            moving = robot.tick(.0005)
            self.assertEqual(moving['status'], 'moving')
            before = runtime.store.list_occupancy()
            runtime.stop()
            restarted = SimRuntime(path, data, mapping, port=port, client_id='field-restart-b',
                                   field_simulation=True)
            restarted.start()
            cleanup.callback(restarted.stop)
            self.assertEqual(restarted.store.list_occupancy(), before)
            self.assertIn('T1', restarted.gate._blocked)
            import json
            payload = json.dumps(moving['order'], separators=(',', ':')).encode('utf-8')
            self.assertFalse(SimOrderAuthorizer(restarted, robot_id='sim-r1')(payload))
            self.assertEqual(robot.executor.snapshot()['pose'], moving['pose'])

    def test_two_simulated_robots_repeat_visit_and_share_zone(self):
        port = free_port()
        broker_env = isolated_environment(os.environ)
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='field-runtime-') as folder, ExitStack() as cleanup, \
                open(os.devnull, 'w') as log, patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            ready = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                                       BROKER, str(port), ready], env=broker_env, stdout=log, stderr=log,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready)
            data = laboratory_map()
            runtime = SimRuntime(Path(folder) / 'server.sqlite3', data,
                                 {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                                 port=port, client_id='field-runtime-test', field_simulation=True)
            runtime.start()
            cleanup.callback(runtime.stop)
            app = FastAPI()
            app.include_router(router)
            app.state.sim_runtime = runtime
            app.state.sim_api_token = 'test-token'

            def api(method, path, body=None):
                code, value, _ = asyncio.run(request(app, method, '/simulation' + path,
                                                     body=body, token='Bearer test-token'))
                self.assertEqual(code, 200, (path, code, value))
                return value

            robots = {}
            for rid, node, edge in [('sim-r1', 'A', 'E0'), ('sim-r2', 'X', 'E5')]:
                inbox = SimOrderInbox(Path(folder) / (rid + '.sqlite3'), rid, 'lab@v1')
                inbox.initialize()
                executor = SimOrderExecutor(inbox, data, node, experimental_field_map=True,
                                            field_incoming_edge=edge)
                robot = SimMqttOrderRobot(executor, port,
                    authorization_guard=SimOrderAuthorizer(runtime, robot_id=rid).guard)
                robot.start()
                cleanup.callback(robot.stop)
                robots[rid] = robot

            def wait(predicate, label, seconds=8):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    value = predicate()
                    if value:
                        return value
                    time.sleep(.02)
                self.fail('timed out: ' + label + '; workflow=' + repr(runtime.workflow.last_error))

            def prepare(task, rid, node, edge, goal):
                api('POST', '/tasks/' + task)
                result = api('POST', '/tasks/' + task + '/prepare-field', {
                    'destination': goal, 'candidates': [{'robot_id': rid, 'node': node,
                    'incoming_edge': edge, 'available': True}], 'action': {'type': 'none'}})
                api('POST', '/tasks/' + task + '/publish')
                wait(lambda: not robots[rid]._queue.empty(), 'first order ' + rid)
                return result

            a = prepare('T1', 'sim-r1', 'A', 'E0', 'D')
            b = prepare('T2', 'sim-r2', 'X', 'E5', 'Y')
            self.assertEqual(a['nodes'], ['A', 'B', 'C', 'B', 'D'])
            self.assertEqual(b['nodes'], ['X', 'C', 'Y'])
            before = runtime.store.list_occupancy()
            self.assertEqual(before[('zone', 'shared-c')], 'sim-r2')

            def stop_and_prove(rid, task, session):
                state = robots[rid].tick(1)
                self.assertEqual(state['status'], 'stopped')
                receipt = state['receipt']
                challenge = api('POST', '/recovery/' + task + '/begin', {'session': session})['challenge']
                api('POST', '/recovery/' + task + '/complete', {'session': session, 'challenge': challenge,
                    'robot_id': rid, 'order_id': task, 'seq': receipt['seq'], 'map_id': 'lab@v1',
                    'stopped_at': state['current_node'], 'order_digest': receipt['digest'], 'stopped': True})
                return state

            def send_stop(rid, task, session, state, start_index, event_id):
                order = state['order']
                return robots[rid].publish_event({'type': 'rear_clear', 'event_id': event_id,
                    'robot_session': session, 'order_id': task, 'seq': order['seq'], 'map_id': 'lab@v1',
                    'stopped_at': state['current_node'], 'cleared_resources': [list(item) for item in
                    occurrence_clearance(data, order['nodes'], start_index)]})

            state_a = stop_and_prove('sim-r1', 'T1', 'boot-1')
            self.assertEqual((state_a['current_node'], state_a['visit_index']), ('B', 1))
            send_stop('sim-r1', 'T1', 'boot-1', state_a, 0, 'a1')
            wait(lambda: any(e['event_id'] == 'a1' for e in runtime.pending_events()['events']), 'blocked event')
            self.assertEqual(runtime.store.list_occupancy(), before)

            state_b = stop_and_prove('sim-r2', 'T2', 'boot-2')
            send_stop('sim-r2', 'T2', 'boot-2', state_b, 0, 'b1')
            wait(lambda: ('zone', 'shared-c') not in runtime.store.list_occupancy(), 'second robot clearance')
            self.assertEqual(runtime.retry('sim-r1', 'a1')['status'], 'advanced')
            api('POST', '/orders/T1/2/submit', {'session': 'boot-1'})
            wait(lambda: not robots['sim-r1']._queue.empty(), 'second order')
            state_a = stop_and_prove('sim-r1', 'T1', 'boot-1')
            self.assertEqual((state_a['current_node'], state_a['visit_index']), ('B', 3))
            send_stop('sim-r1', 'T1', 'boot-1', state_a, 1, 'a2')
            wait(lambda: not robots['sim-r1']._queue.empty(), 'third order')
            state_a = stop_and_prove('sim-r1', 'T1', 'boot-1')
            self.assertEqual((state_a['current_node'], state_a['visit_index']), ('D', 4))
            send_stop('sim-r1', 'T1', 'boot-1', state_a, 3, 'a3')
            wait(lambda: runtime.store.list_occupancy() == {('node', 'D'): 'sim-r1',
                                                          ('node', 'Y'): 'sim-r2'}, 'final occupancy')
            wait(lambda: not runtime.pending_events()['events'], 'event bookkeeping')
            self.assertTrue(runtime.status()['ready'])
            self.assertIsNone(runtime.workflow.last_error)


if __name__ == '__main__':
    unittest.main()
