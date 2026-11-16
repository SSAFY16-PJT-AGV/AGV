"""MQTT callbacks enqueue commands; simulated motion runs only in tick."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sim.mqtt_order_robot import SimMqttOrderRobot, MAX_QUEUED_ORDERS, _verify_no_proxy
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox
from sim.tests.test_order_executor import payload, test_map


class FakePublish:
    rc = 0
    def wait_for_publish(self, timeout):
        return None
    def is_published(self):
        return True


class FakeClient:
    def __init__(self):
        self.on_connect = self.on_subscribe = self.on_message = self.on_disconnect = None
        self.acks, self.publishes = [], []
        self.is_open = False

    def manual_ack_set(self, value):
        self.manual_ack = value

    def connect(self, host, port, keepalive):
        self.host, self.port = host, port
        self.is_open = True
        return 0

    def subscribe(self, topic, qos):
        self.subscription = (topic, qos)
        return (0, 17)

    def loop_start(self):
        self.on_connect(self, None, None, SimpleNamespace(is_failure=False), None)
        self.on_subscribe(self, None, 17, [SimpleNamespace(is_failure=False, value=1)], None)
        return 0

    def disconnect(self):
        self.is_open = False
        if self.on_disconnect:
            self.on_disconnect(self, None, None, SimpleNamespace(is_failure=False), None)
        return 0

    def loop_stop(self):
        return 0

    def is_connected(self):
        return self.is_open

    def ack(self, mid, qos):
        self.acks.append((mid, qos))
        return 0

    def publish(self, topic, body, qos, retain):
        self.publishes.append((topic, json.loads(body), qos, retain))
        return FakePublish()

    def deliver(self, body, *, topic='agv/sim-r1/order', qos=1, retain=False, mid=10):
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=body,
                                                    qos=qos, retain=retain, mid=mid))


class MqttRobotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        inbox = SimOrderInbox(Path(self.tmp.name) / 'robot.sqlite3', 'sim-r1', 'synthetic@v1')
        inbox.initialize()
        self.executor = SimOrderExecutor(inbox, test_map(), 'A')
        self.client = FakeClient()
        self.client_patch = patch('sim.mqtt_order_robot.mqtt.Client', return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.security_patch = patch('sim.mqtt_order_robot.read_mqtt_security', return_value=(None, None, None))
        self.security_patch.start()
        self.addCleanup(self.security_patch.stop)
        self.robot = SimMqttOrderRobot(self.executor, 19123)
        self.robot.start()
        self.addCleanup(self.robot.stop)

    def test_message_callback_only_queues_and_tick_executes_after_durable_accept(self):
        self.assertTrue(self.robot.ready)
        self.assertEqual((self.client.host, self.client.port), ('127.0.0.1', 19123))
        self.assertEqual(self.client.subscription, ('agv/sim-r1/order', 1))
        self.assertTrue(self.client.manual_ack)
        self.client.deliver(payload())
        self.assertEqual(self.executor.snapshot()['status'], 'idle')
        state = self.robot.tick(0.5)
        self.assertEqual(state['status'], 'moving')
        self.assertEqual(state['pose']['x'], 500)
        self.assertEqual(self.client.acks, [(10, 1)])
        self.assertEqual(self.robot.tick(20)['current_node'], 'B')

    def test_second_robot_receives_its_own_order_and_publishes_its_event(self):
        inbox = SimOrderInbox(Path(self.tmp.name) / 'second.sqlite3', 'sim-r2', 'synthetic@v1')
        inbox.initialize()
        executor = SimOrderExecutor(inbox, test_map(), 'A')
        client = FakeClient()
        with patch('sim.mqtt_order_robot.mqtt.Client', return_value=client):
            robot = SimMqttOrderRobot(executor, 19123).start()
        self.addCleanup(robot.stop)
        self.assertEqual(client.subscription, ('agv/sim-r2/order', 1))
        client.deliver(payload(), topic='agv/sim-r2/order')
        stopped = robot.tick(2)
        self.assertEqual((stopped['status'], stopped['current_node']), ('stopped', 'B'))
        event = {'type': 'rear_clear', 'event_id': 'r2-event', 'robot_session': 'boot-2',
                 'order_id': stopped['receipt']['order_id'], 'seq': stopped['receipt']['seq'],
                 'map_id': stopped['order']['map_id'], 'stopped_at': 'B', 'cleared_resources': []}
        robot.publish_event(event)
        self.assertEqual(client.publishes, [('agv/sim-r2/event', event, 1, False)])

    def test_disconnect_and_invalid_messages_fail_closed(self):
        self.client.deliver(payload(), qos=0)
        self.assertFalse(self.robot.ready)
        with self.assertRaises(RuntimeError):
            self.robot.tick(1)
        self.assertEqual(self.executor.snapshot()['status'], 'idle')
        self.assertEqual(self.client.acks, [])

    def test_invalid_payload_does_not_ack_or_move(self):
        self.client.deliver(b'not-json')
        with self.assertRaises(RuntimeError):
            self.robot.tick(1)
        self.assertFalse(self.robot.ready)
        self.assertEqual(self.executor.snapshot()['status'], 'idle')
        self.assertEqual(self.client.acks, [])

    def test_queue_overflow_fails_without_processing_any_order(self):
        for mid in range(MAX_QUEUED_ORDERS + 1):
            self.client.deliver(payload(), mid=mid)
        self.assertFalse(self.robot.ready)
        with self.assertRaises(RuntimeError):
            self.robot.tick(2)
        self.assertEqual(self.executor.snapshot()['status'], 'idle')

    def test_completed_segment_event_is_matching_one_shot_qos1(self):
        self.client.deliver(payload())
        stopped = self.robot.tick(2)
        event = {'type': 'rear_clear', 'event_id': 'e1', 'robot_session': 'boot-1',
                 'order_id': stopped['receipt']['order_id'], 'seq': stopped['receipt']['seq'],
                 'map_id': stopped['order']['map_id'], 'stopped_at': 'B',
                 'cleared_resources': []}
        self.robot.publish_event(event)
        self.assertEqual(self.client.publishes, [('agv/sim-r1/event', event, 1, False)])
        with self.assertRaises(RuntimeError):
            self.robot.publish_event(dict(event, event_id='e2'))
        self.assertEqual(len(self.client.publishes), 1)

    def test_broker_disconnect_prevents_further_motion(self):
        self.client.deliver(payload())
        self.robot.tick(0.25)
        self.client.disconnect()
        self.assertFalse(self.robot.ready)
        with self.assertRaises(RuntimeError):
            self.robot.tick(5)
        self.assertEqual(self.executor.snapshot()['pose']['x'], 250)

    def test_order_authorizer_rejects_before_persist_ack_or_motion(self):
        self.robot.stop()
        self.client.acks.clear()
        seen = []
        self.robot = SimMqttOrderRobot(self.executor, 19123,
                                       order_authorizer=lambda value: seen.append(value) or False)
        self.robot.start()
        self.addCleanup(self.robot.stop)
        self.client.deliver(payload())
        with self.assertRaises(RuntimeError):
            self.robot.tick(2)
        self.assertEqual(seen, [payload()])
        self.assertFalse(self.robot.ready)
        self.assertEqual(self.executor.snapshot()['status'], 'idle')
        self.assertEqual(self.client.acks, [])

    def test_order_authorizer_exception_stops_without_ack(self):
        self.robot.stop()
        def broken(_payload):
            raise OSError('transient database failure')
        self.robot = SimMqttOrderRobot(self.executor, 19123, order_authorizer=broken)
        self.robot.start()
        self.addCleanup(self.robot.stop)
        self.client.deliver(payload())
        with self.assertRaises(RuntimeError):
            self.robot.tick(1)
        self.assertFalse(self.robot.ready)
        self.assertEqual(self.executor.snapshot()['status'], 'idle')
        self.assertEqual(self.client.acks, [])

    def test_authorization_guard_encloses_durable_order_acceptance(self):
        self.robot.stop()
        events = []
        @contextmanager
        def guard(_payload):
            events.append('guard-enter')
            yield
            events.append('guard-exit')
        self.robot = SimMqttOrderRobot(self.executor, 19123, authorization_guard=guard)
        self.robot.start()
        self.addCleanup(self.robot.stop)
        original = self.executor.accept
        def record(value):
            events.append('accept')
            return original(value)
        with patch.object(self.executor, 'accept', side_effect=record):
            self.client.deliver(payload())
            self.robot.tick(0)
        self.assertEqual(events, ['guard-enter', 'accept', 'guard-exit'])

    def test_rejected_guard_never_accepts_acks_or_moves(self):
        self.robot.stop()
        @contextmanager
        def guard(_payload):
            raise ValueError('reservation is not current')
            yield
        self.robot = SimMqttOrderRobot(self.executor, 19123, authorization_guard=guard)
        self.robot.start()
        self.addCleanup(self.robot.stop)
        self.client.deliver(payload())
        with self.assertRaises(RuntimeError):
            self.robot.tick(1)
        self.assertFalse(self.robot.ready)
        self.assertEqual(self.executor.snapshot()['status'], 'idle')
        self.assertEqual(self.client.acks, [])

    def test_movement_revalidates_the_reservation_on_each_tick(self):
        self.robot.stop()
        allowed = [True]
        calls = []
        @contextmanager
        def guard(value):
            calls.append(json.loads(value))
            if not allowed[0]:
                raise ValueError('reservation revoked')
            yield
        self.robot = SimMqttOrderRobot(self.executor, 19123, authorization_guard=guard)
        self.robot.start()
        self.addCleanup(self.robot.stop)
        self.client.deliver(payload())
        self.assertEqual(self.robot.tick(0)['pose']['x'], 0)
        allowed[0] = False
        with self.assertRaises(RuntimeError):
            self.robot.tick(1)
        self.assertEqual(self.executor.snapshot()['pose']['x'], 0)
        self.assertFalse(self.robot.ready)

    def test_local_broker_cannot_be_redirected_by_socks_or_mqtt_proxy(self):
        socks = SimpleNamespace(get_default_proxy=lambda: (2, 'proxy.example', 1080))
        with patch('sim.mqtt_order_robot.mqtt.socks', socks):
            with self.assertRaises(RuntimeError):
                _verify_no_proxy()
        socks = SimpleNamespace(get_default_proxy=lambda: None)
        with patch('sim.mqtt_order_robot.mqtt.socks', socks), \
                patch('sim.mqtt_order_robot.urllib.request.getproxies', return_value={'mqtt': 'socks://proxy.example'}), \
                patch('sim.mqtt_order_robot.urllib.request.proxy_bypass', return_value=False):
            with self.assertRaises(RuntimeError):
                _verify_no_proxy()


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class MqttRobotBrokerTest(unittest.TestCase):
    def test_two_robots_use_distinct_broker_sessions_and_order_topics(self):
        import paho.mqtt.client as mqtt
        from server.tests.test_twin_broker_integration import (
            BROKER, connect_publisher, free_port, loopback_environment, stop, wait_for_owned_broker,
        )
        port = free_port()
        env = loopback_environment()
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='sim-two-robots-') as folder, \
                open(os.devnull, 'w', encoding='utf-8') as broker_log:
            ready_file = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([python, '-c', BROKER, str(port), ready_file], env=env,
                                      stdout=broker_log, stderr=broker_log, creationflags=flags)
            robots = []
            publisher = None
            try:
                wait_for_owned_broker(self, broker, ready_file)
                with patch('sim.mqtt_order_robot.read_mqtt_security', return_value=(None, None, None)), \
                        patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost',
                                                'no_proxy': '127.0.0.1,localhost'}):
                    for robot_id, start in (('sim-r1', 'A'), ('sim-r2', 'C')):
                        inbox = SimOrderInbox(Path(folder) / (robot_id + '.sqlite3'), robot_id, 'synthetic@v1')
                        inbox.initialize()
                        robots.append(SimMqttOrderRobot(SimOrderExecutor(inbox, test_map(), start), port).start())
                    publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
                    connect_publisher(publisher, port)
                    publisher.loop_start()
                    order1 = payload(order_id='task-1')
                    order2 = payload(order_id='task-2', nodes=['C', 'D'], stop='D')
                    for topic, body in (('agv/sim-r1/order', order1), ('agv/sim-r2/order', order2)):
                        info = publisher.publish(topic, body, qos=1, retain=False)
                        info.wait_for_publish(timeout=5)
                        self.assertTrue(info.is_published())
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        first = robots[0].tick(0)
                        second = robots[1].tick(0)
                        if first['status'] == second['status'] == 'moving':
                            break
                        time.sleep(0.03)
                    self.assertEqual((first['status'], first['order']['order_id']), ('moving', 'task-1'))
                    self.assertEqual((second['status'], second['order']['order_id']), ('moving', 'task-2'))
                    first, second = (robot.tick(0.25) for robot in robots)
                    self.assertEqual((first['status'], second['status']), ('moving', 'moving'))
                    self.assertGreater(first['pose']['x'], 0)
                    self.assertGreater(second['pose']['x'], 2000)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        first = robots[0].tick(0.5)
                        second = robots[1].tick(0.5)
                        if first['status'] == second['status'] == 'stopped':
                            break
                        time.sleep(0.03)
                    self.assertEqual((first['status'], first['current_node']), ('stopped', 'B'))
                    self.assertEqual((second['status'], second['current_node']), ('stopped', 'D'))
                    self.assertTrue(all(robot.ready for robot in robots))
            finally:
                if publisher is not None:
                    publisher.disconnect()
                    publisher.loop_stop()
                for robot in robots:
                    robot.stop()
                stop(broker)

    def test_actual_qos1_order_and_event_through_owned_loopback_broker(self):
        import paho.mqtt.client as mqtt
        from server.tests.test_twin_broker_integration import (
            BROKER, connect_publisher, free_port, loopback_environment, stop, wait_for_owned_broker,
        )
        port = free_port()
        env = loopback_environment()
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='sim-mqtt-robot-') as folder, \
                open(os.devnull, 'w', encoding='utf-8') as broker_log:
            ready_file = str(Path(folder) / 'broker.ready')
            broker = subprocess.Popen([python, '-c', BROKER, str(port), ready_file], env=env,
                                      stdout=broker_log, stderr=broker_log, creationflags=flags)
            try:
                wait_for_owned_broker(self, broker, ready_file)
                inbox = SimOrderInbox(Path(folder) / 'robot.sqlite3', 'sim-r1', 'synthetic@v1')
                inbox.initialize()
                executor = SimOrderExecutor(inbox, test_map(), 'A')
                with patch('sim.mqtt_order_robot.read_mqtt_security', return_value=(None, None, None)), \
                        patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost',
                                                'no_proxy': '127.0.0.1,localhost'}):
                    robot = SimMqttOrderRobot(executor, port).start()
                    publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
                    connected, subscribed = threading.Event(), threading.Event()
                    events = queue.Queue()
                    publisher.on_connect = lambda client, data, flags, reason, props: (
                        client.subscribe('agv/sim-r1/event', qos=1), connected.set())
                    publisher.on_subscribe = lambda *args: subscribed.set()
                    publisher.on_message = lambda client, data, msg: events.put(json.loads(msg.payload))
                    try:
                        connect_publisher(publisher, port)
                        publisher.loop_start()
                        self.assertTrue(connected.wait(5))
                        self.assertTrue(subscribed.wait(5))
                        info = publisher.publish('agv/sim-r1/order', payload(), qos=1, retain=False)
                        info.wait_for_publish(timeout=5)
                        self.assertTrue(info.is_published())
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            state = robot.tick(0.1)
                            if state['status'] == 'stopped':
                                break
                            time.sleep(0.03)
                        self.assertEqual((state['status'], state['current_node']), ('stopped', 'B'))
                        event = {'type': 'rear_clear', 'event_id': 'e1', 'robot_session': 'boot-1',
                                 'order_id': state['receipt']['order_id'], 'seq': state['receipt']['seq'],
                                 'map_id': state['order']['map_id'], 'stopped_at': 'B',
                                 'cleared_resources': []}
                        self.assertTrue(robot.publish_event(event)['published'])
                        self.assertEqual(events.get(timeout=5), event)
                    finally:
                        publisher.disconnect()
                        publisher.loop_stop()
                        robot.stop()
            finally:
                stop(broker)
