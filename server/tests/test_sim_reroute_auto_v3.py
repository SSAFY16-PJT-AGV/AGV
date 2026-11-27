"""Team v3 map copy, explicitly simulated starts and loopback MQTT."""
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

from server.order_pickup_workflow import PickupWorkflow
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_reroute_automatic import SimRerouteAutoSession
from server.sim_runtime import SimRuntime
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class AutomaticRerouteV3Test(unittest.TestCase):
    def test_two_virtual_robots_finish_on_team_map_with_simulated_starts(self):
        source = Path(__file__).resolve().parents[1] / 'examples' / 'mat_ssafy_v3_simulated_waits.json'
        data = json.loads(source.read_text(encoding='utf-8'))
        port = free_port(); env = isolated_environment(os.environ); env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='auto-reroute-v3-') as folder, ExitStack() as cleanup:
            folder = Path(folder)
            log = cleanup.enter_context((folder / 'broker.log').open('w+', encoding='utf-8'))
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', BROKER,
                str(port), str(folder / 'ready')], env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker); wait_for_owned_broker(self, broker, str(folder / 'ready'))
            with patch.dict(os.environ, isolated_environment(os.environ), clear=True):
                runtime = SimRuntime(folder / 'sim.sqlite3', data,
                    {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                    port=port, client_id='auto-reroute-v3', field_simulation=True, reroute_transitions=True)
                runtime.start(); cleanup.callback(runtime.stop)
                robots = {}
                for rid, start, edge in [('sim-r1', 'J02', 'E2~r'), ('sim-r2', 'N02', 'E10')]:
                    inbox = SimOrderInbox(folder / (rid + '.sqlite3'), rid, 'mat_ssafy_v3@v0'); inbox.initialize()
                    robot = SimMqttOrderRobot(SimOrderExecutor(inbox, data, start, experimental_field_map=True,
                        field_incoming_edge=edge), port, authorization_guard=SimOrderAuthorizer(runtime, robot_id=rid).guard)
                    robot.start(); cleanup.callback(robot.stop); robots[rid] = robot
                runtime.bind_reroute_simulators(robots)
                reroute = SimRerouteAutoSession(runtime, robots['sim-r1'], 'boot-1', key=bytes(range(32)),
                                                simulation_enabled=True).start(); cleanup.callback(reroute.stop)
                first = PickupWorkflow(runtime, robots['sim-r1'], data, {'p': {'pickup_node': 'J07'}},
                    simulation_enabled=True, task_id='T1', session='boot-1', robot_id='sim-r1',
                    action_at_end={'type': 'none'}, wait_for_reservation=True, reroute_session=reroute)
                second = PickupWorkflow(runtime, robots['sim-r2'], data, {'p': {'pickup_node': 'N01'}},
                    simulation_enabled=True, task_id='T2', session='boot-2', robot_id='sim-r2',
                    action_at_end={'type': 'none'}, wait_for_reservation=True)
                self.assertNotEqual(first.submit('p')['status'], 'unknown')
                self.assertNotEqual(second.submit('p')['status'], 'unknown')
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    states = first.tick(1), second.tick(1)
                    if any(item['status'] == 'unknown' for item in states):
                        self.fail('workflow unknown: {}, phase={}'.format(states, reroute._phase))
                    if all(item['status'] == 'pickup_reached' for item in states):
                        break
                    time.sleep(.02)
                self.assertEqual([item['status'] for item in states], ['pickup_reached', 'pickup_reached'])
                self.assertEqual([item['current_node'] for item in states], ['J07', 'N01'])
                self.assertEqual(reroute._phase, 'finished')
                self.assertEqual(json.loads(reroute._payload)['data']['nodes'], ['N01', 'J02', 'J07'])
                self.assertEqual(runtime.inbox.get('sim-r2', states[1]['finished_evidence']['event_id'])['result']['status'], 'finished')


if __name__ == '__main__':
    unittest.main()
