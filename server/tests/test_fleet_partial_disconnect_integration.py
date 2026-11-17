"""A single MQTT client fault against a test-owned broker and virtual fleet."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import paho.mqtt.client as mqtt

from server.order_demo_app import DemoRuntime, PRODUCTS
from server.order_fleet import FleetProductControlEngine
from server.order_product_control import ProductControlEngine
from server.order_sim_map import make_order_sim_map
from server.sim_handoff_store import SimHandoffStore
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_runtime import SimRuntime
from server.tests.test_fleet_disconnect_integration import _occupancy, _wait
from server.tests.test_order_fleet_integration import scenario_map
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher, free_port, stop,
                                                       stop_publisher, wait_for_owned_broker)
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class PartialFleetDisconnectTest(unittest.TestCase):
    def test_only_target_client_disconnects_and_both_positions_are_frozen(self):
        port = free_port()
        while port == 8000:
            port = free_port()
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        broker_env = isolated_environment(os.environ)
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='fleet-partial-') as folder, \
                open(os.devnull, 'w', encoding='utf-8') as broker_log, ExitStack() as cleanup, \
                patch.dict(os.environ, isolated_environment(os.environ), clear=True):
            ready_path = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([python, '-c', BROKER, str(port), ready_path],
                                      cwd=ROOT, env=broker_env, stdout=broker_log,
                                      stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_path)
            raw = scenario_map(False)
            for node in raw['nodes']:
                node['x'] *= 10
                node['y'] *= 10
            sim_map = make_order_sim_map(raw)
            db_path = Path(folder) / 'server.sqlite3'
            starts = {'sim-r1': 'J01', 'sim-r2': 'J05'}
            store = SimHandoffStore.create_new(db_path, simulation_enabled=True, idle_positions=starts)
            sessions = {'sim-r1': {'web-pickup': 'demo-session', 'web-delivery': 'demo-session'},
                        'sim-r2': {'r2-web-pickup': 'demo-session-r2', 'r2-web-delivery': 'demo-session-r2'}}
            runtime = SimRuntime(db_path, sim_map, sessions, port=port, client_id='partial-test',
                                 simulation_store=store)
            runtime.start()
            cleanup.callback(runtime.stop)
            map_id = '{}@v{}'.format(sim_map['map_id'], sim_map['version'])
            products = {item['product_type']: dict(item) for item in PRODUCTS}
            robots, engines = {}, {}
            for robot_id, start in starts.items():
                inbox = SimOrderInbox(Path(folder) / (robot_id + '.sqlite3'), robot_id, map_id)
                inbox.initialize()
                authorizer = SimOrderAuthorizer(runtime, robot_id=robot_id)
                robot = SimMqttOrderRobot(SimOrderExecutor(inbox, sim_map, start), port,
                                          authorization_guard=authorizer.guard)
                robot.start()
                cleanup.callback(robot.stop)
                robots[robot_id] = robot
                engines[robot_id] = ProductControlEngine(
                    runtime, robot, sim_map, products, robot_id=robot_id,
                    twin_id={'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}[robot_id],
                    session=sessions[robot_id][next(iter(sessions[robot_id]))],
                    task_prefix={'sim-r1': 'web', 'sim-r2': 'r2-web'}[robot_id],
                    wait_for_reservation=True)
            demo = DemoRuntime(FleetProductControlEngine(engines, sim_map), port, [], auto_recovery=True)
            self.assertTrue(demo.actions.attach(object(), True))
            demo.start()
            cleanup.callback(demo.stop)
            demo.create_product_order('red')
            demo.create_product_order('blue')
            self.assertTrue(_wait(lambda: all(r.executor.snapshot()['status'] == 'moving'
                                              for r in robots.values()), 10))
            replacement = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                      client_id='sim-r2-order-executor',
                                      clean_session=False, protocol=mqtt.MQTTv311,
                                      reconnect_on_failure=False)
            subscribed = threading.Event()
            replacement.on_connect = lambda _c, _u, _f, reason, _p: subscribed.set() if not reason.is_failure else None
            cleanup.callback(stop_publisher, replacement)
            # The shared control lock prevents recovery from racing the
            # measurements; the independent Paho network loops stay live.
            with demo.lock:
                before = _occupancy(db_path)
                connect_publisher(replacement, port)
                replacement.loop_start()
                self.assertTrue(subscribed.wait(8))
                self.assertTrue(_wait(lambda: not robots['sim-r2'].ready, 8))
                self.assertTrue(robots['sim-r1'].ready)
                self.assertTrue(runtime.status()['ready'])
                self.assertTrue(demo.connected.is_set())
                self.assertIsNone(broker.poll())
                frozen = {key: robot.executor.snapshot()['pose'] for key, robot in robots.items()}
                time.sleep(.5)
                self.assertEqual({key: robot.executor.snapshot()['pose'] for key, robot in robots.items()}, frozen)
                self.assertEqual(_occupancy(db_path), before)


if __name__ == '__main__':
    unittest.main()
