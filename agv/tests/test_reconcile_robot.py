# -*- coding: utf-8 -*-
"""Isolated reconciliation holds motion; never tests real motors or a broker."""
import hashlib
import hmac
import json
import unittest

from agv.link.reconcile import ReconcileRobot, canonical


KEY = b'k' * 32


def sign(message):
    result = dict(message)
    result['mac'] = hmac.new(KEY, b'AGV-RECONCILE-V1\0' + canonical(message), hashlib.sha256).hexdigest()
    return result


class ReconcileRobotTests(unittest.TestCase):
    def setUp(self):
        self.time = [20.0]
        self.machine = ReconcileRobot('physical-1', KEY, clock=lambda: self.time[0],
                                      random_nonce=lambda: 'c' * 64)
        self.event = {'type': 'rear_clear', 'event_id': 'boot-1', 'robot_session': 'boot',
                      'order_id': 'T1', 'seq': 1, 'map_id': 'map@v1',
                      'stopped_at': 'B', 'cleared_resources': [['node', 'A']]}
        self.local = {'robot_session': 'boot', 'map_id': 'map@v1', 'order_id': 'T1',
                      'seq': 1, 'order_digest': 'd' * 64, 'release_index': 1,
                      'stopped_at': 'B', 'event': self.event, 'safe_hold': True}
        self.probe = sign({'v': 1, 'kind': 'probe', 'nonce': 'a' * 64, 'server_epoch': 'b' * 64,
                           'robot_id': 'physical-1', 'robot_session': 'boot', 'order_id': 'T1',
                           'seq': 1, 'order_digest': 'd' * 64, 'map_id': 'map@v1',
                           'release_index': 1, 'stopped_at': 'B', 'event_id': None,
                           'event_digest': None})

    def test_snapshot_commit_and_retry_never_authorizes_motion(self):
        snapshot = self.machine.probe(self.probe, self.local, 7)
        self.assertEqual(snapshot['event'], self.event)
        self.assertEqual(snapshot['robot_nonce'], 'c' * 64)
        self.assertEqual(snapshot['event_digest'], hashlib.sha256(canonical(self.event)).hexdigest())
        duplicate = self.machine.probe(self.probe, self.local, 7)
        self.assertEqual(snapshot, duplicate)
        commit = {k: v for k, v in snapshot.items() if k not in ('mac', 'event')}
        commit.update(kind='commit', decision='hold_confirmed')
        commit = sign(commit)
        ack = self.machine.commit(commit, self.local, 7)
        self.assertEqual(ack['kind'], 'commit_ack')
        self.assertEqual(ack, self.machine.commit(commit, self.local, 7))

    def test_expired_changed_generation_or_wrong_signature_stays_held(self):
        snap = self.machine.probe(self.probe, self.local, 7)
        commit = {k: v for k, v in snap.items() if k not in ('mac', 'event')}
        commit.update(kind='commit', decision='hold_confirmed')
        commit = sign(commit)
        self.assertIsNone(self.machine.commit(commit, self.local, 8))
        self.time[0] += 11
        self.assertIsNone(self.machine.commit(commit, self.local, 7))
        forged = dict(self.probe, mac='0' * 64)
        self.assertIsNone(self.machine.probe(forged, self.local, 7))

    def test_wrong_location_session_action_or_missing_event_is_rejected(self):
        for change in ({'safe_hold': False}, {'stopped_at': 'C'}, {'robot_session': 'new'},
                       {'order_digest': 'e' * 64}, {'event': None}, {'release_index': 2}):
            with self.subTest(change=change):
                local = dict(self.local, **change)
                self.assertIsNone(self.machine.probe(self.probe, local, 7))

    def test_changed_hold_error_or_event_cannot_commit(self):
        snapshot = self.machine.probe(self.probe, dict(self.local, error_reason='link_lost'), 7)
        commit = {k: v for k, v in snapshot.items() if k not in ('mac', 'event')}
        commit.update(kind='commit', decision='hold_confirmed')
        commit = sign(commit)
        self.assertIsNone(self.machine.commit(commit, dict(self.local, error_reason='event_unconfirmed'), 7))
        altered = dict(self.event, cleared_resources=[])
        self.assertIsNone(self.machine.commit(commit, dict(self.local, error_reason='link_lost', event=altered), 7))

    def test_duplicate_probe_cannot_extend_original_deadline(self):
        self.machine.probe(self.probe, self.local, 7)
        self.time[0] += 9
        self.assertIsNotNone(self.machine.probe(self.probe, self.local, 7))
        self.time[0] += 1
        self.assertIsNone(self.machine.probe(self.probe, self.local, 7))

    def test_replay_nonce_with_new_signed_content_is_rejected(self):
        self.machine.probe(self.probe, self.local, 7)
        body = {k: v for k, v in self.probe.items() if k != 'mac'}
        body['server_epoch'] = 'e' * 64
        self.assertIsNone(self.machine.probe(sign(body), self.local, 7))


if __name__ == '__main__':
    unittest.main()
