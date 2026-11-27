"""Actual team MQTT/controller and server exchange on a test-owned broker only."""
import os
import asyncio
from contextlib import ExitStack
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from server.allocation_store import AllocationStore
from server.physical_reconcile import PhysicalReconciler
from server.physical_reconcile_mqtt import PhysicalReconcileMqtt, isolated_gate_reconciliation
from server.physical_evidence_observer import PhysicalEvidenceInbox, PhysicalEvidenceWatcher
from server.physical_recovery_diagnostics import PhysicalRecoveryDiagnostics
from server.recovery import RecoveryGate
from sim.event_inbox import SimEventInbox


@unittest.skipUnless(os.environ.get('MOSQUITTO_EXE') and os.environ.get('AGV_FIELD_REPO'),
                     'set MOSQUITTO_EXE and AGV_FIELD_REPO for isolated team broker test')
class TeamReconcileMqttTests(unittest.TestCase):
    def test_two_robot_signed_holds_after_broker_restart_preserve_both_allocations(self):
        sys.path.insert(0, os.environ['AGV_FIELD_REPO'])
        self.addCleanup(lambda: sys.path.remove(os.environ['AGV_FIELD_REPO']))
        from agv.link.mqtt import MqttLink
        from agv.tests.test_link_disconnect_fence import _controller, _payload
        import paho.mqtt.client as mqtt
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
            def spawn():
                process = subprocess.Popen([os.environ['MOSQUITTO_EXE'], '-p', str(port)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                def stop():
                    if process.poll() is None:
                        process.terminate(); process.wait(timeout=5)
                cleanup.callback(stop)
                until(lambda: probe_port(port) and process.poll() is None, 'broker ready', 10)
                return process
            broker = spawn()
            links, controllers, runners, keys = {}, {}, {}, {}
            for robot, task, start, end, key in (
                    ('physical-1', 'T1', 'A', 'B', b'A' * 32),
                    ('physical-2', 'T2', 'C', 'D', b'B' * 32)):
                link = MqttLink(robot, host='127.0.0.1', port=port,
                                reconcile_isolated=True, reconcile_key=key).start()
                cleanup.callback(link.stop)
                until(lambda: link.connected and link._reconcile_ready, 'robot subscription', 10)
                ctl, runner = _controller(link)
                ctl.nav.at_node = start
                payload = json.loads(_payload())
                payload.update(order_id=task, nodes=[start, end], released_until=end)
                ctl._handle(json.dumps(payload).encode('utf-8'))
                runner.state, runner.hold_reason, ctl.nav.at_node = 'hold', 'nav_hold', end
                link.publish_event_once = lambda _event: True
                ctl._watch_stop()
                self.assertIsNotNone(ctl._reconcile_event)
                links[robot], controllers[robot], runners[robot], keys[robot] = link, ctl, runner, key
            path = Path(tmp) / 'fleet.db'
            store = AllocationStore(path); store.initialize()
            for robot, task, start, end in (
                    ('physical-1', 'T1', 'A', 'B'), ('physical-2', 'T2', 'C', 'D')):
                store.create_task(task)
                resources = [('node', start), ('node', end), ('edge', json.dumps([start, end], separators=(',', ':')))]
                allocation = store.allocate(task, robot, 'm@v1', [start, end], end, resources,
                                            {'type': 'none'}, map_fingerprint='a' * 64)
                record = store.get_order(task)
                store.mark_attempted(task, record['payload'], allocation, resources)
            gate = RecoveryGate(path); gate.initialize()
            SimEventInbox(path, robot_tasks={'sim-placeholder': ('placeholder',)}, simulation_enabled=True).initialize()
            inbox = PhysicalEvidenceInbox(path, {'physical-1': ('T1',), 'physical-2': ('T2',)}, isolated=True)
            task_sessions = {task: (robot, controllers[robot].session)
                             for robot, task in (('physical-1', 'T1'), ('physical-2', 'T2'))}
            watcher = PhysicalEvidenceWatcher(gate, task_sessions, host='127.0.0.1', port=port, isolated=True)
            watcher.start(); cleanup.callback(watcher.stop)
            broker.terminate(); broker.wait(timeout=5)
            def tick_all():
                for ctl in controllers.values(): ctl.step(now=5)
            until(lambda: all(not link.connected and link.disconnect_pending() for link in links.values()),
                  'both robots detected loss', 10, tick=tick_all)
            watcher.stop(); spawn()
            until(lambda: all(link.connected and link._reconcile_ready for link in links.values()),
                  'both robots reconnected', 15, tick=tick_all)
            watcher = PhysicalEvidenceWatcher(gate, task_sessions, host='127.0.0.1', port=port, isolated=True)
            watcher.start(); cleanup.callback(watcher.stop)
            until(lambda: all(watcher._online.get(robot) is True for robot in links), 'robot online states', 10)
            for robot, task, end in (('physical-1', 'T1', 'B'), ('physical-2', 'T2', 'D')):
                links[robot].publish_state({'robot_session': controllers[robot].session, 'status': 'ERROR',
                    'error': 'link_lost', 'rejected': None, 'order_id': task, 'seq': 1, 'map_id': 'm@v1',
                    'last_node': end, 'released_until': end})
            until(lambda: all(robot in watcher._latest for robot in links), 'fresh states', 5)
            before = store.list_occupancy()
            with isolated_gate_reconciliation(gate, inbox, watcher, keys, port=port, isolated=True) as bridge:
                requests = {}
                for robot, task in (('physical-1', 'T1'), ('physical-2', 'T2')):
                    requests[robot] = bridge.request(robot, task, controllers[robot].session)
                until(lambda: all(bridge.result_for(nonce) is not None for nonce in requests.values()),
                      'both signed confirmations', 8, tick=tick_all)
                for robot in ('physical-1', 'physical-2'):
                    result = bridge.result_for(requests[robot])
                    self.assertEqual(result['status'], 'signed_ack_recorded')
                    self.assertFalse(result['motion_authorized'])
                    self.assertFalse(result['current_hold_proven'])
                    self.assertTrue(links[robot].disconnect_pending())
                    self.assertEqual((runners[robot].state, runners[robot].resume_count), ('hold', 1))
                # Inspect the same existing gate, live watcher and signed
                # ledger. The diagnostic must not replay or authorize either
                # robot even though both signed confirmations were recorded.
                diagnostic = PhysicalRecoveryDiagnostics(gate, watcher, isolated=True).snapshot()
                self.assertEqual(diagnostic['scope'], 'isolated_physical_observation')
                self.assertEqual({row['robot_id'] for row in diagnostic['tasks']}, set(links))
                for row in diagnostic['tasks']:
                    self.assertEqual((row['gate'], row['signed_record'], row['event_state']),
                                     ('blocked', 'recorded', 'not_observed'))
                    self.assertFalse(row['motion_authorized'])
                    self.assertFalse(row['physical_stop_proven'])
                from server.main import app
                from server.tests.test_physical_recovery_diagnostics import call
                previous = app.state._state.copy()
                try:
                    app.state.physical_recovery_diagnostics = PhysicalRecoveryDiagnostics(gate, watcher, isolated=True)
                    with patch.dict(os.environ, {}, clear=True):
                        status, headers, response = asyncio.run(call('/robots/physical-recovery'))
                    self.assertEqual(status, 200)
                    self.assertEqual(headers[b'cache-control'], b'no-store')
                    self.assertEqual(response, diagnostic)
                    self.assertNotIn(controllers['physical-1'].session, json.dumps(response))
                finally:
                    app.state._state.clear()
                    app.state._state.update(previous)
            self.assertEqual(store.list_occupancy(), before)
            self.assertEqual(gate._blocked, {'T1', 'T2'})
            self.assertEqual(inbox.list_pending(), [])

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
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
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
            SimEventInbox(path, robot_tasks={'sim-placeholder': ('placeholder',)}, simulation_enabled=True).initialize()
            inbox = PhysicalEvidenceInbox(path, {'physical-1': ('T1',)}, isolated=True)
            state_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
            watcher = PhysicalEvidenceWatcher(gate, {'T1': ('physical-1', ctl.session)},
                                              client=state_client, host='127.0.0.1', port=port, isolated=True)
            watcher.start()
            cleanup.callback(watcher.stop)
            broker.terminate()
            broker.wait(timeout=5)
            until(lambda: not link.connected and link.disconnect_pending() and 'T1' in gate._blocked,
                  'both clients observed broker loss', 10, tick=lambda: ctl.step(now=5))
            watcher.stop()
            spawn()
            until(lambda: link.connected and link._reconcile_ready, 'robot reconnected', 15,
                  tick=lambda: ctl.step(now=5))
            state_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
            watcher = PhysicalEvidenceWatcher(gate, {'T1': ('physical-1', ctl.session)},
                                              client=state_client, host='127.0.0.1', port=port, isolated=True)
            watcher.start()
            cleanup.callback(watcher.stop)
            until(lambda: watcher._connected.is_set() and watcher._online.get('physical-1') is True,
                  'server watcher resubscribed', 10)
            state = {'robot_session': ctl.session, 'status': 'ERROR', 'error': 'link_lost',
                     'rejected': None, 'order_id': 'T1', 'seq': 1, 'map_id': 'm@v1',
                     'last_node': 'B', 'released_until': 'B'}
            link.publish_state(state)
            until(lambda: (watcher._latest.get('physical-1') or (None,))[0] == state, 'fresh state report', 5)
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
