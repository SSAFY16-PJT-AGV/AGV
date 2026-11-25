"""Isolated physical state proof fencing; this does not enable physical runtime."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.allocation_store import AllocationStore
from server.physical_state_proof import PhysicalStateWatcher
from server.recovery import RecoveryError, RecoveryGate
from server.sim_runtime import SimRuntime


class PhysicalInternalProofTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'db.sqlite3'
        self.store = AllocationStore(self.path)
        self.store.initialize()
        self.store.create_task('T1')
        resources = [('node', 'A'), ('node', 'B'), ('edge', '["A","B"]')]
        allocation = self.store.allocate('T1', 'physical-1', 'layout@v1', ['A', 'B'], 'B',
                                         resources, {'type': 'none'}, map_fingerprint='a' * 64)
        payload = self.store.get_order('T1')['payload']
        self.store.mark_attempted('T1', payload, allocation, resources)
        self.now = [0.0]
        self.gate = RecoveryGate(self.path, clock=lambda: self.now[0], ttl=5)
        self.gate.initialize()
        runtime = self.runtime = SimRuntime.__new__(SimRuntime)
        runtime._lock = threading.RLock()
        runtime.receiver = SimpleNamespace(_dispatch_lock=threading.RLock())
        runtime._ready = lambda: None
        runtime._tasks = {'T1': ('physical-1', 'session')}
        runtime._field_simulation = False
        runtime.path = str(self.path)
        runtime.gate = self.gate
        watcher = self.watcher = PhysicalStateWatcher(
            ['physical-1'], client=SimpleNamespace(), host='127.0.0.1', port=1,
            clock=lambda: self.now[0], log=lambda _message: None)
        watcher._runtime = runtime
        watcher._sessions = {'physical-1': 'session'}
        watcher._mqtt_started = True
        watcher._connected.set()
        watcher._subscription_done.set()
        watcher._online['physical-1'] = True
        runtime.physical_watcher = watcher
        self.gate.bind_physical_watcher(watcher)
        self.state = {'robot_session': 'session', 'status': 'WAITING', 'order_id': 'T1',
                      'seq': 1, 'last_node': 'B', 'released_until': 'B', 'map_id': 'layout@v1',
                      'error': None, 'rejected': None}
        with watcher._lock:
            watcher._state_serial = 1
            watcher._latest['physical-1'] = (dict(self.state), 0.0, 0, 1)
        self.token = (0, 1)

    def prove(self):
        return self.runtime._physical_prove(self.watcher, 'physical-1', self.state, self.token)

    def required(self):
        with self.gate.transaction() as db:
            return self.gate.require_reconciled(
                db, 'T1', 'session', expected_seq=1, expected_stopped_at='B')

    def test_valid_fresh_stop_creates_proof_without_public_recovery_api(self):
        self.assertTrue(self.prove())
        self.assertTrue(self.required())
        self.assertTrue(self.store.list_occupancy())

    def test_watcher_delegates_valid_stop_and_completes_without_error(self):
        self.watcher._maybe_prove('physical-1', dict(self.state))
        self.assertTrue(self.required())
        self.assertEqual(self.watcher._proven['physical-1'][0], ('T1', 1))

    def test_already_observed_disconnect_cannot_be_cleared_by_old_state(self):
        self.gate.invalidate_many(('T1',))
        self.assertFalse(self.prove())
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertTrue(self.store.list_occupancy())

    def test_invalid_stale_changed_or_offline_evidence_cannot_prove(self):
        for mode in ('expired', 'offline', 'generation', 'serial', 'moving', 'session',
                     'error', 'missing-error', 'rejected', 'bool-seq'):
            with self.subTest(mode=mode):
                # Each mode starts from its own gate and allocation so an
                # earlier invalidation cannot mask a later validation bug.
                self.setUp()
                watcher = self.watcher
                self.now[0] = 3 if mode == 'expired' else 0
                with watcher._lock:
                    watcher._online['physical-1'] = mode != 'offline'
                    state = dict(self.state)
                    if mode == 'moving':
                        state['status'] = 'MOVING'
                    if mode == 'session':
                        state['robot_session'] = 'other'
                    if mode == 'error':
                        state['error'] = 'link_lost'
                    if mode == 'missing-error':
                        del state['error']
                    if mode == 'rejected':
                        state['rejected'] = {'reason': 'bad order'}
                    if mode == 'bool-seq':
                        state['seq'] = True
                    generation = 2 if mode == 'generation' else 0
                    serial = 2 if mode == 'serial' else 1
                    watcher._latest['physical-1'] = (state, 0.0, generation, serial)
                self.assertFalse(self.prove())
                with self.assertRaises(RecoveryError):
                    self.required()
        self.assertTrue(self.store.list_occupancy())

    def test_expiry_while_completing_cannot_leave_verified_proof(self):
        real_complete = self.gate.complete
        def delayed(*args, **kwargs):
            real_complete(*args, **kwargs)
            self.now[0] = 3.0
        with patch.object(self.gate, 'complete', side_effect=delayed):
            self.assertFalse(self.prove())
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertIn('physical-1', self.watcher._invalidated)
        self.assertTrue(self.store.list_occupancy())

    def test_stale_existing_proof_is_revoked_at_next_proof_check(self):
        self.assertTrue(self.prove())
        self.assertTrue(self.required())
        self.now[0] = 3.0
        self.assertFalse(self.prove())
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertTrue(self.store.list_occupancy())

    def test_new_moving_state_revokes_old_proof_at_next_proof_check(self):
        self.assertTrue(self.prove())
        with self.watcher._lock:
            self.watcher._latest['physical-1'] = (dict(self.state, status='MOVING'), 0.1, 0, 2)
        self.now[0] = 0.1
        self.assertFalse(self.prove())
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertTrue(self.store.list_occupancy())

    def test_worker_observing_moving_revokes_existing_proof(self):
        self.assertTrue(self.prove())
        moving = dict(self.state, status='MOVING')
        with self.watcher._lock:
            self.watcher._latest['physical-1'] = (moving, 0.1, 0, 2)
        self.now[0] = 0.1
        self.watcher._maybe_prove('physical-1', moving)
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertIn('physical-1', self.watcher._invalidated)
        self.assertTrue(self.store.list_occupancy())

    def test_worker_observing_reported_error_revokes_existing_proof(self):
        self.assertTrue(self.prove())
        errored = dict(self.state, error='link_lost')
        with self.watcher._lock:
            self.watcher._latest['physical-1'] = (errored, 0.1, 0, 2)
        self.now[0] = 0.1
        self.watcher._maybe_prove('physical-1', errored)
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertIn('physical-1', self.watcher._invalidated)
        self.assertTrue(self.store.list_occupancy())

    def test_worker_observing_unmapped_order_revokes_existing_proof(self):
        self.assertTrue(self.prove())
        unexpected = dict(self.state, order_id='UNKNOWN')
        with self.watcher._lock:
            self.watcher._latest['physical-1'] = (unexpected, 0.1, 0, 2)
        self.now[0] = 0.1
        self.watcher._maybe_prove('physical-1', unexpected)
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertIn('physical-1', self.watcher._invalidated)

    def test_new_sample_missing_error_revokes_existing_proof(self):
        self.assertTrue(self.prove())
        malformed = dict(self.state)
        del malformed['error']
        with self.watcher._lock:
            self.watcher._latest['physical-1'] = (malformed, 0.1, 0, 2)
        self.now[0] = 0.1
        self.assertFalse(self.prove())
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertTrue(self.store.list_occupancy())

    def test_newer_equivalent_state_retries_without_latching(self):
        newer = dict(self.state, ts=123.0)
        with self.watcher._lock:
            self.watcher._latest['physical-1'] = (newer, 0.1, 0, 2)
        self.now[0] = 0.1
        self.assertFalse(self.prove())
        self.assertNotIn('physical-1', self.watcher._invalidated)
        self.assertTrue(self.runtime._physical_prove(self.watcher, 'physical-1', newer, (0, 2)))
        self.assertTrue(self.required())

    def test_database_failure_latches_and_blocks_without_releasing(self):
        with patch.object(self.gate, 'begin', side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaises(sqlite3.OperationalError):
                self.prove()
        self.assertIn('physical-1', self.watcher._invalidated)
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertTrue(self.store.list_occupancy())

    def test_receiver_offline_waits_for_proof_then_revokes_it(self):
        entered, release, offline_done = threading.Event(), threading.Event(), threading.Event()
        real_complete = self.gate.complete
        def delayed(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return real_complete(*args, **kwargs)
        proof_result = []
        with patch.object(self.gate, 'complete', side_effect=delayed):
            proof_thread = threading.Thread(target=lambda: proof_result.append(self.prove()))
            proof_thread.start()
            self.addCleanup(lambda: (release.set(), proof_thread.join(5)))
            self.assertTrue(entered.wait(3))
            def offline():
                with self.runtime.receiver._dispatch_lock:
                    self.gate.invalidate_many(('T1',))
                offline_done.set()
            offline_thread = threading.Thread(target=offline)
            offline_thread.start()
            self.addCleanup(lambda: offline_thread.join(5))
            self.assertFalse(offline_done.wait(0.1))
            release.set()
            proof_thread.join(5)
            offline_thread.join(5)
        self.assertEqual(proof_result, [True])
        self.assertTrue(offline_done.is_set())
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertTrue(self.store.list_occupancy())

    def test_watcher_disconnect_waits_for_proof_then_revokes_it(self):
        entered, release, offline_done = threading.Event(), threading.Event(), threading.Event()
        real_complete = self.gate.complete
        def delayed(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return real_complete(*args, **kwargs)
        proof_result = []
        with patch.object(self.gate, 'complete', side_effect=delayed):
            proof_thread = threading.Thread(target=lambda: proof_result.append(self.prove()))
            proof_thread.start()
            self.addCleanup(lambda: (release.set(), proof_thread.join(5)))
            self.assertTrue(entered.wait(3))
            def offline():
                self.watcher._on_disconnect(None, None, None, None, None)
                offline_done.set()
            offline_thread = threading.Thread(target=offline)
            offline_thread.start()
            self.addCleanup(lambda: offline_thread.join(5))
            self.assertFalse(offline_done.wait(0.1))
            release.set()
            proof_thread.join(5)
            offline_thread.join(5)
        self.assertEqual(proof_result, [True])
        self.assertTrue(offline_done.is_set())
        self.assertIn('physical-1', self.watcher._invalidated)
        with self.assertRaises(RecoveryError):
            self.required()
        self.assertTrue(self.store.list_occupancy())
