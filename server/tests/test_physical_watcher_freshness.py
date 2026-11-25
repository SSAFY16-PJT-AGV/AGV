"""Watcher evidence freshness tests; no broker or physical robot is used."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.physical_state_proof import PhysicalStateWatcher


class Message:
    def __init__(self, state):
        self.topic = 'agv/physical-1/state'
        self.retain = False
        self.payload = json.dumps(state).encode('utf-8')


class FakeDB:
    def execute(self, _sql):
        return None

    def close(self):
        pass


class WatcherFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.w = PhysicalStateWatcher(
            ['physical-1'], client=SimpleNamespace(), host='127.0.0.1', port=1,
            log=lambda _message: None, clock=lambda: self.now[0])
        self.w._mqtt_started = True
        self.client = SimpleNamespace(subscribe=lambda *_a, **_kw: (0, 1))
        self.w._on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.state = {'robot_session': 'boot-1', 'status': 'WAITING', 'order_id': 'T1',
                      'seq': 1, 'last_node': 'B', 'released_until': 'B', 'map_id': 'm@v1'}

    def receive(self, state=None):
        self.w._on_message(self.client, None, Message(self.state if state is None else state))

    def bind_runtime(self):
        calls = []
        gate = SimpleNamespace(invalidate_many=lambda tasks: calls.append(tuple(tasks)))
        self.w._runtime = SimpleNamespace(_tasks={'T1': ('physical-1', 'boot-1')}, gate=gate)
        self.w._sessions = {'physical-1': 'boot-1'}
        return calls

    def test_state_expiry_blocks_task_and_is_not_reused(self):
        blocked = self.bind_runtime()
        self.receive()
        self.assertIsNotNone(self.w.latest('physical-1'))
        self.now[0] = 2.01
        self.assertIsNone(self.w.latest('physical-1'))
        self.assertEqual(blocked, [('T1',)])
        self.receive()
        self.assertFalse(self.w._check_session('physical-1', self.state))

    def test_disconnect_revokes_evidence_and_reconnect_does_not_restore_it(self):
        blocked = self.bind_runtime()
        self.receive()
        self.w._on_disconnect(self.client, None, None, None, None)
        self.assertIsNone(self.w.latest('physical-1'))
        self.assertEqual(blocked, [('T1',)])
        self.w._on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.assertIsNone(self.w.latest('physical-1'))
        self.receive()
        self.assertFalse(self.w._check_session('physical-1', self.state))

    def test_session_discovery_does_not_accept_an_expired_state(self):
        self.receive()
        self.now[0] = 3.0
        with self.assertRaisesRegex(RuntimeError, 'physical-1'):
            self.w.wait_sessions(0)

    def test_expired_state_does_not_open_a_new_proof(self):
        self.bind_runtime()
        self.receive()
        self.now[0] = 3.0
        attempted = []
        self.w._runtime.begin = lambda *_args: attempted.append(True)
        self.w._maybe_prove('physical-1', dict(self.state))
        self.assertEqual(attempted, [])

    def test_new_state_between_begin_and_complete_does_not_finish_proof(self):
        self.bind_runtime()
        self.receive()
        complete = []
        def begin(*_args):
            self.receive(dict(self.state, status='MOVING'))
            return {'challenge': 'challenge'}
        self.w._runtime.path = 'unused'
        self.w._runtime._field_simulation = True
        self.w._runtime.begin = begin
        self.w._runtime.complete = lambda *_args: complete.append(True)
        expected = {'robot_id': 'physical-1', 'seq': 1, 'stopped_at': 'B',
                    'map_id': 'm@v1', 'order_digest': 'digest'}
        with patch('server.physical_state_proof.sqlite3.connect', return_value=FakeDB()), \
             patch('server.physical_state_proof._snapshot', return_value=expected):
            self.w._maybe_prove('physical-1', dict(self.state))
        self.assertEqual(complete, [])

    def test_disconnect_between_begin_and_complete_revokes_challenge(self):
        blocked = self.bind_runtime()
        self.receive()
        completed = []
        def begin(*_args):
            self.w._on_disconnect(self.client, None, None, None, None)
            return {'challenge': 'challenge'}
        self.w._runtime.path = 'unused'
        self.w._runtime._field_simulation = True
        self.w._runtime.begin = begin
        self.w._runtime.complete = lambda *_args: completed.append(True)
        expected = {'robot_id': 'physical-1', 'seq': 1, 'stopped_at': 'B',
                    'map_id': 'm@v1', 'order_digest': 'digest'}
        with patch('server.physical_state_proof.sqlite3.connect', return_value=FakeDB()), \
             patch('server.physical_state_proof._snapshot', return_value=expected):
            self.w._maybe_prove('physical-1', dict(self.state))
        self.assertEqual(completed, [])
        self.assertEqual(blocked, [('T1',)])

    def test_stale_state_does_not_retry_pending_event(self):
        self.bind_runtime()
        self.receive()
        self.now[0] = 3.0
        retried = []
        self.w._runtime.pending_events = lambda: {'events': [
            {'robot_id': 'physical-1', 'event_id': 'E1', 'task_id': 'T1'}]}
        self.w._runtime.receiver = SimpleNamespace(_occupied_events=set())
        self.w._runtime.retry = lambda *_args: retried.append(True)
        self.w._retry_events()
        self.assertEqual(retried, [])

    def test_new_robot_session_does_not_reuse_old_event_proof(self):
        blocked = self.bind_runtime()
        self.receive(dict(self.state, robot_session='boot-2'))
        self.w._proven['physical-1'] = (('T1', 1), 0)
        retried = []
        self.w._runtime.pending_events = lambda: {'events': [
            {'robot_id': 'physical-1', 'event_id': 'E1', 'task_id': 'T1'}]}
        self.w._runtime.receiver = SimpleNamespace(_occupied_events=set())
        self.w._runtime.retry = lambda *_args: retried.append(True)
        self.w._retry_events()
        self.assertEqual(retried, [])
        self.assertEqual(blocked, [('T1',)])
