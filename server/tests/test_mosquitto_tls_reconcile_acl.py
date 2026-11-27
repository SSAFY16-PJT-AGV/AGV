"""Test optional reconciliation ACLs with TLS, passwords and a local broker.

This never controls real hardware and verifies actual delivery, not PUBACK.
The team MqttLink also runs with an inert fake controller in a separate test.
"""
import os
import queue
import secrets
import socket
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path

import paho.mqtt.client as mqtt

from server.tests.test_mosquitto_tls_robot_acl import IsolatedTlsRobotAclTest
from server.mqtt_security import configure_mqtt_client


class IsolatedTlsReconcileAclTest(IsolatedTlsRobotAclTest):
    ISOLATED_RECONCILIATION = True
    # Baseline checks run on the baseline fixture separately. Repeating them
    # here after real team links have published retained state/conn messages
    # would conflate those retained observations with new publications.
    test_tls_password_and_distinct_robot_topic_permissions = None
    test_own_lwt_is_delivered_but_cross_robot_lwt_is_not = None
    test_plaintext_mqtt_cannot_establish_a_session = None
    test_bad_password_anonymous_and_wrong_ca_are_rejected = None
    test_tls_rejects_certificate_for_another_hostname = None

    def test_reconcile_topics_reject_cross_robot_reads_and_writes(self):
        monitor, reason = self.connect('observer')
        self.assertFalse(reason.is_failure)
        responses = self.subscribe(monitor, ['agv/+/reconcile/response'])
        robots = []
        for name in ('robot-1', 'robot-2'):
            robot, reason = self.connect(name)
            self.assertFalse(reason.is_failure)
            subscriptions = [f'agv/sim-r{i}/reconcile/{suffix}'
                             for i in (1, 2) for suffix in ('request', 'commit')]
            robots.append((robot, self.subscribe(robot, subscriptions)))
        for index, (robot, _messages) in enumerate(robots, 1):
            other = 3 - index
            self.publish(robot, f'agv/sim-r{other}/reconcile/response', secrets.token_bytes(12))
            self.publish(robot, f'agv/sim-r{index}/reconcile/request', secrets.token_bytes(12))
            barrier = (f'agv/sim-r{index}/reconcile/response', secrets.token_bytes(12))
            self.publish(robot, *barrier)
            self.assertEqual(responses.get(timeout=8), barrier)
        with self.assertRaises(queue.Empty):
            responses.get(timeout=.5)
        for index in (1, 2):
            for suffix in ('request', 'commit'):
                own = (f'agv/sim-r{index}/reconcile/{suffix}', secrets.token_bytes(12))
                self.publish(monitor, *own)
                self.assertEqual(robots[index - 1][1].get(timeout=8), own)
                with self.assertRaises(queue.Empty):
                    robots[2 - index][1].get(timeout=.3)

    @unittest.skipUnless(os.environ.get('AGV_FIELD_REPO'), 'set AGV_FIELD_REPO for actual team MQTT link')
    def test_team_link_receives_reconcile_topics_over_tls(self):
        repo = os.environ['AGV_FIELD_REPO']
        sys.path.insert(0, repo)
        self.addCleanup(lambda: sys.path.remove(repo))
        from agv.link.mqtt import MqttLink

        monitor, reason = self.connect('observer')
        self.assertFalse(reason.is_failure)
        links = []
        for index in (1, 2):
            link = MqttLink(f'sim-r{index}', host='127.0.0.1', port=self.port,
                            username=f'robot-{index}', password=self.passwords[f'robot-{index}'],
                            tls_ca=str(self.cert), reconcile_isolated=True,
                            reconcile_key=bytes([index]) * 32).start()
            self.addCleanup(link.stop)
            links.append(link)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not all(link.connected and link._reconcile_ready for link in links):
            time.sleep(.05)
        self.assertTrue(all(link.connected and link._reconcile_ready for link in links))
        for index, link in enumerate(links, 1):
            for suffix in ('request', 'commit'):
                payload = secrets.token_bytes(12)
                self.publish(monitor, f'agv/sim-r{index}/reconcile/{suffix}', payload)
                found = []
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and not found:
                    found = link.poll_reconcile()
                    if not found:
                        time.sleep(.05)
                self.assertEqual([(entry[2], entry[3]) for entry in found], [(suffix, payload)])
                self.assertEqual(links[2 - index].poll_reconcile(), [])

    @unittest.skipUnless(os.environ.get('AGV_FIELD_REPO'), 'set AGV_FIELD_REPO for actual team MQTT link')
    def test_team_reconcile_hold_exchange_through_authenticated_tls_broker(self):
        """Actual team protocol with fake motor and stopped-position evidence."""
        from server.allocation_store import AllocationStore
        from server.physical_evidence_observer import PhysicalEvidenceInbox, PhysicalEvidenceWatcher
        from server.physical_reconcile_mqtt import isolated_gate_reconciliation
        from server.recovery import RecoveryGate
        from sim.event_inbox import SimEventInbox
        repo = os.environ['AGV_FIELD_REPO']
        sys.path.insert(0, repo)
        self.addCleanup(lambda: sys.path.remove(repo))
        from agv.link.mqtt import MqttLink
        from agv.tests.test_link_disconnect_fence import _controller, _payload

        def wait_for(predicate, timeout=15, tick=None):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if tick:
                    tick()
                if predicate():
                    return
                time.sleep(.05)
            self.fail('isolated TLS exchange timed out')

        def server_client():
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
            configure_mqtt_client(client, 'observer', self.passwords['observer'], str(self.cert))
            return client

        with tempfile.TemporaryDirectory(prefix='agv-tls-reconcile-') as tmp, ExitStack() as cleanup:
            link = MqttLink('physical-1', host='127.0.0.1', port=self.port,
                            username='robot-1', password=self.passwords['robot-1'],
                            tls_ca=str(self.cert), reconcile_isolated=True,
                            reconcile_key=b'A' * 32).start()
            cleanup.callback(link.stop)
            wait_for(lambda: link.connected and link._reconcile_ready)
            ctl, runner = _controller(link)
            ctl._handle(_payload())
            runner.state, runner.hold_reason, ctl.nav.at_node = 'hold', 'nav_hold', 'B'
            link.publish_event_once = lambda _event: True
            ctl._watch_stop()
            self.assertIsNotNone(ctl._reconcile_event)
            path = Path(tmp) / 'fleet.sqlite3'
            store = AllocationStore(path)
            store.initialize()
            store.create_task('T1')
            resources = [('node', 'A'), ('node', 'B'), ('edge', '["A","B"]')]
            allocation = store.allocate('T1', 'physical-1', 'm@v1', ['A', 'B'], 'B', resources,
                                        {'type': 'none'}, map_fingerprint='a' * 64)
            record = store.get_order('T1')
            store.mark_attempted('T1', record['payload'], allocation, resources)
            gate = RecoveryGate(path)
            gate.initialize()
            SimEventInbox(path, robot_tasks={'sim-placeholder': ('placeholder',)}, simulation_enabled=True).initialize()
            inbox = PhysicalEvidenceInbox(path, {'physical-1': ('T1',)}, isolated=True)

            link.c.socket().shutdown(socket.SHUT_RDWR)
            wait_for(lambda: link.disconnect_pending(), tick=lambda: ctl.step(now=5))
            wait_for(lambda: link.connected and link._reconcile_ready, tick=lambda: ctl.step(now=5))
            watcher = PhysicalEvidenceWatcher(gate, {'T1': ('physical-1', ctl.session)},
                client=server_client(), host='127.0.0.1', port=self.port, isolated=True)
            watcher.start()
            cleanup.callback(watcher.stop)
            wait_for(lambda: watcher._online.get('physical-1') is True)
            state = {'robot_session': ctl.session, 'status': 'ERROR', 'error': 'link_lost',
                     'rejected': None, 'order_id': 'T1', 'seq': 1, 'map_id': 'm@v1',
                     'last_node': 'B', 'released_until': 'B'}
            link.publish_state(state)
            wait_for(lambda: (watcher._latest.get('physical-1') or (None,))[0] == state)
            before = store.list_occupancy()
            with isolated_gate_reconciliation(gate, inbox, watcher, {'physical-1': b'A' * 32},
                    port=self.port, isolated=True, client=server_client()) as bridge:
                nonce = bridge.request('physical-1', 'T1', ctl.session)
                wait_for(lambda: bridge.result_for(nonce) is not None, tick=lambda: ctl.step(now=5))
                result = bridge.result_for(nonce)
                self.assertEqual(result['status'], 'signed_ack_recorded')
                self.assertEqual(result['event_state'], 'not_observed')
                self.assertFalse(result['current_hold_proven'])
                self.assertFalse(result['motion_authorized'])
            self.assertEqual(store.list_occupancy(), before)
            self.assertIn('T1', gate._blocked)
            self.assertTrue(link.disconnect_pending())
            self.assertEqual((runner.state, runner.resume_count), ('hold', 1))


if __name__ == '__main__':
    unittest.main()
