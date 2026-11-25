"""Malformed live physical state must revoke evidence; no broker or hardware."""
import json
import unittest
from types import SimpleNamespace

from server.physical_state_proof import MAX_STATE_BYTES
from server.recovery import RecoveryError
from server.reproductions import physical_safety_race as reproduction


class MalformedPhysicalStateTests(unittest.TestCase):
    def make_fixture(self):
        f = reproduction.SafetyWindowTests('test_moving_state_must_block_automatic_receiver_progress')
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    @staticmethod
    def require(f):
        with f.gate.transaction() as db:
            return f.gate.require_reconciled(
                db, 'T1', 'boot-1', expected_seq=1, expected_stopped_at='N3')

    @staticmethod
    def send(f, payload, *, retain=False, topic='agv/physical-1/state', generation=None):
        message = SimpleNamespace(topic=topic, payload=payload, retain=retain, qos=0)
        f.watcher._on_message(None, None, message, _generation=generation)

    def test_malformed_live_state_revokes_only_target_robot_and_keeps_occupancy(self):
        cases = [
            ('missing session', b'{}'),
            ('null session', b'{"robot_session":null}'),
            ('boolean session', b'{"robot_session":false}'),
            ('empty session', b'{"robot_session":""}'),
            ('non-object JSON', b'[]'),
            ('invalid JSON', b'{'),
            ('invalid UTF-8', b'\xff'),
            ('oversized payload', b' ' * (MAX_STATE_BYTES + 1)),
            ('non-bytes payload', 'not bytes'),
        ]
        for name, payload in cases:
            with self.subTest(name=name):
                f = self.make_fixture()
                self.assertTrue(self.require(f))
                before = f.store.list_occupancy()
                self.send(f, payload)
                self.assertIn('physical-1', f.watcher._invalidated)
                self.send(f, json.dumps(f.state).encode('utf-8'))
                with self.assertRaises(RecoveryError):
                    self.require(f)
                self.assertEqual(f.store.list_occupancy(), before)

    def test_retained_malformed_state_does_not_revoke_live_proof(self):
        f = self.make_fixture()
        self.send(f, b'bad json', retain=True)
        self.assertNotIn('physical-1', f.watcher._invalidated)
        self.assertTrue(self.require(f))

    def test_other_topic_and_obsolete_callback_do_not_revoke_current_proof(self):
        f = self.make_fixture()
        self.send(f, b'bad json', topic='agv/another-robot/state')
        self.send(f, b'bad json', generation=f.watcher._connection_generation - 1)
        self.assertNotIn('physical-1', f.watcher._invalidated)
        self.assertTrue(self.require(f))

    def test_malformed_live_state_during_discovery_discards_prior_sample(self):
        f = self.make_fixture()
        f.watcher._runtime = None
        self.send(f, b'{')
        with self.assertRaisesRegex(RuntimeError, 'physical-1'):
            f.watcher.wait_sessions(0)
