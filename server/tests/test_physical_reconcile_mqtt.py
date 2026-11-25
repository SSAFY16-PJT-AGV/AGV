"""MQTT routing cannot make evidence executable or authorize robot motion."""
import json
from types import SimpleNamespace

from server.physical_reconcile_mqtt import PhysicalReconcileMqtt
from server.tests.test_physical_reconcile_exchange import PhysicalReconcileExchangeTests


class FakeClient:
    _reconnect_on_failure = False
    def __init__(self): self.sent = []
    def publish(self, topic, payload, qos, retain):
        self.sent.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=0)


class ReconcileMqttTests(PhysicalReconcileExchangeTests):
    def test_separate_topics_route_valid_signed_exchange_only(self):
        client = FakeClient()
        bridge = PhysicalReconcileMqtt(self.service, port=1883, client=client, isolated=True)
        bridge._ready.set()
        nonce = bridge.request('physical-1', 'T1', 'boot-1')
        topic, payload, qos, retained = client.sent[0]
        self.assertEqual((topic, qos, retained), ('agv/physical-1/reconcile/request', 1, False))
        probe = json.loads(payload)
        self.assertEqual(nonce, probe['nonce'])
        snapshot = self.snapshot(probe)
        msg = SimpleNamespace(topic='agv/physical-1/reconcile/response', qos=1, retain=False,
                              payload=json.dumps(snapshot).encode())
        bridge._message(client, None, msg)
        self.assertIsNone(bridge.last_error)
        topic, payload, qos, retained = client.sent[1]
        self.assertEqual((topic, qos, retained), ('agv/physical-1/reconcile/commit', 1, False))
        commit = json.loads(payload)
        msg.payload = json.dumps(self.ack(commit)).encode()
        bridge._message(client, None, msg)
        self.assertEqual(bridge.last_result['status'], 'signed_ack_recorded')
        self.assertFalse(bridge.last_result['current_hold_proven'])
        self.assertFalse(bridge.last_result['motion_authorized'])
        self.assertIsNone(self.inbox.get('physical-1', 'event-1'))
        self.assertIn('T1', self.gate._blocked)

    def test_retained_and_wrong_topic_cannot_trigger_a_commit(self):
        client = FakeClient()
        bridge = PhysicalReconcileMqtt(self.service, port=1883, client=client, isolated=True)
        bridge._ready.set()
        bridge.request('physical-1', 'T1', 'boot-1')
        probe = json.loads(client.sent[0][1])
        snapshot = self.snapshot(probe)
        msg = SimpleNamespace(topic='agv/physical-1/reconcile/response', qos=1, retain=True,
                              payload=json.dumps(snapshot).encode())
        bridge._message(client, None, msg)
        self.assertEqual(len(client.sent), 1)
        msg.retain = False
        msg.topic = 'agv/physical-2/reconcile/response'
        bridge._message(client, None, msg)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(self.inbox.list_pending(), [])
