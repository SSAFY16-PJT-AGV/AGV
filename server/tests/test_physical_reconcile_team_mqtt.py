"""Actual team MQTT/controller and server exchange on a test-owned broker only."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.allocation_store import AllocationStore
from server.physical_reconcile import PhysicalReconciler
from server.physical_reconcile_mqtt import PhysicalReconcileMqtt
from server.physical_state_proof import PhysicalStateWatcher
from server.recovery import RecoveryGate
from sim.event_inbox import SimEventInbox


@unittest.skipUnless(os.environ.get('MOSQUITTO_EXE') and os.environ.get('AGV_FIELD_REPO'),
                     'set MOSQUITTO_EXE and AGV_FIELD_REPO for isolated team broker test')
class TeamReconcileMqttTests(unittest.TestCase):
    def test_same_session_after_real_broker_restart_exchanges_only_hold_evidence(self):
        sys.path.insert(0, os.environ['AGV_FIELD_REPO'])
        self.addCleanup(lambda: sys.path.remove(os.environ['AGV_FIELD_REPO']))
        from agv.link.mqtt import MqttLink
        from agv.tests.test_link_disconnect_fence import _controller, _payload
        import paho.mqtt.client as mqtt
        key = b'K' * 32
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        brokers = []
        def spawn():
            broker = subprocess.Popen([os.environ['MOSQUITTO_EXE'], '-p', str(port)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            brokers.append(broker)
            until(lambda: probe_port(port), 'broker ready', 10)
            return broker
        def stop_brokers():
            for process in brokers:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
        self.addCleanup(stop_brokers)
        broker = spawn()
        link = MqttLink('physical-1', host='127.0.0.1', port=port,
                        reconcile_isolated=True, reconcile_key=key).start()
        self.addCleanup(link.stop)
        until(lambda: link.connected and link._reconcile_ready, 'robot subscription', 10)
        ctl, runner = _controller(link)
        ctl._handle(_payload())
        runner.state, runner.hold_reason, ctl.nav.at_node = 'hold', 'nav_hold', 'B'
        link.publish_event_once = lambda _event: True
        ctl._watch_stop()
        self.assertIsNotNone(ctl._reconcile_event)
        self.assertEqual(runner.resume_count, 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fleet.db'
            store = AllocationStore(path)
            store.initialize()
            store.create_task('T1')
            resources = [('node', 'A'), ('node', 'B'), ('edge', '["A","B"]')]
            allocated = store.allocate('T1', 'physical-1', 'm@v1', ['A', 'B'], 'B', resources,
                                       {'type': 'none'}, map_fingerprint='a' * 64)
            order = store.get_order('T1')
            store.mark_attempted('T1', order['payload'], allocated, resources)
            gate = RecoveryGate(path)
            gate.initialize()
            inbox = SimEventInbox(path, robot_tasks={'physical-1': ('T1',)}, simulation_enabled=True)
            inbox.initialize()
            state_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
            watcher = PhysicalStateWatcher(['physical-1'], client=state_client, host='127.0.0.1', port=port,
                                           log=lambda _: None)
            watcher.start()
            self.addCleanup(watcher.stop)
            runtime = SimpleNamespace(_tasks={'T1': ('physical-1', ctl.session)}, gate=gate)
            watcher._runtime, watcher._sessions = runtime, {'physical-1': ctl.session}
            gate.bind_physical_watcher(watcher)
            broker.terminate()
            broker.wait(timeout=5)
            until(lambda: not link.connected and link.disconnect_pending() and 'T1' in gate._blocked,
                  'both clients observed broker loss', 10, tick=lambda: ctl.step(now=5))
            state_client.loop_stop()
            # Paho's loop_start creates a socketpair every time. Reusing the
            # same disconnected client with another loop_start overwrites the
            # old pair, so replace that test-owned client before restart.
            watcher._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
            del state_client
            spawn()
            until(lambda: link.connected and link._reconcile_ready, 'robot reconnected', 15,
                  tick=lambda: ctl.step(now=5))
            watcher.start()
            until(lambda: watcher._connected.is_set() and watcher._online.get('physical-1') is True,
                  'server watcher resubscribed', 10)
            state = {'robot_session': ctl.session, 'status': 'ERROR', 'error': 'link_lost',
                     'rejected': None, 'order_id': 'T1', 'seq': 1, 'map_id': 'm@v1',
                     'last_node': 'B', 'released_until': 'B'}
            link.publish_state(state)
            until(lambda: watcher.latest('physical-1') == state, 'fresh state report', 5)
            before = store.list_occupancy()
            service = PhysicalReconciler(gate, inbox, {'physical-1': key}, isolated=True)
            bridge = PhysicalReconcileMqtt(service, port=port, isolated=True).start()
            try:
                bridge.request('physical-1', 'T1', ctl.session)
                until(lambda: bridge.last_result is not None, 'signed commit acknowledgement', 8,
                      tick=lambda: ctl.step(now=5))
                self.assertEqual(bridge.last_result['status'], 'signed_ack_recorded')
                self.assertFalse(bridge.last_result['current_hold_proven'])
                self.assertFalse(bridge.last_result['motion_authorized'])
                self.assertEqual(bridge.last_result['event_state'], 'not_observed')
                self.assertIsNone(inbox.get('physical-1', ctl._reconcile_event['event_id']))
                self.assertIn('T1', gate._blocked)
                self.assertEqual(store.list_occupancy(), before)
                self.assertTrue(link.disconnect_pending())
                self.assertEqual((runner.state, runner.resume_count), ('hold', 1))
            finally:
                bridge.stop()


def until(predicate, label, timeout, tick=None):
    deadline = time.monotonic() + timeout
    while not predicate():
        if tick is not None:
            tick()
        if time.monotonic() >= deadline:
            raise AssertionError('timed out: ' + label)
        time.sleep(.02)


def probe_port(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.2):
            return True
    except OSError:
        return False
