"""Isolated recovery exchange never releases the robot or its reservations."""
import copy
from contextlib import closing
import hashlib
import hmac
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.allocation_store import AllocationStore
from server.recovery import RecoveryGate
from sim.event_inbox import SimEventInbox
from server.physical_evidence_observer import PhysicalEvidenceWatcher, PhysicalEvidenceInbox
from server.physical_reconcile import PhysicalReconciler

KEY = b'A' * 32
DOMAIN = b'AGV-RECONCILE-V1\0'


def sign(message, key=KEY):
    value = dict(message)
    value.pop('mac', None)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    value['mac'] = hmac.new(key, DOMAIN + raw, hashlib.sha256).hexdigest()
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()


class PhysicalReconcileExchangeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'state.sqlite3'
        self.store = AllocationStore(self.path)
        self.store.initialize()
        self.store.create_task('T1')
        self.resources = [('node', 'A'), ('node', 'B'), ('edge', '["A","B"]')]
        allocated = self.store.allocate('T1', 'physical-1', 'layout@v1', ['A', 'B'], 'B',
            self.resources, {'type': 'none'}, map_fingerprint='a' * 64)
        record = self.store.get_order('T1')
        self.order = json.loads(record['payload'])
        self.store.mark_attempted('T1', record['payload'], allocated, self.resources)
        self.time = [0.0]
        self.gate = RecoveryGate(self.path, clock=lambda: self.time[0])
        self.gate.initialize()
        self.gate.invalidate_many(('T1',))
        self.watcher = PhysicalEvidenceWatcher(self.gate, {'T1': ('physical-1', 'boot-1')},
                                               client=SimpleNamespace(), host='127.0.0.1', port=1,
                                               clock=lambda: self.time[0], isolated=True)
        self.watcher._mqtt_started = True
        self.watcher._connected.set()
        self.watcher._subscription_done.set()
        self.watcher._online['physical-1'] = True
        self.state = {'robot_session': 'boot-1', 'status': 'ERROR', 'error': 'event_unconfirmed',
                      'rejected': None, 'order_id': 'T1', 'seq': 1, 'map_id': 'layout@v1',
                      'last_node': 'B', 'released_until': 'B'}
        self.watcher._latest['physical-1'] = (dict(self.state), 0.0, 0, 1)
        SimEventInbox(self.path, robot_tasks={'sim-placeholder': ('placeholder',)},
                      simulation_enabled=True).initialize()
        self.inbox = PhysicalEvidenceInbox(self.path, {'physical-1': ('T1',)}, isolated=True)
        self.service = PhysicalReconciler(self.gate, self.inbox,
            {'physical-1': KEY}, isolated=True, clock=lambda: self.time[0])
        self.event = {'type': 'rear_clear', 'event_id': 'event-1', 'robot_session': 'boot-1',
            'order_id': 'T1', 'seq': 1, 'map_id': 'layout@v1', 'stopped_at': 'B',
            'cleared_resources': [['node', 'A'], ['edge', '["A","B"]']]}

    def snapshot(self, probe):
        data = {k: v for k, v in probe.items() if k != 'mac'}
        data.update(kind='snapshot', event_id=self.event['event_id'], event_digest=digest(self.event),
                    event=copy.deepcopy(self.event), connection_generation=2, robot_nonce='f' * 64)
        return sign(data)

    def ack(self, commit):
        data = dict(commit)
        data['kind'] = 'commit_ack'
        return sign(data)

    def insert_event(self, value):
        # Simulate an already durable event without exposing a physical-event
        # writing or progression method through the observation-only view.
        from sim.event_inbox import _event
        event, body, digest_value = _event(value)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute('INSERT INTO sim_event_inbox '
                           '(robot_id,event_id,task_id,robot_session,seq,payload,digest,state) '
                           "VALUES(?,?,?,?,?,?,?,'pending')",
                           ('physical-1', event['event_id'], event['order_id'],
                            event['robot_session'], event['seq'], body, digest_value))

    def test_ordinary_gate_and_inbox_still_reject_physical_execution(self):
        from server.recovery import RecoveryError
        with self.assertRaises(RecoveryError):
            self.gate.begin('T1', 'boot-1')
        with self.assertRaises(ValueError):
            SimEventInbox(self.path, robot_tasks={'physical-1': ('T1',)}, simulation_enabled=True)
        from server.sim_runtime import SimRuntime
        with self.assertRaises(ValueError):
            SimRuntime(self.path, {}, {'physical-1': {'T1': 'boot-1'}},
                       host='127.0.0.1', port=1883, client_id='disabled-physical')
        self.assertFalse(hasattr(self.inbox, 'store'))
        self.assertFalse(hasattr(self.inbox, 'apply'))

    def test_stopping_watcher_invalidates_evidence_and_allows_new_observer_without_unblocking(self):
        self.watcher._client = SimpleNamespace(disconnect=lambda: None, loop_stop=lambda: None)
        self.watcher.stop()
        self.assertIn('T1', self.gate._blocked)
        self.assertIsNone(self.gate._physical_watcher)
        with self.assertRaises(ValueError):
            self.service.begin('physical-1', 'T1', 'boot-1')
        replacement = PhysicalEvidenceWatcher(self.gate, {'T1': ('physical-1', 'boot-1')},
                                               client=SimpleNamespace(), host='127.0.0.1', port=1,
                                               clock=lambda: self.time[0], isolated=True)
        self.assertIs(self.gate._physical_watcher, replacement)
        self.assertIn('T1', self.gate._blocked)
        with self.assertRaises(ValueError):
            self.service.begin('physical-1', 'T1', 'boot-1')

    def test_absent_event_is_nonexecutable_evidence_and_latch_and_occupancy_survive(self):
        before = self.store.list_occupancy()
        probe = self.service.begin('physical-1', 'T1', 'boot-1')
        self.assertIsNone(probe['event_id'])
        commit = self.service.accept_snapshot(self.snapshot(probe))
        self.assertEqual(commit['decision'], 'hold_confirmed')
        self.assertIsNone(self.inbox.get('physical-1', 'event-1'))
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertIn('T1', self.gate._blocked)
        result = self.service.accept_ack(self.ack(commit))
        self.assertEqual((result['status'], result['event_state']), ('signed_ack_recorded', 'not_observed'))
        self.assertIn('T1', self.gate._blocked)
        self.assertEqual(self.store.list_occupancy(), before)

    def test_duplicate_response_and_ack_are_idempotent_without_deadline_extension(self):
        probe = self.service.begin('physical-1', 'T1', 'boot-1')
        snapshot = self.snapshot(probe)
        commit = self.service.accept_snapshot(snapshot)
        self.assertEqual(self.service.accept_snapshot(snapshot), commit)
        ack = self.ack(commit)
        self.assertEqual(self.service.accept_ack(ack), self.service.accept_ack(ack))
        self.assertEqual(len(self.inbox.list_pending()), 0)

    def test_tampering_expiry_wrong_session_and_conflicting_event_fail_closed(self):
        probe = self.service.begin('physical-1', 'T1', 'boot-1')
        item = self.snapshot(probe)
        item['stopped_at'] = 'A'
        with self.assertRaises(ValueError): self.service.accept_snapshot(item)
        item = self.snapshot(probe)
        item['robot_session'] = 'other'
        with self.assertRaises(ValueError): self.service.accept_snapshot(sign(item))
        self.time[0] = 11.0
        with self.assertRaises(ValueError): self.service.accept_snapshot(self.snapshot(probe))
        self.assertIn('T1', self.gate._blocked)
        self.assertEqual(self.inbox.list_pending(), [])

    def test_restarted_server_epoch_rejects_old_response(self):
        from server.physical_reconcile import PhysicalReconciler
        probe = self.service.begin('physical-1', 'T1', 'boot-1')
        other = RecoveryGate(self.path, clock=lambda: self.time[0])
        other.initialize()
        with self.assertRaises(ValueError):
            PhysicalReconciler(other, self.inbox, {'physical-1': KEY}, isolated=True,
                                         clock=lambda: self.time[0])
        self.watcher = PhysicalEvidenceWatcher(other, {'T1': ('physical-1', 'boot-1')},
                                               client=SimpleNamespace(), host='127.0.0.1', port=1,
                                               clock=lambda: self.time[0], isolated=True)
        self.watcher._mqtt_started = True
        self.watcher._connected.set()
        self.watcher._subscription_done.set()
        self.watcher._online['physical-1'] = True
        self.watcher._latest['physical-1'] = (dict(self.state), 0.0, 0, 1)
        replacement = PhysicalReconciler(other, self.inbox, {'physical-1': KEY}, isolated=True,
                                         clock=lambda: self.time[0])
        with self.assertRaises(ValueError): replacement.accept_snapshot(self.snapshot(probe))
        self.assertEqual(self.inbox.list_pending(), [])

    def test_changed_session_or_watcher_generation_cannot_confirm(self):
        probe = self.service.begin('physical-1', 'T1', 'boot-1')
        self.watcher._connection_generation += 1
        self.watcher._latest['physical-1'] = (dict(self.state), 0.0, 1, 2)
        with self.assertRaises(ValueError): self.service.accept_snapshot(self.snapshot(probe))
        self.assertEqual(self.inbox.list_pending(), [])
        with self.assertRaises(ValueError): self.service.begin('physical-1', 'T1', 'different-session')

    def test_different_event_for_same_visit_is_rejected_across_nonces(self):
        first = self.service.begin('physical-1', 'T1', 'boot-1')
        self.service.accept_snapshot(self.snapshot(first))
        second = self.service.begin('physical-1', 'T1', 'boot-1')
        self.event['event_id'] = 'different-event'
        with self.assertRaises(ValueError): self.service.accept_snapshot(self.snapshot(second))
        self.assertEqual(self.inbox.list_pending(), [])

    def test_existing_matching_event_is_observed_without_applying_or_replaying(self):
        self.insert_event(self.event)
        probe = self.service.begin('physical-1', 'T1', 'boot-1',
                                   event_id='event-1', event_digest=digest(self.event))
        commit = self.service.accept_snapshot(self.snapshot(probe))
        result = self.service.accept_ack(self.ack(commit))
        self.assertEqual(result['event_state'], 'pending')
        self.assertFalse(result['motion_authorized'])
        self.assertEqual(self.inbox.get('physical-1', 'event-1')['state'], 'pending')
        self.assertIn('T1', self.gate._blocked)

    def test_racing_mismatched_inbox_event_rejects_confirmation(self):
        probe = self.service.begin('physical-1', 'T1', 'boot-1')
        commit = self.service.accept_snapshot(self.snapshot(probe))
        conflict = dict(self.event, event_id='other-event')
        self.insert_event(conflict)
        with self.assertRaises(ValueError): self.service.accept_ack(self.ack(commit))
        self.assertIn('T1', self.gate._blocked)
        self.assertEqual(self.inbox.get('physical-1', 'other-event')['state'], 'pending')

    def test_gate_generation_or_connection_loss_rejects_ack(self):
        probe = self.service.begin('physical-1', 'T1', 'boot-1')
        commit = self.service.accept_snapshot(self.snapshot(probe))
        self.gate.invalidate_many(('T1',))
        with self.assertRaises(ValueError): self.service.accept_ack(self.ack(commit))
        self.assertIn('T1', self.gate._blocked)

    def test_committed_nonce_cannot_be_sent_after_snapshot_changes(self):
        probe = self.service.begin('physical-1', 'T1', 'boot-1')
        self.service.accept_snapshot(self.snapshot(probe))
        self.gate.invalidate_many(('T1',))
        with self.assertRaises(ValueError):
            with self.service.publication(probe['nonce'], 'commit'):
                self.fail('stale commit must not be queued')

    def test_load_unload_uncertain_action_is_never_accepted_as_hold(self):
        for action in ({'type': 'load'}, {'type': 'unload'}, {'type': 'none', 'extra': True}):
            with self.subTest(action=action):
                order = dict(self.order, action_at_end=action)
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('UPDATE allocation_outbox SET payload=? WHERE task_id=?',
                               (json.dumps(order, separators=(',', ':')), 'T1'))
                with self.assertRaises(ValueError): self.service.begin('physical-1', 'T1', 'boot-1')
                self.assertIn('T1', self.gate._blocked)

    def test_live_state_must_match_stopped_order_at_every_step(self):
        for change in ({'status': 'MOVING'}, {'status': 'ERROR', 'error': None},
                       {'status': 'ERROR', 'error': 'nav_off_route'}, {'last_node': 'A'},
                       {'seq': 2}, {'seq': True}, {'map_id': 'other@v1'}, {'order_id': 'other'},
                       {'released_until': 'A'}, {'rejected': {'reason': 'error'}}):
            with self.subTest(change=change):
                self.watcher._latest['physical-1'] = (dict(self.state, **change), 0.0, 0, 1)
                with self.assertRaises(ValueError): self.service.begin('physical-1', 'T1', 'boot-1')
        self.watcher._latest['physical-1'] = (dict(self.state), 0.0, 0, 1)
        probe = self.service.begin('physical-1', 'T1', 'boot-1')
        self.watcher._latest['physical-1'] = (dict(self.state, status='MOVING', error=None), 0.0, 0, 2)
        with self.assertRaises(ValueError): self.service.accept_snapshot(self.snapshot(probe))

    def test_expiry_immediately_before_commit_rolls_back_snapshot_and_ack(self):
        probe = self.service.begin('physical-1', 'T1', 'boot-1')
        original = self.service._record
        def expire_after_updated(db, nonce):
            row = original(db, nonce)
            if row[6] == 'commit_sent':
                self.time[0] = 11.0
            return row
        self.service._record = expire_after_updated
        with self.assertRaises(ValueError): self.service.accept_snapshot(self.snapshot(probe))
        self.service._record = original
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT state,event_json FROM physical_reconcile WHERE nonce=?',
                                        (probe['nonce'],)).fetchone(), ('probed', None))
        self.time[0] = 0.0
        commit = self.service.accept_snapshot(self.snapshot(probe))
        def expire_after_ack_update(db, nonce):
            row = original(db, nonce)
            if row[6] == 'confirmed':
                self.time[0] = 11.0
            return row
        self.service._record = expire_after_ack_update
        with self.assertRaises(ValueError): self.service.accept_ack(self.ack(commit))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT state,ack_hash FROM physical_reconcile WHERE nonce=?',
                                        (probe['nonce'],)).fetchone(), ('commit_sent', None))
        self.assertIn('T1', self.gate._blocked)


if __name__ == '__main__': unittest.main()
