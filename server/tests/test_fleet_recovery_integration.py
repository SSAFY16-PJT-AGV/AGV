"""Two synthetic stopped robots: isolated MQTT restart and explicit recovery.

This does not infer a physical stop, resume a mid-edge position, exercise the
web/Unreal fleet UI, or contact a physical device. All broker processes and
databases are created exclusively by this test.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import paho.mqtt.client as mqtt

from server.recovery import RecoveryError
from server.segments import plan_segment
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_runtime import SimRuntime
from server.tests.test_twin_broker_integration import (
    BROKER, connect_publisher, free_port, loopback_environment, stop,
    stop_publisher, wait_for_owned_broker,
)
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


def _map():
    nodes = [{'id': '{}{}'.format(prefix, i), 'pos': [1000 * i, y]}
             for prefix, y in (('A', 0), ('B', 10000)) for i in range(3)]
    edges = [{'from': '{}{}'.format(prefix, i), 'to': '{}{}'.format(prefix, i + 1),
              'shape': [[1000 * i, y], [1000 * (i + 1), y]],
              'bidirectional': True, 'lanes': 1}
             for prefix, y in (('A', 0), ('B', 10000)) for i in range(2)]
    zones = [{'id': '{}wait{}'.format(prefix, i), 'type': 'waiting', 'node': '{}{}'.format(prefix, i)}
             for prefix in ('A', 'B') for i in (1, 2)]
    return {'map_id': 'recovery-test', 'version': 1, 'unit': 'mm',
            'nodes': nodes, 'edges': edges, 'zones': zones}


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class FleetRecoveryIntegrationTest(unittest.TestCase):
    def test_each_stopped_synthetic_robot_requires_fresh_proof_and_explicit_resume(self):
        # SimRuntime and both virtual robots read their MQTT settings from the
        # current process, not just the child broker environment. Remove all
        # inherited MQTT credentials, proxies and AGV settings before any of
        # them are constructed; restore the caller's environment on exit.
        broker_deps = os.environ['AMQTT_TEST_PATH']
        clean_env = {key: value for key, value in isolated_environment(os.environ).items()
                     if not key.lower().startswith('agv_')}
        clean_env['AMQTT_TEST_PATH'] = broker_deps
        clean_env['PYTHON_DOTENV_DISABLED'] = '1'
        with patch.dict(os.environ, clean_env, clear=True), \
                tempfile.TemporaryDirectory(prefix='agv-two-recovery-') as folder, \
                open(os.devnull, 'w', encoding='utf-8') as broker_log, ExitStack() as cleanup:
            self.assertFalse(any(key.lower().startswith(('mqtt_', 'agv_')) for key in os.environ))
            root = Path(folder)
            port = free_port()
            if port == 8000:
                self.skipTest('selected port must differ from the user server')
            ready_file = root / 'broker.ready'
            env = loopback_environment()
            env['PYTHONPATH'] = broker_deps
            python = getattr(sys, '_base_executable', sys.executable)
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

            def start_broker():
                process = subprocess.Popen([python, '-c', BROKER, str(port), str(ready_file)],
                                           env=env, stdout=broker_log, stderr=broker_log,
                                           creationflags=flags)
                cleanup.callback(stop, process)
                wait_for_owned_broker(self, process, str(ready_file))
                return process

            def observer():
                received, subscribed = queue.Queue(), threading.Event()
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                     protocol=mqtt.MQTTv311, reconnect_on_failure=False)
                client.on_connect = lambda c, u, f, r, p: c.subscribe(
                    [('agv/sim-r1/order', 1), ('agv/sim-r2/order', 1)]) if not r.is_failure else None
                client.on_subscribe = lambda c, u, mid, codes, p: subscribed.set() if (
                    len(codes) == 2 and all(not code.is_failure and code.value == 1 for code in codes)) else None
                client.on_message = lambda c, u, message: received.put((message.topic, json.loads(message.payload)))
                connect_publisher(client, port)
                client.loop_start()
                cleanup.callback(stop_publisher, client)
                self.assertTrue(subscribed.wait(5), 'observer failed to subscribe to owned broker')
                return received, client

            broker = start_broker()
            received, _initial_observer = observer()
            data = _map()
            mapping = {'sim-r1': {'task-a': 'boot-a'}, 'sim-r2': {'task-b': 'boot-b'}}
            db_path = root / 'server.sqlite3'
            runtime = SimRuntime(db_path, data, mapping, port=port, client_id='two-recovery-test')
            runtime.start()
            cleanup.callback(runtime.stop)
            robots = {}
            for rid, task, start, destination in (('sim-r1', 'task-a', 'A0', 'A2'),
                                                  ('sim-r2', 'task-b', 'B0', 'B2')):
                inbox = SimOrderInbox(root / (rid + '.sqlite3'), rid, 'recovery-test@v1')
                inbox.initialize()
                robot = SimMqttOrderRobot(SimOrderExecutor(inbox, data, start), port,
                                          authorization_guard=SimOrderAuthorizer(runtime, robot_id=rid).guard)
                robot.start()
                cleanup.callback(robot.stop)
                robots[rid] = robot
                runtime.create_task(task)
                runtime.prepare(task, destination, [{'robot_id': rid, 'node': start, 'available': True}],
                                {'type': 'load'})
                self.assertEqual(runtime.publish(task)['state'], 'acked')
            initial_orders = dict(received.get(timeout=5) for _ in range(2))
            self.assertEqual(set(initial_orders), {'agv/sim-r1/order', 'agv/sim-r2/order'})

            # Obtain durable simulated receipts at each actual released node
            # before interruption. No mid-edge pose is relabeled as stopped.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                states = {rid: robot.tick(.25) for rid, robot in robots.items()}
                if all(state['status'] == 'stopped' for state in states.values()):
                    break
                time.sleep(.02)
            else:
                self.fail('both synthetic robots did not reach their released stops')
            for rid, stop_node in (('sim-r1', 'A1'), ('sim-r2', 'B1')):
                state = states[rid]
                self.assertEqual((state['current_node'], state['order']['released_until'], state['receipt']['state']),
                                 (stop_node, stop_node, 'completed'))
                self.assertEqual(state['receipt']['seq'], 1)
            occupancy = runtime.store.list_occupancy()
            self.assertTrue(any(owner == 'sim-r1' for owner in occupancy.values()))
            self.assertTrue(any(owner == 'sim-r2' for owner in occupancy.values()))

            stop(broker)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and (runtime.status()['ready'] or any(r.ready for r in robots.values())):
                time.sleep(.02)
            self.assertFalse(runtime.status()['ready'])
            for robot in robots.values():
                self.assertFalse(robot.ready)
                with self.assertRaises(RuntimeError):
                    robot.tick(1)
            self.assertEqual(runtime.store.list_occupancy(), occupancy)
            runtime.stop()

            ready_file.unlink()
            start_broker()
            fresh_messages, fresh_client = observer()
            restarted = SimRuntime(db_path, data, mapping, port=port, client_id='two-recovery-test')
            restarted.start()
            cleanup.callback(restarted.stop)
            self.assertEqual(restarted.store.list_occupancy(), occupancy)
            with self.assertRaises(queue.Empty):
                fresh_messages.get(timeout=.4)

            # The completion event is synthesized from the recorded stopped
            # receipt; each robot's new epoch requires its own fresh proof.
            events = {}
            for rid, task, session, prefix in (('sim-r1', 'task-a', 'boot-a', 'A'),
                                                ('sim-r2', 'task-b', 'boot-b', 'B')):
                path = [prefix + str(i) for i in range(3)]
                old = set(plan_segment(data, path).resources)
                new = set(plan_segment(data, path[1:]).resources)
                event = {'type': 'rear_clear', 'event_id': task + '-clear', 'robot_session': session,
                         'order_id': task, 'seq': 1, 'map_id': 'recovery-test@v1',
                         'stopped_at': prefix + '1', 'cleared_resources': [list(item) for item in sorted(old - new)]}
                info = fresh_client.publish('agv/{}/event'.format(rid), json.dumps(event), qos=1, retain=False)
                info.wait_for_publish(timeout=5)
                self.assertTrue(info.is_published(), 'synthetic event did not reach the owned broker')
                deadline = time.monotonic() + 5
                while restarted.inbox.get(rid, event['event_id']) is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIsNotNone(restarted.inbox.get(rid, event['event_id']))
                events[rid] = event
                with self.assertRaises(RecoveryError):
                    restarted.retry(rid, event['event_id'])
                self.assertEqual(restarted.inbox.get(rid, event['event_id'])['state'], 'pending')
                self.assertEqual(restarted.store.list_occupancy(), occupancy)
            with self.assertRaises(queue.Empty):
                fresh_messages.get(timeout=.4)

            for rid, task, session in (('sim-r1', 'task-a', 'boot-a'), ('sim-r2', 'task-b', 'boot-b')):
                state = states[rid]
                challenge = restarted.begin(task, session)['challenge']
                result = restarted.complete(task, session, challenge, rid, task, 1,
                                            state['order']['map_id'], state['current_node'],
                                            state['receipt']['digest'], True)
                self.assertTrue(result['reconciled'])
                advance = restarted.retry(rid, events[rid]['event_id'])
                self.assertEqual((advance['status'], advance['next_seq']), ('advanced', 2))
                # Recovery alone must not send a command until explicit submit.
                with self.assertRaises(queue.Empty):
                    fresh_messages.get(timeout=.3)
                self.assertTrue(restarted.submit(task, 2, session)['queued'])
                topic, order = fresh_messages.get(timeout=5)
                self.assertEqual((topic, order['order_id'], order['seq']),
                                 ('agv/{}/order'.format(rid), task, 2))
            with self.assertRaises(queue.Empty):
                fresh_messages.get(timeout=.5)


if __name__ == '__main__':
    unittest.main()
