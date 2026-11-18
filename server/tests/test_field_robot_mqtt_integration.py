"""Opt-in loopback MQTT test with the team's virtual-mat robot implementation.

The server's one-shot OrderPublisher sends an explicitly supplied route. This
does not exercise Coordinator, reserve resources or authorize real movement.
The robot uses field-run's real MqttLink/LinkController/Navigator, but its
camera and motors are a synthetic mat simulation. Requires an independently
prepared field-run checkout and OpenCV/NumPy in AGV_FIELD_MAT_DEPS.
"""
import json
from contextlib import ExitStack
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest

import paho.mqtt.client as mqtt

from server.mqtt_client import MqttService
from server.order_publisher import OrderPublisher
from server.field_robot_contract import validate_field_robot_order
from server.field_map_planner import plan_field_route
from server.segments import plan_segment
from server.tools.twin_multi_test import BROKER_SCRIPT, isolated_environment, stop_process, wait_for_ready_log


ROOT = Path(__file__).resolve().parents[2]
FIELD = os.environ.get('AGV_FIELD_REPO')
MAT_DEPS = os.environ.get('AGV_FIELD_MAT_DEPS')
BROKER_DEPS = os.environ.get('AMQTT_TEST_PATH')


@unittest.skipUnless(FIELD and MAT_DEPS and BROKER_DEPS,
                     'set AGV_FIELD_REPO, AGV_FIELD_MAT_DEPS and AMQTT_TEST_PATH for isolated field test')
class FieldRobotMqttIntegration(unittest.TestCase):
    def test_server_publisher_and_team_robot_exchange_real_mqtt_messages(self):
        field = Path(FIELD).resolve()
        mat_deps = Path(MAT_DEPS).resolve()
        broker_deps = Path(BROKER_DEPS).resolve()
        for path in (field / 'sim/link_demo.py', field / 'maps/mat_ssafy_v2.json',
                     mat_deps / 'cv2', broker_deps / 'amqtt'):
            self.assertTrue(path.exists(), 'missing test input: ' + str(path))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]

        # Results and test DB are isolated; no user server/broker is touched.
        parent = ROOT / '.venv'
        with tempfile.TemporaryDirectory(prefix='field-robot-mqtt-', dir=parent) as temp, ExitStack() as stack:
            run = Path(temp)
            broker_log, robot_log = run / 'broker.log', run / 'robot.log'
            env = isolated_environment(os.environ)
            env.update(PYTHONPATH=str(broker_deps), PYTHONUTF8='1', TEMP=str(run), TMP=str(run))
            with broker_log.open('w', encoding='utf-8') as blog, robot_log.open('w', encoding='utf-8') as rlog:
                python = getattr(sys, '_base_executable', sys.executable)
                broker = subprocess.Popen([python, '-u', '-c', BROKER_SCRIPT, str(port)],
                                          env=env, stdout=blog, stderr=blog)
                stack.callback(stop_process, broker)
                wait_for_ready_log(broker, broker_log,
                                   '[AGV_ISOLATED_BROKER_READY]:' + str(broker.pid), 'broker')
                service = MqttService('127.0.0.1', port).start()
                stack.callback(service.stop)
                events, states, subscribed = queue.Queue(), queue.Queue(), threading.Event()
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
                def on_message(client, userdata, msg):
                    value = json.loads(msg.payload.decode('utf-8'))
                    (events if msg.topic.endswith('/event') else states).put(value)
                client.on_connect = lambda c, u, flags, reason, props: c.subscribe(
                    [('agv/sim-r1/event', 1), ('agv/sim-r1/state', 0)])
                client.on_subscribe = lambda c, u, mid, codes, props: subscribed.set()
                client.on_message = on_message
                client.connect('127.0.0.1', port, 15)
                client.loop_start()
                stack.callback(client.loop_stop)
                stack.callback(client.disconnect)
                self.assertTrue(subscribed.wait(5), 'observer subscription timed out')
                deadline = time.monotonic() + 5
                while not service.snapshot()['ready'] and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(service.snapshot()['ready'])
                robot_env = isolated_environment(os.environ)
                robot_env.update(PYTHONPATH=os.pathsep.join([str(mat_deps), str(ROOT), sysconfig.get_paths()['purelib']]),
                                 PYTHONUTF8='1', TEMP=str(run), TMP=str(run))
                robot = subprocess.Popen(
                    [python, '-u', str(field / 'sim/link_demo.py'), '--host', '127.0.0.1',
                     '--port', str(port), '--robot-id', 'sim-r1', '--waiting', 'zones', '--fast',
                     '--battery', '-1', '--max-time', '1200', '--out', str(run / 'robot')], cwd=field,
                    env=robot_env, stdout=rlog, stderr=rlog)
                stack.callback(stop_process, robot)
                deadline = time.monotonic() + 20
                state = None
                while time.monotonic() < deadline:
                    if robot.poll() is not None:
                        self.fail('team robot exited early: ' + robot_log.read_text(encoding='utf-8', errors='replace'))
                    try:
                        state = states.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if state.get('status') == 'IDLE' and state.get('last_node') == 'P_orange':
                        break
                else:
                    self.fail('team robot did not report initial state: ' +
                              robot_log.read_text(encoding='utf-8', errors='replace'))

                # Incoming E13 is an explicit test assumption, not a position
                # proof. Only the main-line portion is planned; the final spur
                # is appended as a known fixture, not by the experimental
                # planner. There is no Coordinator or real reservation here.
                field_map = json.loads((field / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
                route = plan_field_route(field_map, map_ref='mat_ssafy_v2@v0',
                                         incoming_edge='E13', goal='P_purple')
                self.assertEqual(route.nodes, ('P_orange', 'S1', 'M1', 'P_purple'))
                order = {'order_id': 'field-mqtt-test', 'seq': 1, 'map_id': 'mat_ssafy_v2@v0',
                         'nodes': list(route.nodes) + ['D_P_purple'],
                         'released_until': 'D_P_purple', 'action_at_end': {'type': 'none'}}
                validate_field_robot_order(order)
                segment = plan_segment(field_map, order['nodes'])
                self.assertEqual(segment.released_until, order['released_until'])
                owner = object()
                publisher = OrderPublisher(client, enabled=True)
                publisher.claim(owner)
                info = publisher.publish('sim-r1', order, owner=owner)
                info.wait_for_publish(timeout=5)
                self.assertTrue(info.is_published())
                seen, event = [], None
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and event is None:
                    try:
                        event = events.get(timeout=.05)
                    except queue.Empty:
                        pass
                    while not states.empty():
                        state = states.get_nowait()
                        seen.append(state)
                        if state.get('status') == 'ERROR':
                            self.fail('team robot reported an error: ' + repr(state) + '\n' +
                                      robot_log.read_text(encoding='utf-8', errors='replace'))
                    if robot.poll() is not None and event is None:
                        self.fail('team robot exited before rear_clear: ' +
                                  robot_log.read_text(encoding='utf-8', errors='replace'))
                if event is None:
                    self.fail('no rear_clear from team robot: ' +
                              robot_log.read_text(encoding='utf-8', errors='replace'))
                self.assertEqual((event['type'], event['order_id'], event['seq'], event['map_id'],
                                  event['stopped_at']),
                                 ('rear_clear', order['order_id'], 1, order['map_id'], 'D_P_purple'))
                self.assertEqual(set(event), {'type', 'event_id', 'robot_session', 'order_id', 'seq',
                                              'map_id', 'stopped_at', 'cleared_resources'})
                cleared = {tuple(resource) for resource in event['cleared_resources']}
                self.assertEqual(len(cleared), len(event['cleared_resources']))
                self.assertEqual(cleared, set(segment.resources) - {('node', 'D_P_purple')})
                self.assertTrue(any(s.get('status') == 'MOVING' for s in seen), seen[-5:])
                observed = service.snapshot()['robots'].get('sim-r1')
                self.assertIsNotNone(observed)
                self.assertTrue(observed['state_received_live'])
                self.assertIn('battery', observed['state'])
                self.assertIsNone(observed['state']['battery'])
                self.assertTrue(event['robot_session'])
                self.assertTrue(event['event_id'])


if __name__ == '__main__':
    unittest.main()
