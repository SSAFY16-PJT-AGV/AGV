"""Clock continuity includes recovery gate internals and pending retry."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import threading

from sim.mqtt_order_robot import SimMqttOrderRobot

from server.sim_wait_refresh import _now, _tracked_gate_clock, retry_wait


class ClockGate:
    def __init__(self, readings):
        self._epoch = 'boot'
        self._readings = iter(readings)
        self._clock = lambda: next(self._readings)
        self._operation_lock = threading.RLock()
        self.blocked = set()

    def _block(self, task):
        self.blocked.add(task)


class WaitClockBoundaryTest(TestCase):
    def test_internal_observation_clock_jump_then_rollback_is_permanently_blocked(self):
        runtime, gate = SimpleNamespace(), ClockGate([120, 1000, 120, 1200])
        original = gate._clock
        with _tracked_gate_clock(runtime, gate, 'T'):
            self.assertEqual(_now(runtime, gate, 'T'), 120)
            self.assertEqual(gate._clock(), 1000)
            with self.assertRaisesRegex(ValueError, 'backwards'):
                gate._clock()
        self.assertIs(gate._clock, original)
        self.assertIn('T', gate.blocked)
        with self.assertRaises(ValueError):
            _now(runtime, gate, 'T')

    def test_retry_tracks_internal_gate_clock_before_any_progress(self):
        gate = ClockGate([120, 110, 130])
        robot = SimpleNamespace(_lock=threading.RLock(), ready=True,
            _event_uncertain=False, _reroute_next_inflight=False,
            _queue=SimpleNamespace(empty=lambda: True))
        robot._client = object()
        robot._motion_fence = threading.Lock()
        robot.executor = SimpleNamespace(_lock=threading.RLock(), _inbox=SimpleNamespace(_lock=threading.RLock()))
        advanced = []
        def retry(robot_id, event):
            gate._clock()  # represents the actual retry's gate validation
            advanced.append((robot_id, event))
            return {'status': 'advanced'}
        receiver = SimpleNamespace(_dispatch_lock=threading.RLock(), _occupied_events=set(), ready=True,
            client=SimpleNamespace(is_connected=lambda: True), retry_occupied_event=retry)
        runtime = SimpleNamespace(_tasks={'T': ('sim-r1', 'session')},
            _reroute_simulators={'sim-r1': robot}, _runtime_read_operation=nullcontext,
            _lock=threading.RLock(), _ready=lambda: None, gate=gate, receiver=receiver,
            inbox=SimpleNamespace(_lock=threading.RLock()))
        def check(runtime, gate, task, session):
            self.assertEqual(_now(runtime, gate, task), 120)
        with patch('server.sim_wait_refresh._owned_simulator', return_value=True), \
             patch('server.sim_wait_refresh._check_wait_clock', side_effect=check):
            with self.assertRaisesRegex(ValueError, 'backwards'):
                retry_wait(runtime, 'T', robot, 'event', simulation_enabled=True)
        self.assertEqual(advanced, [])
        self.assertIn('T', gate.blocked)
        with self.assertRaises(ValueError):
            _now(runtime, gate, 'T')


    def test_real_disconnect_callback_and_retry_are_ordered_by_motion_fence(self):
        class Robot:
            def __init__(self):
                self._lock = threading.RLock(); self._motion_fence = threading.Lock()
                self._client = object(); self._ready = threading.Event(); self._ready.set()
                self._stopping = True; self._event_uncertain = False; self._reroute_next_inflight = False
                self._queue = SimpleNamespace(empty=lambda: True)
                self.executor = SimpleNamespace(_lock=threading.RLock(),
                    _inbox=SimpleNamespace(_lock=threading.RLock()))
            @property
            def ready(self): return self._ready.is_set()
        def setup():
            robot = Robot(); gate = ClockGate([120, 120]); seen = []
            receiver = SimpleNamespace(_dispatch_lock=threading.RLock(), _occupied_events=set(), ready=True,
                client=SimpleNamespace(is_connected=lambda: True),
                retry_occupied_event=lambda *args: seen.append(args) or {'status': 'not_occupied'})
            runtime = SimpleNamespace(_tasks={'T': ('sim-r1', 'session')},
                _reroute_simulators={'sim-r1': robot}, _runtime_read_operation=nullcontext,
                _lock=threading.RLock(), _ready=lambda: None, gate=gate, receiver=receiver,
                inbox=SimpleNamespace(_lock=threading.RLock()))
            return robot, gate, seen, runtime
        def disconnect(robot):
            SimMqttOrderRobot._on_disconnect(robot, robot._client, None, None, None, None)
        robot, gate, seen, runtime = setup()
        disconnect(robot)
        with patch('server.sim_wait_refresh._owned_simulator', return_value=True):
            with self.assertRaises(ValueError):
                retry_wait(runtime, 'T', robot, 'event', simulation_enabled=True)
        self.assertEqual(seen, [])

        # Pause this retry after its initial ready check but before it can
        # acquire the motion fence; let the actual callback finish first.
        robot, gate, seen, runtime = setup()
        before_fence, allow_retry = threading.Event(), threading.Event()
        class HookFence:
            def __init__(self): self.lock = threading.Lock(); self.retry_thread = None
            def __enter__(self):
                if threading.current_thread() is self.retry_thread:
                    before_fence.set()
                    if not allow_retry.wait(5): raise AssertionError('barrier timed out')
                self.lock.acquire(); return self
            def __exit__(self, *args): self.lock.release()
        robot._motion_fence = HookFence(); errors = []
        def paused_retry():
            try: retry_wait(runtime, 'T', robot, 'event', simulation_enabled=True)
            except Exception as exc: errors.append(exc)
        with patch('server.sim_wait_refresh._owned_simulator', return_value=True):
            thread = threading.Thread(target=paused_retry); robot._motion_fence.retry_thread = thread
            thread.start(); self.assertTrue(before_fence.wait(5))
            try: disconnect(robot)
            finally: allow_retry.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError); self.assertEqual(seen, [])

        robot, gate, seen, runtime = setup()
        entered, release, disconnected = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def check(*args):
            entered.set()
            if not release.wait(5): raise AssertionError('retry barrier not released')
        def retry_worker():
            try: retry_wait(runtime, 'T', robot, 'event', simulation_enabled=True)
            except Exception as exc: errors.append(exc)
        def disconnect_worker():
            disconnect(robot); disconnected.set()
        with patch('server.sim_wait_refresh._owned_simulator', return_value=True), \
                patch('server.sim_wait_refresh._check_wait_clock', side_effect=check):
            first = threading.Thread(target=retry_worker); first.start()
            self.assertTrue(entered.wait(5))
            second = threading.Thread(target=disconnect_worker); second.start()
            try:
                self.assertFalse(disconnected.wait(.1), 'disconnect crossed the motion fence')
            finally:
                release.set(); first.join(5); second.join(5)
        self.assertFalse(first.is_alive()); self.assertFalse(second.is_alive())
        self.assertEqual(errors, []); self.assertEqual(seen, [('sim-r1', 'event')])
        self.assertTrue(disconnected.is_set()); self.assertFalse(robot.ready)
