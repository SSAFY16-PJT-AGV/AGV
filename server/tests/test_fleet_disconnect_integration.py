"""Two virtual AGVs during broker loss, on an independently owned broker.

The geometry, identity, occupancy and stop indications here are synthetic;
this test neither connects to the team's broker nor proves physical safety.
"""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import queue
import secrets
import sqlite3
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
from server.tests.test_order_fleet_integration import scenario_map
from server.tests.test_twin_broker_integration import (BROKER, connect_publisher,
    free_port, stop, stop_publisher, wait_for_owned_broker)
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


ROOT = Path(__file__).resolve().parents[2]


def _wait(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.03)
    return False


def _occupancy(path):
    with closing(sqlite3.connect(path, timeout=5)) as db:
        db.execute('BEGIN')
        return tuple(db.execute('SELECT kind,resource_id,task_id FROM allocation_occupancy '
                                'ORDER BY kind,resource_id,task_id'))


def _persistent_orders(server_path, folder):
    with closing(sqlite3.connect(server_path, timeout=5)) as db:
        db.execute('BEGIN')
        outbox = tuple(db.execute('SELECT task_id,payload,state FROM allocation_outbox ORDER BY task_id'))
        progress = tuple(db.execute('SELECT task_id,seq,payload,state FROM progress_orders ORDER BY task_id,seq'))
    inboxes = {}
    for robot_id in ('sim-r1', 'sim-r2'):
        with closing(sqlite3.connect(Path(folder) / (robot_id + '.sqlite3'), timeout=5)) as db:
            db.execute('BEGIN')
            inboxes[robot_id] = tuple(db.execute('SELECT order_id,seq,digest,payload,state '
                                                 'FROM sim_inbox_orders ORDER BY order_id,seq'))
    return outbox, progress, inboxes


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class FleetBrokerDisconnectIntegrationTest(unittest.TestCase):
    def test_two_moving_robots_preserve_occupancy_and_do_not_resume_after_broker_restart(self):
        port = free_port()
        while port == 8000:
            port = free_port()
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        broker_env = isolated_environment(os.environ)
        broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='fleet-disconnect-') as folder, \
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
            runtime = SimRuntime(db_path, sim_map, sessions, port=port, client_id='disconnect-test',
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
            fleet = FleetProductControlEngine(engines, sim_map)
            demo = DemoRuntime(fleet, port, [])
            demo.start()
            cleanup.callback(demo.stop)
            self.assertTrue(demo.actions.attach(object(), True))
            red = demo.create_product_order('red')
            blue = demo.create_product_order('blue')
            self.assertEqual((red['assigned_robot'], blue['assigned_robot']), ('sim-r1', 'sim-r2'))
            def both_moving():
                return all(robot.executor.snapshot()['status'] == 'moving' for robot in robots.values())
            self.assertTrue(_wait(both_moving), 'both virtual orders must be accepted before fault injection')
            # Hold only this test runtime's lock while recording and killing
            # its broker, avoiding a tick/rear-clear transition at the cut.
            with demo.lock:
                self.assertTrue(both_moving())
                before = _occupancy(db_path)
                orders_before = _persistent_orders(db_path, folder)
                self.assertEqual({row[2] for row in before}, {'web-pickup', 'r2-web-pickup'})
                self.assertTrue(any(row[0] == 'edge' for row in before))
                self.assertEqual(len(orders_before[0]), 2)
                self.assertTrue(all(len(orders_before[2][key]) == 1 for key in robots))
                stop(broker)
            self.assertTrue(_wait(lambda: all(not robot.ready for robot in robots.values())
                                  and not runtime.status()['ready'] and not demo.connected.is_set()),
                            'all test clients must detect the broker disconnect')
            self.assertTrue(_wait(lambda: all(engine.snapshot()['status'] == 'unknown'
                                              for engine in engines.values())),
                            'the simulated jobs must be marked unknown')
            detected = {key: robot.executor.snapshot() for key, robot in robots.items()}
            preserved = _occupancy(db_path)
            self.assertEqual(preserved, before)
            self.assertEqual(_persistent_orders(db_path, folder), orders_before)
            # Measure from detection, not from the instant the socket was cut.
            time.sleep(2.0)
            self.assertEqual({key: robot.executor.snapshot()['pose'] for key, robot in robots.items()},
                             {key: state['pose'] for key, state in detected.items()})
            self.assertEqual(_occupancy(db_path), preserved)
            self.assertEqual(_persistent_orders(db_path, folder), orders_before)
            for robot in robots.values():
                with self.assertRaises(RuntimeError):
                    robot.tick(.1)

            # Bring back the same *test-owned* broker. A new independent
            # observer checks both command topics; it cannot certify absence
            # of messages sent before its subscription was acknowledged.
            ready_path2 = str(Path(folder) / 'broker-restarted.ready')
            restarted = subprocess.Popen([python, '-c', BROKER, str(port), ready_path2],
                                         cwd=ROOT, env=broker_env, stdout=broker_log,
                                         stderr=broker_log, creationflags=flags)
            cleanup.callback(stop, restarted)
            wait_for_owned_broker(self, restarted, ready_path2)
            messages, subscribed = queue.Queue(), threading.Event()
            observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id='disconnect-observer-' + secrets.token_hex(5),
                                   reconnect_on_failure=False)
            def on_connect(client, userdata, flags, reason, properties):
                if not reason.is_failure:
                    client.subscribe([('agv/sim-r1/order', 1), ('agv/sim-r2/order', 1)])
            def on_subscribe(client, userdata, mid, reasons, properties):
                if len(reasons) == 2 and all(not reason.is_failure for reason in reasons):
                    subscribed.set()
            observer.on_connect, observer.on_subscribe = on_connect, on_subscribe
            observer.on_message = lambda client, userdata, message: messages.put(message.topic)
            connect_publisher(observer, port)
            observer.loop_start()
            cleanup.callback(stop_publisher, observer)
            self.assertTrue(subscribed.wait(8))
            time.sleep(2.0)
            self.assertTrue(messages.empty(), 'no simulated command may be automatically re-published')
            self.assertTrue(all(not robot.ready for robot in robots.values()))
            self.assertFalse(runtime.status()['ready'])
            self.assertEqual(_occupancy(db_path), preserved)
            self.assertEqual(_persistent_orders(db_path, folder), orders_before)
            self.assertEqual({key: robot.executor.snapshot()['pose'] for key, robot in robots.items()},
                             {key: state['pose'] for key, state in detected.items()})
            with self.assertRaises(ConnectionError):
                demo.create_product_order('red')
            with self.assertRaises(RuntimeError):
                runtime.begin('web-pickup', 'demo-session')
            self.assertEqual(_occupancy(db_path), preserved)


if __name__ == '__main__':
    unittest.main()
