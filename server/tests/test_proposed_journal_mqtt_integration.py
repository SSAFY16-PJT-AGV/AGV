"""Isolated proposed journal + MQTT tests; no real robot, state or broker.

The responder below is a test peer, and its response format is a team-unagreed
proposal. The journal has no transport or reservation control of its own.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import paho.mqtt.client as mqtt

from server.order_publisher import OrderPublisher, _payload
from server.proposed_command_journal import JournalConflict, ProposedCommandJournal
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment


_RESTART_SCRIPT = r'''
import json, sys, threading
import paho.mqtt.client as mqtt
from server.proposed_command_journal import JournalConflict, ProposedCommandJournal
from server.tests.test_proposed_journal_mqtt_integration import proposed_order, send_once
journal = ProposedCommandJournal.open_existing(sys.argv[1], simulation=True, expected_creation_id=sys.argv[2])
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='journal-restarted-process',
                     clean_session=True, reconnect_on_failure=False)
ready = threading.Event()
errors = []
def connected(_client, _user, _flags, reason, _properties):
    if reason.is_failure:
        errors.append('connection rejected')
    ready.set()
client.on_connect = connected
client.connect('127.0.0.1', int(sys.argv[3]), keepalive=10)
client.loop_start()
try:
    if not ready.wait(10) or errors:
        raise RuntimeError('isolated broker unavailable')
    try:
        send_once(journal, client, proposed_order())
    except JournalConflict:
        blocked = True
    else:
        blocked = False
    print(json.dumps({'blocked': blocked, 'state': journal.get('sim-r1', 'task-1', 1)['state']}), flush=True)
finally:
    client.disconnect()
    client.loop_stop()
'''


def proposed_order():
    return {'order_id': 'task-1', 'seq': 1, 'map_id': 'test@v1',
            'nodes': ['A', 'B'], 'released_until': 'B', 'action_at_end': {'type': 'load'}}


def send_once(journal, client, order):
    """Test-only gate: commit the attempt before using this publisher."""
    expected = _payload(order)
    preview = json.loads(expected)
    publisher = OrderPublisher(client, enabled=True)
    owner = object()
    publisher.claim(owner)
    publisher.validate('sim-r1', preview, owner=owner)
    wire = journal.mark_attempted('sim-r1', preview['order_id'], preview['seq'], expected_payload=expected)
    if journal.get('sim-r1', preview['order_id'], preview['seq'])['state'] != 'uncertain':
        raise AssertionError('attempt was not recorded before publish')
    outbound = json.loads(wire)
    if _payload(outbound) != wire:
        raise AssertionError('publisher serialization differs from recorded bytes')
    return publisher.publish('sim-r1', outbound, owner=owner)


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class ProposedJournalMqttIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cleanup = ExitStack()
        self.addCleanup(self.cleanup.close)
        folder = self.cleanup.enter_context(tempfile.TemporaryDirectory(prefix='journal-mqtt-'))
        self.root = Path(folder)
        self.port = free_port()
        ready = self.root / 'broker.ready'
        log = self.cleanup.enter_context((self.root / 'broker.log').open('w', encoding='utf-8'))
        env = isolated_environment(os.environ)
        env['PYTHONPATH'] = str(Path(os.environ['AMQTT_TEST_PATH']).resolve())
        process = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', BROKER,
                                    str(self.port), str(ready)], env=env, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.cleanup.callback(stop, process)
        wait_for_owned_broker(self, process, str(ready))
        self.path = self.root / 'journal.sqlite'
        self.journal = ProposedCommandJournal.create(self.path, simulation=True)
        self.commands = Queue()
        self.responses = Queue()
        self.robot = self._client('journal-test-peer', ['agv/sim-r1/order', 'journal-test/liveness'],
                                  self.commands)
        self.server = self._client('journal-test-server', 'agv/sim-r1/event', self.responses)

    def _client(self, client_id, subscription, queue):
        subscriptions = [subscription] if isinstance(subscription, str) else list(subscription)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                             clean_session=True, reconnect_on_failure=False)
        ready, errors = threading.Event(), []
        def connected(connection, _user, _flags, reason, _properties):
            if reason.is_failure:
                errors.append('connection rejected')
                ready.set()
                return
            result, _mid = connection.subscribe([(topic, 1) for topic in subscriptions])
            if result != mqtt.MQTT_ERR_SUCCESS:
                errors.append('subscription failed to queue')
                ready.set()
        def subscribed(_client, _user, _mid, reasons, _properties):
            if (len(reasons) != len(subscriptions)
                    or any(reason.is_failure or reason.value != 1 for reason in reasons)):
                errors.append('subscription rejected')
            ready.set()
        def received(_client, _user, message):
            queue.put((message.topic, bytes(message.payload), message.retain))
        client.on_connect, client.on_subscribe, client.on_message = connected, subscribed, received
        client.connect('127.0.0.1', self.port, keepalive=10)
        client.loop_start()
        self.cleanup.callback(self._close_client, client)
        self.assertTrue(ready.wait(10), 'isolated client subscription timed out')
        self.assertEqual(errors, [])
        return client

    @staticmethod
    def _close_client(client):
        client.disconnect()
        client.loop_stop()

    def _send_once(self, journal, client, order):
        info = send_once(journal, client, order)
        info.wait_for_publish(timeout=5)
        self.assertTrue(info.is_published(), 'broker PUBACK not observed')

    def _probe_robot(self):
        marker = os.urandom(16).hex().encode('ascii')
        info = self.robot.publish('journal-test/liveness', marker, qos=1, retain=False)
        info.wait_for_publish(timeout=5)
        self.assertTrue(info.is_published(), 'test peer probe was not acknowledged')
        self.assertEqual(self.commands.get(timeout=5), ('journal-test/liveness', marker, False))

    def _response(self, received_payload, *, outcome='accepted'):
        order = json.loads(received_payload)
        return {'event_id': 'event-1', 'robot_session': 'boot-1',
                'order_id': order['order_id'], 'seq': order['seq'], 'map_id': order['map_id'],
                'order_digest': hashlib.sha256(received_payload).hexdigest(), 'outcome': outcome}

    def _send_response(self, event):
        info = self.robot.publish('agv/sim-r1/event', json.dumps(event, separators=(',', ':')),
                                  qos=1, retain=False)
        info.wait_for_publish(timeout=5)
        self.assertTrue(info.is_published(), 'test peer PUBACK not observed')
        topic, payload, retained = self.responses.get(timeout=5)
        self.assertEqual(topic, 'agv/sim-r1/event')
        return json.loads(payload), retained

    def test_wire_order_and_duplicate_or_conflicting_responses(self):
        order = proposed_order()
        staged = self.journal.stage('sim-r1', 'boot-1', order)
        self.assertEqual(staged['state'], 'pending')
        self._send_once(self.journal, self.server, order)
        topic, wire, retained = self.commands.get(timeout=5)
        self.assertEqual(topic, 'agv/sim-r1/order')
        self.assertFalse(retained)
        self.assertEqual(json.loads(wire), order)
        self.assertEqual(hashlib.sha256(wire).hexdigest(), staged['digest'])

        event = self._response(wire)
        delivered, retained = self._send_response(event)
        self.assertEqual(self.journal.record_response('sim-r1', delivered, retained=retained), 'accepted')
        duplicate, retained = self._send_response(event)
        self.assertEqual(self.journal.record_response('sim-r1', duplicate, retained=retained), 'accepted')
        conflicting, retained = self._send_response(dict(event, outcome='rejected'))
        with self.assertRaises(JournalConflict):
            self.journal.record_response('sim-r1', conflicting, retained=retained)
        self.assertEqual(self.journal.get('sim-r1', 'task-1', 1)['state'], 'accepted')

    def test_different_order_with_same_identity_never_reaches_peer(self):
        order = proposed_order()
        self.journal.stage('sim-r1', 'boot-1', order)
        self._probe_robot()
        different = dict(order, action_at_end={'type': 'unload'})
        with self.assertRaises(JournalConflict):
            self._send_once(self.journal, self.server, different)
        self.assertEqual(self.journal.get('sim-r1', 'task-1', 1)['state'], 'pending')
        with self.assertRaises(Empty):
            self.commands.get(timeout=1)
        self._probe_robot()

    def test_mutating_input_during_attempt_cannot_change_published_order(self):
        order = proposed_order()
        staged = self.journal.stage('sim-r1', 'boot-1', order)
        original_attempt = self.journal.mark_attempted
        def mutate_input(*args, **kwargs):
            order['action_at_end']['type'] = 'unload'
            return original_attempt(*args, **kwargs)
        with patch.object(self.journal, 'mark_attempted', side_effect=mutate_input):
            self._send_once(self.journal, self.server, order)
        topic, wire, retained = self.commands.get(timeout=5)
        self.assertEqual((topic, retained), ('agv/sim-r1/order', False))
        self.assertEqual(wire, staged['payload'])
        self.assertEqual(json.loads(wire)['action_at_end'], {'type': 'load'})

    def test_lost_response_and_reopen_do_not_republish_through_test_gate(self):
        order = proposed_order()
        self.journal.stage('sim-r1', 'boot-1', order)
        self._send_once(self.journal, self.server, order)
        topic, wire, _retained = self.commands.get(timeout=5)
        self.assertEqual((topic, json.loads(wire)), ('agv/sim-r1/order', order))
        self._probe_robot()
        # The peer sends no response: PUBACK did not resolve robot acceptance.
        self.assertEqual(self.journal.get('sim-r1', 'task-1', 1)['state'], 'uncertain')
        self._close_client(self.server)
        env = isolated_environment(os.environ)
        env['PYTHONUTF8'] = '1'
        child = subprocess.run([sys.executable, '-c', _RESTART_SCRIPT, str(self.path),
                                self.journal.creation_id, str(self.port)],
                               cwd=Path(__file__).resolve().parents[2], env=env,
                               capture_output=True, text=True, encoding='utf-8', errors='replace',
                               timeout=25, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(child.returncode, 0, child.stdout + '\n' + child.stderr)
        self.assertEqual(json.loads(child.stdout.strip().splitlines()[-1]),
                         {'blocked': True, 'state': 'uncertain'})
        # The independently reopened journal fences the prior process object.
        with self.assertRaises(JournalConflict):
            self.journal.get('sim-r1', 'task-1', 1)
        with self.assertRaises(Empty):
            self.commands.get(timeout=1)
        self._probe_robot()


if __name__ == '__main__':
    unittest.main()
