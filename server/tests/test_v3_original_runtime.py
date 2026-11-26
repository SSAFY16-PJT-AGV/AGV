"""Original v3 parking points, SQLite, private MQTT and two virtual robots.

Receipts and rear clearance are simulated signals, not evidence of real robot
position, physical stopping, safe clearance, or permission to drive hardware.
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
class OriginalV3RuntimeTest(unittest.TestCase):
    def test_two_spur_to_spur_routes_reserve_and_finish_without_extra_waits(self):
        source = Path(__file__).resolve().parents[1] / 'examples/mat_ssafy_v3.json'
        data = json.loads(source.read_text(encoding='utf-8'))
        self.assertEqual({z['node'] for z in data['zones'] if z['type'] == 'waiting'},
                         {f'D{i:02d}' for i in range(1, 9)})
        self.assertEqual(len(data['zones']), 8)
        port = free_port()
        broker_env = isolated_environment(os.environ)
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='v3-original-runtime-') as folder, ExitStack() as cleanup, \
                patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            log = cleanup.enter_context(open(Path(folder) / 'broker.log', 'w+', encoding='utf-8'))
            ready = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                BROKER, str(port), ready], env=broker_env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            try:
                wait_for_owned_broker(self, broker, ready)
            except AssertionError as error:
                log.flush()
                log.seek(0)
                raise AssertionError(f'{error}: {log.read()}') from error
            runtime = SimRuntime(Path(folder) / 'server.sqlite3', data,
                {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                port=port, client_id='v3-original-runtime', field_simulation=True)
            runtime.start()
            cleanup.callback(runtime.stop)
            receiver_errors = []
            runtime.receiver.on_error = receiver_errors.append
            robots = {}
            for rid, node in [('sim-r1', 'D01'), ('sim-r2', 'D06')]:
                inbox = SimOrderInbox(Path(folder) / (rid + '.sqlite3'), rid, 'mat_ssafy_v3@v0')
                inbox.initialize()
                executor = SimOrderExecutor(inbox, data, node, experimental_field_map=True,
                                            field_incoming_edge='E_' + node)
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

            def prepare(task, rid, node, goal):
                return runtime.prepare_field(task, goal, [{'robot_id': rid, 'node': node,
                    'incoming_edge': 'E_' + node, 'available': True}], {'type': 'none'})

            def complete(rid, task, session, goal):
                state = robots[rid].tick(100)
                self.assertEqual((state['status'], state['current_node']), ('stopped', goal))
                receipt = state['receipt']
                challenge = runtime.begin(task, session)['challenge']
                runtime.complete(task, session, challenge, rid, task, receipt['seq'],
                    'mat_ssafy_v3@v0', goal, receipt['digest'], True)
                robots[rid].publish_event({'type': 'rear_clear', 'event_id': task + '-clear',
                    'robot_session': session, 'order_id': task, 'seq': state['order']['seq'],
                    'map_id': 'mat_ssafy_v3@v0', 'stopped_at': goal,
                    'cleared_resources': [list(item) for item in
                        occurrence_clearance(data, state['order']['nodes'], 0)]})

            for task in ('T1', 'T2'):
                runtime.create_task(task)
            a = prepare('T1', 'sim-r1', 'D01', 'D04')
            b = prepare('T2', 'sim-r2', 'D06', 'D03')
            self.assertEqual(a['nodes'], ['D01', 'N01', 'J02', 'J03', 'N04', 'D04'])
            self.assertEqual(b['nodes'], ['D06', 'N06', 'J06', 'J07', 'N03', 'D03'])
            self.assertEqual(a['released_until'], 'D04')
            self.assertEqual(b['released_until'], 'D03')
            occupied = runtime.store.list_occupancy()
            self.assertEqual(occupied[('node', 'J03')], 'sim-r1')
            self.assertEqual(occupied[('node', 'J07')], 'sim-r2')
            runtime.publish('T1')
            runtime.publish('T2')
            for rid in robots:
                wait(lambda: not robots[rid]._queue.empty(), rid + ' command')
            complete('sim-r1', 'T1', 'boot-1', 'D04')
            complete('sim-r2', 'T2', 'boot-2', 'D03')
            wait(lambda: runtime.store.list_occupancy() == {('node', 'D04'): 'sim-r1',
                 ('node', 'D03'): 'sim-r2'}, 'final spur occupancy')
            wait(lambda: not runtime.pending_events()['events'], 'event bookkeeping')
            self.assertIsNone(runtime.workflow.last_error)
            self.assertEqual(receiver_errors, [])
            for robot in robots.values():
                robot.stop()
            runtime.stop()
            self.assertEqual([str(error) for error in receiver_errors], ['simulation runtime stopped'])


if __name__ == '__main__':
    unittest.main()
