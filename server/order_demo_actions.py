"""Correlate simulated actions with one authenticated Unreal connection."""
import threading
import time


class DemoActionSession:
    """No action is retried after uncertain delivery or disconnection.

    The caller passes the same reentrant lock used by the demo engine.
    """
    def __init__(self, engine, lock, settle_seconds=1.5, timeout_seconds=10, can_dispatch=None,
                 on_change=None):
        self.engine = engine
        self.lock = lock
        self.settle_seconds = settle_seconds
        self.timeout_seconds = timeout_seconds
        self.can_dispatch = can_dispatch or (lambda: True)
        if on_change is not None and not callable(on_change):
            raise ValueError('action change callback must be callable')
        self.on_change = on_change
        self._owner_available = threading.Event()
        self._owner_fast_lock = threading.Lock()
        self._fast_owner = None
        self.owner = None
        self.action_id = None
        self.observed_at = None
        self.sent_at = None

    def _changed(self):
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                # Observers must not break an action transition. If an
                # invalidation callback fails, the fast view fails closed.
                self._owner_available.clear()

    def attach(self, client, authenticated):
        with self.lock:
            if not authenticated or self.owner is not None:
                return False
            with self._owner_fast_lock:
                self.owner = self._fast_owner = client
                self._owner_available.set()
            self._changed()
            return True

    def available(self):
        with self.lock:
            return self.owner is not None and self._owner_available.is_set()

    def available_fast(self):
        """Nonblocking observation only; authorization still uses available()."""
        return self._owner_available.is_set()

    def invalidate_owner(self, client):
        """Immediately fail closed for this owner without the action lock."""
        with self._owner_fast_lock:
            if client is not self._fast_owner:
                return
            self._owner_available.clear()
        self._changed()

    def detach(self, client):
        # Clear before waiting for the shared lock so a disconnected owner is
        # not advertised as available while a slow operation is in progress.
        self.invalidate_owner(client)
        with self.lock:
            if client is not self.owner:
                return
            with self._owner_fast_lock:
                self._owner_available.clear()
                self._fast_owner = None
            self.engine.action_unknown()
            self.owner = None
            self.action_id = self.observed_at = self.sent_at = None
            self._changed()

    def poll(self, client, now=None):
        with self.lock:
            if client is not self.owner:
                return None
            if not self._owner_available.is_set() or not self.can_dispatch():
                self.engine.action_unknown()
                self.action_id = self.observed_at = self.sent_at = None
                self._changed()
                return None
            snapshot = self.engine.snapshot()
            action = snapshot.get('pending_action')
            if not action:
                self.action_id = self.observed_at = self.sent_at = None
                return None
            # Evaluate the live clock only after the lock, readiness checks,
            # and state snapshot, never while queued for a slow operation.
            now = time.monotonic() if now is None else now
            if action['action_id'] != self.action_id:
                self.action_id = action['action_id']
                self.observed_at, self.sent_at = now, None
            if self.sent_at is not None:
                if now - self.sent_at >= self.timeout_seconds:
                    self.engine.action_unknown()
                    self.action_id = self.observed_at = self.sent_at = None
                    self._changed()
                return None
            if now - self.observed_at < self.settle_seconds:
                return None
            if not self._owner_available.is_set():
                self.engine.action_unknown()
                self.action_id = self.observed_at = self.sent_at = None
                self._changed()
                return None
            self.sent_at = now
            pose = snapshot.get('pose')
            if not pose or type(pose.get('x')) not in (int, float) or type(pose.get('y')) not in (int, float):
                self.engine.action_unknown()
                self._changed()
                return None
            return {'type': 'robot.action', 'data': dict(action, result='success',
                                                          expected_x=pose['x'] / 1000,
                                                          expected_y=pose['y'] / 1000)}

    def receive(self, client, message, now=None):
        with self.lock:
            if client is not self.owner or self.sent_at is None or not isinstance(message, dict):
                return False
            live_clock = now is None
            now = time.monotonic() if now is None else now
            if (not self._owner_available.is_set() or not self.can_dispatch()
                    or now - self.sent_at >= self.timeout_seconds):
                self.engine.action_unknown()
                self.action_id = self.observed_at = self.sent_at = None
                self._changed()
                return False
            data = message.get('data')
            if message.get('type') != 'robot.action.result' or not isinstance(data, dict):
                return False
            action = self.engine.snapshot().get('pending_action')
            if not action or action.get('action_id') != self.action_id:
                return False
            keys = ('order_id', 'action_id', 'agv_id', 'action')
            if any(data.get(key) != action.get(key) for key in keys) or type(data.get('applied')) is not bool:
                return False
            # Readiness and the selected-engine snapshot can block. Never apply
            # an acknowledgement that expired while either one was waiting.
            applied_at = time.monotonic() if live_clock else now
            if (not self._owner_available.is_set()
                    or applied_at - self.sent_at >= self.timeout_seconds):
                self.engine.action_unknown()
                self.action_id = self.observed_at = self.sent_at = None
                self._changed()
                return False
            applied = self.engine.apply_action_result(*(data[key] for key in keys), data['applied'])
            if applied:
                self.action_id = self.observed_at = self.sent_at = None
                self._changed()
            return bool(applied)
