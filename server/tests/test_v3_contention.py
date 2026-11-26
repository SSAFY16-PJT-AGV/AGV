"""V3 crossing contention through SQLite, isolated MQTT and two virtual AGVs.

Main-line wait zones and recovery proof are synthetic; this never controls
hardware or authenticates a real pose. Entry edges are explicit test inputs.
"""
from contextlib import ExitStack
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


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class V3ContentionTest(unittest.TestCase):
    def test_wait_for_v3_crossing_to_be_cleared_before_second_entry(self):
        # Exact team topology with additional synthetic waiting zones. The
        # stopped receipts and rear-clear events are simulated, never hardware
        # evidence. Original team's map is not modified.
        path = Path(__file__).resolve().parents[1] / 'examples/mat_ssafy_v3_simulated_waits.json'
        data = json.loads(path.read_text(encoding='utf-8'))
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
            receiver_errors = []
            runtime.receiver.on_error = receiver_errors.append
            robots = {}
            for rid, node, edge in [('sim-r1', 'N01', 'E12'), ('sim-r2', 'N06', 'E6')]:
                inbox = SimOrderInbox(Path(folder) / (rid + '.sqlite3'), rid, 'mat_ssafy_v3@v0')
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
                                 'mat_ssafy_v3@v0', state['current_node'], receipt['digest'], True)
                return state

            def event(rid, task, session, state, start, eid):
                order = state['order']
                robots[rid].publish_event({'type': 'rear_clear', 'event_id': eid, 'robot_session': session,
                    'order_id': task, 'seq': order['seq'], 'map_id': 'mat_ssafy_v3@v0',
                    'stopped_at': state['current_node'], 'cleared_resources': [list(item) for item in
                    occurrence_clearance(data, order['nodes'], start)]})

            runtime.create_task('T1')
            runtime.create_task('T2')
            # Both first segments need J07: N01->J02->J07 and
            # N06->J06->J07. T1 holds it before T2 tries to plan.
            a = prepare('T1', 'sim-r1', 'N01', 'E12', 'N03')
            self.assertEqual(a['nodes'], ['N01', 'J02', 'J07', 'N03'])
            runtime.publish('T1')
            wait(lambda: not robots['sim-r1']._queue.empty(), 'first segment A')
            before = runtime.store.list_occupancy()
            self.assertEqual(before[('node', 'J07')], 'sim-r1')
            with self.assertRaises(ValueError):
                prepare('T2', 'sim-r2', 'N06', 'E6', 'N01')
            self.assertEqual(runtime.store.list_occupancy(), before)
            self.assertTrue(robots['sim-r2']._queue.empty())

            state_a = stop_and_prove('sim-r1', 'T1', 'boot-1')
            self.assertEqual((state_a['current_node'], state_a['visit_index']), ('J07', 2))
            event('sim-r1', 'T1', 'boot-1', state_a, 0, 'a1')
            wait(lambda: not robots['sim-r1']._queue.empty(), 'second segment A')
            # A is stopped at J07 and its next reservation still holds it.
            before = runtime.store.list_occupancy()
            self.assertEqual(before[('node', 'J07')], 'sim-r1')
            with self.assertRaises(ValueError):
                prepare('T2', 'sim-r2', 'N06', 'E6', 'N01')
            self.assertEqual(runtime.store.list_occupancy(), before)
            self.assertTrue(robots['sim-r2']._queue.empty())

            state_a = stop_and_prove('sim-r1', 'T1', 'boot-1')
            self.assertEqual((state_a['current_node'], state_a['visit_index']), ('N03', 3))
            event('sim-r1', 'T1', 'boot-1', state_a, 2, 'a2')
            wait(lambda: ('node', 'J07') not in runtime.store.list_occupancy(), 'A clears J07')
            b = prepare('T2', 'sim-r2', 'N06', 'E6', 'N01')
            self.assertEqual(b['nodes'], ['N06', 'J06', 'J07', 'N03', 'N02', 'N07', 'N01'])
            self.assertEqual(runtime.store.list_occupancy()[('node', 'J07')], 'sim-r2')
            runtime.publish('T2')
            wait(lambda: not robots['sim-r2']._queue.empty(), 'first segment B')
            state_b = stop_and_prove('sim-r2', 'T2', 'boot-2')
            self.assertEqual((state_b['current_node'], state_b['visit_index']), ('J07', 2))
            self.assertTrue(runtime.status()['ready'])
            self.assertIsNone(runtime.workflow.last_error)
            self.assertEqual(receiver_errors, [])
            for robot in robots.values():
                robot.stop()
            runtime.stop()
            # SimRuntime deliberately reports its shutdown fence as an error.
            self.assertEqual([str(error) for error in receiver_errors], ['simulation runtime stopped'])


if __name__ == '__main__':
    unittest.main()
