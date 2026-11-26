"""Synthetic shortage approval uses the existing isolated MQTT navigation.

The resulting broker ACK and simulated arrival do not prove that a pick or
place happened. Slot-specific Unreal actions are not connected here.
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

from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_runtime import SimRuntime
from server.simulated_shelf_perception import SimulatedShelfPerception
from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_sim_dispatch import TeamShelfSimDispatch
from server.tests.test_simulated_shelf_perception import frame, rows
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class SimulatedShelfPerceptionMqttTests(unittest.TestCase):
    def test_approved_shortage_navigates_via_mqtt_and_keeps_both_claims(self):
        root = Path(__file__).resolve().parents[2]
        data = json.loads((root / 'server/examples/mat_ssafy_v3.json').read_text(encoding='utf-8'))
        port = free_port()
        env = isolated_environment(os.environ)
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='perception-sim-mqtt-') as folder, ExitStack() as stack:
            folder = Path(folder)
            log = stack.enter_context((folder / 'broker.log').open('w+', encoding='utf-8'))
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                BROKER, str(port), str(folder / 'ready')], env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            stack.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(folder / 'ready'))
            with patch.dict(os.environ, isolated_environment(os.environ), clear=True):
                runtime = SimRuntime(folder / 'runtime.sqlite3', data, {'sim-r1': {'T1': 'boot'}},
                                     port=port, client_id='perception-sim-test', field_simulation=True)
                runtime.start()
                stack.callback(runtime.stop)
                errors = []
                runtime.receiver.on_error = errors.append
                inbox = SimOrderInbox(folder / 'robot.sqlite3', 'sim-r1', 'mat_ssafy_v3@v0')
                inbox.initialize()
                robot = SimMqttOrderRobot(SimOrderExecutor(inbox, data, 'D07',
                    experimental_field_map=True, field_incoming_edge='E_D07'), port,
                    authorization_guard=SimOrderAuthorizer(runtime, robot_id='sim-r1').guard)
                robot.start()
                stack.callback(robot.stop)
                slots = TeamShelfOrderSlots.create_new(folder / 'slots.sqlite3')
                session = slots.begin_session('synthetic-test-world')
                bridge = SimulatedShelfPerception(slots, session, max_age_seconds=60)
                capture = bridge.begin_capture()
                for i in range(1, 7):
                    shelf = f'N{i:02d}'
                    observation = rows('milk', 'empty', 'empty', 'empty') if i == 1 else rows(
                        'empty', 'empty', 'empty', 'empty')
                    bridge.accept_frame(capture, shelf, frame(shelf, observation))
                self.assertEqual(bridge.shortages(threshold=1)['milk']['status'], 'shortage')
                dispatcher = TeamShelfSimDispatch(slots, runtime)
                candidate = {'robot_id': 'sim-r1', 'node': 'D07',
                             'incoming_edge': 'E_D07', 'available': True}
                with self.assertRaises(ValueError):
                    bridge.approve_and_dispatch(dispatcher, 'r1', 'milk', 'T1', candidate,
                                                threshold=1, approved=False)
                self.assertTrue(robot._queue.empty())
                result = bridge.approve_and_dispatch(dispatcher, 'r1', 'milk', 'T1', candidate,
                                                       threshold=1, approved=True)
                self.assertEqual(result['broker_ack']['state'], 'acked')
                self.assertEqual((result['slots']['source_slot_id'], result['slots']['destination_slot_id']), (1, 5))
                deadline = time.monotonic() + 10
                while robot._queue.empty() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertFalse(robot._queue.empty())
                state = robot.tick(100)
                self.assertEqual((state['status'], state['current_node']), ('stopped', 'D01'))
                result = slots.inspect()
                self.assertEqual((result['slots'][0]['state'], result['slots'][0]['product_type']),
                                 ('occupied', 'milk'))
                self.assertEqual(result['slots'][4]['state'], 'empty')
                self.assertEqual(result['orders'][0]['state'], 'reserved')
                self.assertEqual({claim['slot_id'] for claim in result['claims']}, {1, 5})
                self.assertEqual(errors, [])
                robot.stop()
                runtime.stop()
                self.assertEqual([str(e) for e in errors], ['simulation runtime stopped'])


if __name__ == '__main__':
    unittest.main()
