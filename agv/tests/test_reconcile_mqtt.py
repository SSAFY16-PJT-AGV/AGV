# -*- coding: utf-8 -*-
"""Loopback-only Mosquitto test of the actual MQTT/controller handshake."""
import json
import os
import queue
import socket
import subprocess
import threading
import time
import unittest

import paho.mqtt.client as mqtt
from agv.link.mqtt import MqttLink
from agv.tests.test_link_disconnect_fence import _controller, _payload
from agv.tests.test_reconcile_robot import KEY, sign


@unittest.skipUnless(os.environ.get('MOSQUITTO_EXE'), 'set MOSQUITTO_EXE for isolated loopback')
class ReconcileMqttTests(unittest.TestCase):
    def test_probe_commit_over_loopback_does_not_allow_motion(self):
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        def spawn_broker():
            process = subprocess.Popen([os.environ['MOSQUITTO_EXE'], '-p', str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def stop_broker():
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            self.addCleanup(stop_broker)
            return process
        broker = spawn_broker()
        deadline = time.monotonic() + 5
        while True:
            try:
                sock = socket.create_connection(('127.0.0.1', port), timeout=.2)
                sock.close()
                break
            except OSError:
                if time.monotonic() >= deadline or broker.poll() is not None:
                    self.fail('isolated broker did not start')
                time.sleep(.02)
        server = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        connected, subscribed = threading.Event(), threading.Event()
        responses = queue.Queue()
        def on_connect(client, _userdata, _flags, reason, _properties):
            if not reason.is_failure:
                client.subscribe('agv/physical-1/reconcile/response', qos=1)
                connected.set()
        server.on_connect = on_connect
        server.on_subscribe = lambda *_args: subscribed.set()
        server.on_message = lambda _c, _u, msg: responses.put(json.loads(msg.payload.decode('utf8')))
        server.connect('127.0.0.1', port, 10)
        server.loop_start()
        self.addCleanup(lambda: (server.disconnect(), server.loop_stop()))
        self.assertTrue(connected.wait(5))
        self.assertTrue(subscribed.wait(5))
        link = MqttLink('physical-1', host='127.0.0.1', port=port,
                        reconcile_isolated=True, reconcile_key=KEY).start()
        self.addCleanup(link.stop)
        deadline = time.monotonic() + 5
        while not link._reconcile_ready:
            if time.monotonic() >= deadline:
                self.fail('reconciliation subscription was not acknowledged')
            time.sleep(.01)
        ctl, runner = _controller(link)
        ctl._handle(_payload())
        runner.state, runner.hold_reason, ctl.nav.at_node = 'hold', 'nav_hold', 'B'
        link.publish_event_once = lambda _event: True
        ctl._watch_stop()
        self.assertIsNotNone(ctl._reconcile_event)
        original_event_id = ctl._reconcile_event['event_id']
        broker.terminate()
        broker.wait(timeout=5)
        deadline = time.monotonic() + 8
        while not link.disconnect_pending() or link.connected:
            ctl.step(now=5)
            if time.monotonic() >= deadline:
                self.fail('robot did not observe broker disconnect')
            time.sleep(.02)
        ctl.step(now=5)
        self.assertEqual(runner.state, 'hold')
        # Server client is replaced, robot process/link/controller stay alive.
        server.disconnect()
        server.loop_stop()
        broker = spawn_broker()
        deadline = time.monotonic() + 5
        while True:
            try:
                sock = socket.create_connection(('127.0.0.1', port), timeout=.2)
                sock.close()
                break
            except OSError:
                if time.monotonic() >= deadline or broker.poll() is not None:
                    self.fail('isolated broker did not restart')
                time.sleep(.02)
        server = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        connected.clear()
        subscribed.clear()
        server.on_connect = on_connect
        server.on_subscribe = lambda *_args: subscribed.set()
        server.on_message = lambda _c, _u, msg: responses.put(json.loads(msg.payload.decode('utf8')))
        server.connect('127.0.0.1', port, 10)
        server.loop_start()
        self.addCleanup(lambda: (server.disconnect(), server.loop_stop()))
        self.assertTrue(connected.wait(5))
        self.assertTrue(subscribed.wait(5))
        deadline = time.monotonic() + 10
        while not link.connected or not link._reconcile_ready:
            ctl.step(now=5)
            if time.monotonic() >= deadline:
                self.fail('robot did not reconnect and subscribe')
            time.sleep(.02)
        old_order = {'order_id': 'T2', 'seq': 1, 'map_id': 'm@v1', 'nodes': ['B', 'A'],
                     'released_until': 'A', 'action_at_end': {'type': 'none'}}
        queued = server.publish('agv/physical-1/order', json.dumps(old_order), qos=1, retain=False)
        queued.wait_for_publish(timeout=3)
        for _ in range(10):
            ctl.step(now=5)
            time.sleep(.02)
        self.assertTrue(link.disconnect_pending())
        self.assertEqual(runner.resume_count, 1)
        probe = sign({'v': 1, 'kind': 'probe', 'nonce': 'a' * 64, 'server_epoch': 'b' * 64,
                      'robot_id': 'physical-1', 'robot_session': ctl.session, 'order_id': 'T1',
                      'seq': 1, 'order_digest': ctl.book.current_digest, 'map_id': 'm@v1',
                      'release_index': ctl.book.release_i, 'stopped_at': 'B', 'event_id': None,
                      'event_digest': None})
        def send(kind, value):
            info = server.publish('agv/physical-1/reconcile/' + kind, json.dumps(value), qos=1, retain=False)
            info.wait_for_publish(timeout=3)
            self.assertTrue(info.is_published())
        def receive():
            until = time.monotonic() + 5
            while time.monotonic() < until:
                ctl.step(now=5)
                try:
                    return responses.get(timeout=.05)
                except queue.Empty:
                    pass
            self.fail('no reconciliation reply')
        send('request', probe)
        snapshot = receive()
        self.assertEqual(snapshot['kind'], 'snapshot')
        self.assertEqual(snapshot['event_id'], original_event_id)
        commit = {k: v for k, v in snapshot.items() if k not in ('mac', 'event')}
        commit.update(kind='commit', decision='hold_confirmed')
        send('commit', sign(commit))
        self.assertEqual(receive()['kind'], 'commit_ack')
        self.assertTrue(link.disconnect_pending())
        self.assertEqual(runner.state, 'hold')
        self.assertEqual(runner.resume_count, 1)

        # Delay a fresh response before its one-shot client can publish, then
        # kill the real broker. The next connection must not replay it.
        began, release = threading.Event(), threading.Event()
        original_send = link.publish_reconcile_once
        def delayed_send(value, generation, signal):
            began.set()
            if not release.wait(8):
                return False
            return original_send(value, generation, signal)
        link.publish_reconcile_once = delayed_send
        next_probe = {k: v for k, v in probe.items() if k != 'mac'}
        next_probe['nonce'] = 'd' * 64
        send('request', sign(next_probe))
        errors = []
        def run_until_delay():
            try:
                until = time.monotonic() + 5
                while not began.is_set() and time.monotonic() < until:
                    ctl.step(now=5)
                    time.sleep(.01)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run_until_delay)
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(5)))
        self.assertTrue(began.wait(5), 'delayed response path not reached')
        generation_before = link._connection_generation
        broker.terminate()
        broker.wait(timeout=5)
        until = time.monotonic() + 8
        while link.connected:
            if time.monotonic() >= until:
                self.fail('second disconnect not observed')
            time.sleep(.02)
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        server.disconnect()
        server.loop_stop()
        broker = spawn_broker()
        until = time.monotonic() + 5
        while True:
            try:
                sock = socket.create_connection(('127.0.0.1', port), timeout=.2)
                sock.close()
                break
            except OSError:
                if time.monotonic() >= until:
                    self.fail('broker failed to restart')
                time.sleep(.02)
        observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        observer_ready, observer_subscribed = threading.Event(), threading.Event()
        delayed = queue.Queue()
        def observer_connect(client, _userdata, _flags, reason, _properties):
            if not reason.is_failure:
                client.subscribe('agv/physical-1/reconcile/response', qos=1)
                observer_ready.set()
        observer.on_connect = observer_connect
        observer.on_subscribe = lambda *_args: observer_subscribed.set()
        observer.on_message = lambda _c, _u, msg: delayed.put(msg.payload)
        observer.connect('127.0.0.1', port, 10)
        observer.loop_start()
        self.addCleanup(lambda: (observer.disconnect(), observer.loop_stop()))
        self.assertTrue(observer_ready.wait(5))
        self.assertTrue(observer_subscribed.wait(5))
        until = time.monotonic() + 10
        while not link.connected or not link._reconcile_ready:
            if time.monotonic() >= until:
                self.fail('second robot reconnect failed')
            time.sleep(.02)
        self.assertGreater(link._connection_generation, generation_before)
        with self.assertRaises(queue.Empty):
            delayed.get(timeout=1)
        self.assertTrue(link.disconnect_pending())
        self.assertEqual(runner.state, 'hold')
        self.assertEqual(runner.resume_count, 1)


if __name__ == '__main__':
    unittest.main()
