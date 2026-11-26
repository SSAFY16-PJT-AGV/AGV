"""Physical observation retains the existing runtime's fail-closed gate."""
import json
from types import SimpleNamespace
import unittest

from server.physical_evidence_observer import PhysicalEvidenceWatcher
from server.physical_reconcile_mqtt import isolated_gate_reconciliation
from server.tests import test_physical_observation_exchange as fixtures


class PhysicalEvidenceObserverTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PhysicalReconcileExchangeTests('test_ordinary_gate_and_inbox_still_reject_physical_execution')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.watcher = self.fixture.watcher

    def message(self, topic, payload, *, retain=False, qos=0, generation=0):
        self.watcher._on_message(None, None, SimpleNamespace(topic=topic, payload=payload,
                                 retain=retain, qos=qos), generation)

    def test_retained_or_old_generation_state_cannot_replace_live_state(self):
        previous = self.watcher._latest['physical-1']
        self.message('agv/physical-1/state', json.dumps(dict(self.fixture.state, last_node='X')).encode(), retain=True)
        self.message('agv/physical-1/state', json.dumps(dict(self.fixture.state, last_node='X')).encode(), generation=9)
        self.assertEqual(self.watcher._latest['physical-1'], previous)

    def test_invalid_session_invalidates_the_observation(self):
        self.message('agv/physical-1/state', json.dumps(dict(self.fixture.state, robot_session='wrong')).encode())
        self.assertNotIn('physical-1', self.watcher._latest)
        with self.assertRaises(ValueError):
            self.fixture.service.begin('physical-1', 'T1', 'boot-1')
        self.assertIn('T1', self.fixture.gate._blocked)

    def test_invalid_connection_message_requires_new_online_before_state(self):
        self.message('agv/physical-1/conn', b'not-a-status', qos=1)
        self.assertNotIn('physical-1', self.watcher._latest)
        self.assertIsNot(self.watcher._online.get('physical-1'), True)
        self.message('agv/physical-1/state', json.dumps(self.fixture.state).encode())
        self.assertNotIn('physical-1', self.watcher._latest)
        self.message('agv/physical-1/conn', b'online', qos=1)
        self.message('agv/physical-1/state', json.dumps(self.fixture.state).encode())
        self.assertIn('physical-1', self.watcher._latest)

    def test_disconnect_invalidates_report_and_does_not_release_gate(self):
        self.watcher._on_disconnect(None, None, None, None, None, 0)
        self.assertFalse(self.watcher._connected.is_set())
        self.assertNotIn('physical-1', self.watcher._latest)
        self.assertIn('T1', self.fixture.gate._blocked)
        self.assertFalse(hasattr(self.watcher, '_run'))
        self.assertFalse(hasattr(self.watcher, 'bind'))

    def test_non_loopback_and_automatic_reconnect_are_rejected(self):
        gate = self.fixture.gate
        for kwargs in ({'host': '192.0.2.1', 'client': SimpleNamespace()},
                       {'host': '127.0.0.1', 'client': SimpleNamespace(_reconnect_on_failure=True)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PhysicalEvidenceWatcher(gate, {'T1': ('physical-1', 'boot-1')},
                                        port=1883, isolated=True, **kwargs)

    def test_robot_identifier_cannot_escape_its_mqtt_topic(self):
        gate = self.fixture.gate
        for robot in ('physical-+', 'physical-#', 'physical-one/other'):
            with self.subTest(robot=robot), self.assertRaisesRegex(ValueError, 'disallowed'):
                PhysicalEvidenceWatcher(gate, {'T1': (robot, 'boot-1')}, host='127.0.0.1',
                                        client=SimpleNamespace(), port=1883, isolated=True)

    def test_gate_context_rejects_different_broker_or_nonisolated_use(self):
        f = self.fixture
        for kwargs in ({'port': 2, 'isolated': True}, {'port': 1, 'isolated': False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                with isolated_gate_reconciliation(f.gate, f.inbox, f.watcher,
                                                  {'physical-1': fixtures.KEY}, **kwargs):
                    self.fail('invalid observer was attached')
        self.assertIn('T1', f.gate._blocked)


if __name__ == '__main__':
    unittest.main()
