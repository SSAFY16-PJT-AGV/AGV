"""Existing runtime, isolated MQTT and two virtual AGVs on a team-map copy.

Main-line wait zones and recovery proof are synthetic; this never controls
hardware or authenticates a real pose. Entry edges are explicit test inputs.
"""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from server.route_occurrence import occurrence_clearance
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_runtime import SimRuntime
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH') and os.environ.get('AGV_FIELD_REPO'),
                     'requires isolated aMQTT and team field-run source')
class TeamMapRuntimeMqttTests(unittest.TestCase):
    def test_two_robots_repeat_and_contend_for_shared_crossing(self):
        root = Path(os.environ['AGV_FIELD_REPO'])
        data = json.loads((root / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
        data = copy.deepcopy(data)
        for node in ('M3', 'P_green', 'P_purple'):
            data['zones'].append({'id': 'test-wait-' + node, 'type': 'waiting', 'node': node})
        port = free_port()
        broker_env = isolated_environment(os.environ)
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='field-team-runtime-') as folder, ExitStack() as cleanup, \
                open(os.devnull, 'w') as log, patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            ready = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                                       BROKER, str(port), ready], env=broker_env, stdout=log, stderr=log,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready)
            runtime = SimRuntime(Path(folder) / 'server.sqlite3', data,
                                 {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                                 port=port, client_id='team-field-runtime', field_simulation=True)
            runtime.start()
            cleanup.callback(runtime.stop)
            robots = {}
            for rid, node, edge in [('sim-r1', 'P_yellow', 'E8'), ('sim-r2', 'V_E16', 'E16')]:
                inbox = SimOrderInbox(Path(folder) / (rid + '.sqlite3'), rid, 'mat_ssafy_v2@v0')
                inbox.initialize()
                executor = SimOrderExecutor(inbox, data, node, experimental_field_map=True,
                                            field_incoming_edge=edge)
                robot = SimMqttOrderRobot(executor, port,
                    authorization_guard=SimOrderAuthorizer(runtime, robot_id=rid).guard)
                robot.start()
                cleanup.callback(robot.stop)
                robots[rid] = robot

            def wait(predicate, label, seconds=10):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(.02)
                self.fail('timed out: ' + label + '; workflow=' + repr(runtime.workflow.last_error))

            def prepare(task, rid, node, edge, goal):
                return runtime.prepare_field(task, goal, [{'robot_id': rid, 'node': node,
                    'incoming_edge': edge, 'available': True}], {'type': 'none'})

            def stop_and_prove(rid, task, session):
                state = robots[rid].tick(10)
                self.assertEqual(state['status'], 'stopped')
                receipt = state['receipt']
                challenge = runtime.begin(task, session)['challenge']
                runtime.complete(task, session, challenge, rid, task, receipt['seq'],
                                 'mat_ssafy_v2@v0', state['current_node'], receipt['digest'], True)
                return state

            def event(rid, task, session, state, start, eid):
                order = state['order']
                robots[rid].publish_event({'type': 'rear_clear', 'event_id': eid, 'robot_session': session,
                    'order_id': task, 'seq': order['seq'], 'map_id': 'mat_ssafy_v2@v0',
                    'stopped_at': state['current_node'], 'cleared_resources': [list(item) for item in
                    occurrence_clearance(data, order['nodes'], start)]})

            runtime.create_task('T1')
            runtime.create_task('T2')
            a = prepare('T1', 'sim-r1', 'P_yellow', 'E8', 'P_green')
            self.assertEqual(a['nodes'], ['P_yellow', 'X1', 'M3', 'P_red', 'M3',
                                          'X1', 'P_yellow', 'S3', 'P_green'])
            runtime.publish('T1')
            wait(lambda: not robots['sim-r1']._queue.empty(), 'first order A')
            before = runtime.store.list_occupancy()
            self.assertEqual(before[('zone', 'Z_X1_X2')], 'sim-r1')
            with self.assertRaises(ValueError):
                prepare('T2', 'sim-r2', 'V_E16', 'E16', 'P_purple')
            self.assertEqual(runtime.store.list_occupancy(), before)

            state_a = stop_and_prove('sim-r1', 'T1', 'boot-1')
            self.assertEqual((state_a['current_node'], state_a['visit_index']), ('M3', 2))
            event('sim-r1', 'T1', 'boot-1', state_a, 0, 'a1')
            wait(lambda: not robots['sim-r1']._queue.empty(), 'second order A')
            prepare('T2', 'sim-r2', 'V_E16', 'E16', 'P_purple')
            runtime.publish('T2')
            wait(lambda: not robots['sim-r2']._queue.empty(), 'first order B')
            state_a = stop_and_prove('sim-r1', 'T1', 'boot-1')
            self.assertEqual((state_a['current_node'], state_a['visit_index']), ('M3', 4))
            before = runtime.store.list_occupancy()
            event('sim-r1', 'T1', 'boot-1', state_a, 2, 'a2')
            wait(lambda: any(e['event_id'] == 'a2' for e in runtime.pending_events()['events']), 'blocked A')
            self.assertEqual(runtime.store.list_occupancy(), before)
            state_b = stop_and_prove('sim-r2', 'T2', 'boot-2')
            event('sim-r2', 'T2', 'boot-2', state_b, 0, 'b1')
            wait(lambda: ('zone', 'Z_X1_X2') not in runtime.store.list_occupancy(), 'B clears crossing')
            self.assertEqual(runtime.retry('sim-r1', 'a2')['status'], 'advanced')
            runtime.submit('T1', 3, 'boot-1')
            wait(lambda: not robots['sim-r1']._queue.empty(), 'third order A')
            state_a = stop_and_prove('sim-r1', 'T1', 'boot-1')
            self.assertEqual((state_a['current_node'], state_a['visit_index']), ('P_green', 8))
            event('sim-r1', 'T1', 'boot-1', state_a, 4, 'a3')
            wait(lambda: runtime.store.list_occupancy() == {('node', 'P_green'): 'sim-r1',
                                                          ('node', 'P_purple'): 'sim-r2'}, 'final occupancy')
            wait(lambda: not runtime.pending_events()['events'], 'event bookkeeping')
            self.assertTrue(runtime.status()['ready'])
            self.assertIsNone(runtime.workflow.last_error)


if __name__ == '__main__':
    unittest.main()
