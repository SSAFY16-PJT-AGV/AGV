"""Team map, two virtual robots and an isolated broker; no physical evidence."""
from contextlib import ExitStack
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import paho.mqtt.client as mqtt
from fastapi import FastAPI

from server.route_occurrence import occurrence_clearance
from server.recovery import RecoveryError
from server.segment_progress import SegmentOccupied
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_api import router
from server.sim_runtime import SimRuntime
from server.tests.test_sim_api import request
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port,
                                                       stop, stop_publisher, wait_for_owned_broker)
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class FieldContentionRestartTests(unittest.TestCase):
    def test_lost_clearance_duplicate_and_restart_preserve_both_occupancies(self):
        source = Path(__file__).resolve().parents[1] / 'examples/mat_ssafy_v2_simulated_waits.json'
        data = json.loads(source.read_text(encoding='utf-8'))
        # Match the existing team-map contention fixture with only three invented waits.
        keep = {'test-wait-M3', 'test-wait-P_green', 'test-wait-P_purple'}
        data['zones'] = [z for z in data['zones']
                         if not z['id'].startswith('test-wait-') or z['id'] in keep]
        port = free_port()
        broker_env = isolated_environment(os.environ)
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='field-contention-restart-') as folder, ExitStack() as cleanup, \
                open(os.devnull, 'w') as log, patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            ready = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                                       BROKER, str(port), ready], env=broker_env, stdout=log, stderr=log,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready)
            path = Path(folder) / 'server.sqlite3'
            mapping = {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}}
            runtime = SimRuntime(path, data, mapping, port=port,
                                 client_id='field-contention-before', field_simulation=True)
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

            def wait(predicate, label):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(.02)
                self.fail('timed out waiting for ' + label)

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

            def event(task, session, state, start, event_id):
                current = state['order']
                return {'type': 'rear_clear', 'event_id': event_id, 'robot_session': session,
                        'order_id': task, 'seq': current['seq'], 'map_id': 'mat_ssafy_v2@v0',
                        'stopped_at': state['current_node'], 'cleared_resources': [list(x) for x in
                            occurrence_clearance(data, current['nodes'], start)]}

            runtime.create_task('T1')
            runtime.create_task('T2')
            prepare('T1', 'sim-r1', 'P_yellow', 'E8', 'P_green')
            runtime.publish('T1')
            wait(lambda: not robots['sim-r1']._queue.empty(), 'first A order')
            first_a = stop_and_prove('sim-r1', 'T1', 'boot-1')
            self.assertEqual((first_a['current_node'], first_a['visit_index']), ('M3', 2))
            robots['sim-r1'].publish_event(event('T1', 'boot-1', first_a, 0, 'a1'))
            wait(lambda: not robots['sim-r1']._queue.empty(), 'second A order')
            prepare('T2', 'sim-r2', 'V_E16', 'E16', 'P_purple')
            runtime.publish('T2')
            wait(lambda: not robots['sim-r2']._queue.empty(), 'first B order')
            second_a = stop_and_prove('sim-r1', 'T1', 'boot-1')
            self.assertEqual((second_a['current_node'], second_a['visit_index']), ('M3', 4))
            blocked_event = event('T1', 'boot-1', second_a, 2, 'a2')
            before = runtime.store.list_occupancy()
            self.assertEqual(before[('zone', 'Z_X1_X2')], 'sim-r2')
            robots['sim-r1'].publish_event(blocked_event)
            wait(lambda: any(e['event_id'] == 'a2' for e in runtime.pending_events()['events']), 'blocked A')
            self.assertEqual(runtime.store.list_occupancy(), before)

            # B reaches its virtual stop, but its event is intentionally withheld.
            stopped_b = stop_and_prove('sim-r2', 'T2', 'boot-2')
            with self.assertRaises(SegmentOccupied):
                runtime.retry('sim-r1', 'a2')
            self.assertEqual(runtime.store.list_occupancy(), before)
            self.assertTrue(robots['sim-r1']._queue.empty())
            tasks_before = {task: runtime.task_status(task) for task in ('T1', 'T2')}

            # Duplicate delivery is injected through a distinct test publisher;
            # the virtual robot itself intentionally does not republish events.
            injector = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='field-duplicate-injector',
                                   protocol=mqtt.MQTTv311, reconnect_on_failure=False)
            connect_publisher(injector, port)
            injector.loop_start()
            cleanup.callback(stop_publisher, injector)
            info = injector.publish('agv/sim-r1/event', json.dumps(blocked_event), qos=1, retain=False)
            info.wait_for_publish(timeout=5)
            self.assertTrue(info.is_published())
            wait(lambda: isinstance(runtime.receiver.last_result, dict)
                 and runtime.receiver.last_result.get('status') == 'duplicate', 'duplicate A event')
            self.assertEqual(runtime.store.list_occupancy(), before)
            self.assertEqual({task: runtime.task_status(task) for task in ('T1', 'T2')}, tasks_before)

            runtime.stop()
            restarted = SimRuntime(path, data, mapping, port=port,
                                   client_id='field-contention-after', field_simulation=True)
            restarted.start()
            cleanup.callback(restarted.stop)
            self.assertEqual(restarted.store.list_occupancy(), before)
            self.assertEqual({task: restarted.task_status(task) for task in ('T1', 'T2')}, tasks_before)
            self.assertEqual({'T1', 'T2'} & restarted.gate._blocked, {'T1', 'T2'})
            self.assertEqual([e['event_id'] for e in restarted.pending_events()['events']], ['a2'])

            # A delayed B stop event cannot be applied with the old recovery proof.
            delayed = event('T2', 'boot-2', stopped_b, 0, 'b1')
            robots['sim-r2'].publish_event(delayed)
            wait(lambda: {e['event_id'] for e in restarted.pending_events()['events']} == {'a2', 'b1'},
                 'delayed B event after restart')
            app = FastAPI()
            app.include_router(router)
            app.state.sim_runtime = restarted
            app.state.sim_api_token = 'test-token'
            code, diagnostics, headers = asyncio.run(request(
                app, 'GET', '/simulation/tasks/T1/diagnostics', token='Bearer test-token'))
            self.assertEqual(code, 200)
            self.assertEqual(headers.get(b'cache-control'), b'no-store')
            self.assertTrue(diagnostics['recovery_blocked_observation'])
            self.assertTrue(diagnostics['mqtt_ready_observation'])
            snapshot = diagnostics['database_snapshot']
            self.assertEqual(snapshot['orders'], tasks_before['T1']['orders'])
            self.assertEqual(snapshot['occupancy'], tasks_before['T1']['occupancy'])
            self.assertEqual(snapshot['pending_events'], [{'event_id': 'a2', 'seq': 2}])
            self.assertNotIn('b1', repr(diagnostics))
            with self.assertRaises(RecoveryError):
                restarted.retry('sim-r1', 'a2')
            with self.assertRaises(RecoveryError):
                restarted.retry('sim-r2', 'b1')
            self.assertEqual(restarted.store.list_occupancy(), before)
            self.assertEqual({task: restarted.task_status(task) for task in ('T1', 'T2')}, tasks_before)
            self.assertTrue(robots['sim-r1']._queue.empty())
            self.assertEqual(robots['sim-r1'].executor.snapshot()['visit_index'], 4)
            self.assertEqual(robots['sim-r2'].executor.snapshot()['current_node'], stopped_b['current_node'])


if __name__ == '__main__':
    unittest.main()
