"""Reserve team slots before the existing isolated simulated MQTT workflow."""
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
from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_sim_dispatch import TeamShelfSimDispatch
from server.tests.test_team_shelf_order_slots import snapshot
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


class TeamShelfDispatchFailureTest(unittest.TestCase):
    def test_prevalidated_existing_reservation_can_be_published_once(self):
        root = Path(__file__).resolve().parents[2]
        data = json.loads((root / 'server/examples/mat_ssafy_v3.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory(prefix='team-slot-existing-') as folder:
            folder = Path(folder)
            slots = TeamShelfOrderSlots.create_new(folder / 'slots.sqlite3')
            session = slots.begin_session('a' * 32)
            slots.apply_snapshot(session, snapshot(), fence=slots.begin_snapshot(session))
            runtime = SimRuntime(folder / 'runtime.sqlite3', data, {'sim-r1': {'T1': 'boot'}},
                                 port=18883, client_id='slot-existing-test', field_simulation=True)
            dispatch = TeamShelfSimDispatch(slots, runtime)
            bad = {'robot_id': 'sim-r2', 'node': 'D07', 'incoming_edge': 'E_D07', 'available': True}
            with self.assertRaises(ValueError):
                dispatch.validate_candidate('T1', bad)
            self.assertEqual(slots.inspect()['orders'], [])
            candidate = dict(bad, robot_id='sim-r1')
            dispatch.validate_candidate('T1', candidate)
            slots.reserve(session, 'r1', 'milk')
            with patch.object(runtime, 'create_task'), patch.object(runtime, 'prepare_field',
                    return_value={'nodes': ['D07', 'D01']}), patch.object(runtime, 'publish',
                    return_value={'task_id': 'T1', 'state': 'acked'}) as publish:
                result = dispatch.publish_reserved_pickup(session, 'r1', 'T1', candidate)
                self.assertEqual(result['slots']['source_slot_id'], 1)
                with self.assertRaises(ValueError):
                    dispatch.publish_reserved_pickup(session, 'r1', 'T1', candidate)
                self.assertEqual(publish.call_count, 1)

    def test_lost_publish_result_blocks_retry_and_holds_both_slots(self):
        root = Path(__file__).resolve().parents[2]
        data = json.loads((root / 'server/examples/mat_ssafy_v3.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory(prefix='team-slot-failure-') as folder:
            folder = Path(folder)
            slots = TeamShelfOrderSlots.create_new(folder / 'slots.sqlite3')
            session = slots.begin_session('a' * 32)
            slots.apply_snapshot(session, snapshot(), fence=slots.begin_snapshot(session))
            runtime = SimRuntime(folder / 'runtime.sqlite3', data, {'sim-r1': {'T1': 'boot'}},
                                 port=18883, client_id='slot-fail-test', field_simulation=True)
            dispatch = TeamShelfSimDispatch(slots, runtime)
            candidate = {'robot_id': 'sim-r1', 'node': 'D07',
                         'incoming_edge': 'E_D07', 'available': True}
            with patch.object(runtime, 'create_task'), patch.object(runtime, 'prepare_field',
                    return_value={'nodes': ['D07', 'D01']}), patch.object(runtime, 'publish',
                    side_effect=TimeoutError('publication result unknown')) as publish:
                with self.assertRaises(TimeoutError):
                    dispatch.reserve_and_publish_pickup(session, 'r1', 'milk', 'T1', candidate)
                with self.assertRaises(ValueError):
                    dispatch.reserve_and_publish_pickup(session, 'r1', 'milk', 'T1', candidate)
                self.assertEqual(publish.call_count, 1)
            self.assertEqual(slots.get('r1')['state'], 'unknown')
            self.assertEqual({r['slot_id'] for r in slots.inspect()['claims']}, {1, 5})


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class TeamShelfDispatchTest(unittest.TestCase):
    def test_web_style_order_reserves_slots_before_existing_mqtt_navigation(self):
        root = Path(__file__).resolve().parents[2]
        data = json.loads((root / 'server/examples/mat_ssafy_v3.json').read_text(encoding='utf-8'))
        port = free_port()
        env = isolated_environment(os.environ)
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='team-slot-mqtt-') as folder, ExitStack() as stack:
            folder = Path(folder)
            log = stack.enter_context((folder / 'broker.log').open('w+', encoding='utf-8'))
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                BROKER, str(port), str(folder / 'ready')], env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            stack.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(folder / 'ready'))
            with patch.dict(os.environ, isolated_environment(os.environ), clear=True):
                runtime = SimRuntime(folder / 'runtime.sqlite3', data, {'sim-r1': {'T1': 'boot'}},
                                     port=port, client_id='team-slot-test', field_simulation=True)
                runtime.start()
                stack.callback(runtime.stop)
                errors = []
                runtime.receiver.on_error = errors.append
                inbox = SimOrderInbox(folder / 'robot.sqlite3', 'sim-r1', 'mat_ssafy_v3@v0')
                inbox.initialize()
                executor = SimOrderExecutor(inbox, data, 'D07', experimental_field_map=True,
                                            field_incoming_edge='E_D07')
                robot = SimMqttOrderRobot(executor, port,
                    authorization_guard=SimOrderAuthorizer(runtime, robot_id='sim-r1').guard)
                robot.start()
                stack.callback(robot.stop)
                slots = TeamShelfOrderSlots.create_new(folder / 'slots.sqlite3')
                session = slots.begin_session('a' * 32)
                slots.apply_snapshot(session, snapshot(), fence=slots.begin_snapshot(session))
                dispatch = TeamShelfSimDispatch(slots, runtime)
                candidate = {'robot_id': 'sim-r1', 'node': 'D07',
                             'incoming_edge': 'E_D07', 'available': True}
                result = dispatch.reserve_and_publish_pickup(session, 'request-1', 'milk', 'T1', candidate)
                self.assertEqual(result['slots']['source_slot_id'], 1)
                self.assertEqual(result['slots']['destination_slot_id'], 5)
                self.assertEqual(result['navigation']['nodes'][-1], 'D01')
                self.assertEqual(result['broker_ack']['state'], 'acked')
                self.assertEqual({x['slot_id'] for x in slots.inspect()['claims']}, {1, 5})
                deadline = time.monotonic() + 10
                while robot._queue.empty() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertFalse(robot._queue.empty())
                state = robot.tick(100)
                self.assertEqual((state['current_node'], state['status']), ('D01', 'stopped'))
                self.assertEqual(slots.get('request-1')['state'], 'reserved')
                with self.assertRaises(ValueError):
                    dispatch.reserve_and_publish_pickup(session, 'request-1', 'milk', 'T1', candidate)
                self.assertEqual(errors, [])
                robot.stop()
                runtime.stop()
                self.assertEqual([str(item) for item in errors], ['simulation runtime stopped'])


if __name__ == '__main__':
    unittest.main()
