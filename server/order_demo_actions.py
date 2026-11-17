"""Correlate simulated actions with one authenticated Unreal connection."""
from contextlib import contextmanager
import threading
import time


class DemoActionSession:
    """No action is retried after uncertain delivery or disconnection.

    The caller passes the same reentrant lock used by the demo engine.
    """
    def __init__(self, engine, lock, settle_seconds=1.5, timeout_seconds=10, can_dispatch=None,
                 on_change=None, pause_on_unavailable=False, inventory=None):
        self.engine = engine
        self.lock = lock
        self.settle_seconds = settle_seconds
        self.timeout_seconds = timeout_seconds
        self.can_dispatch = can_dispatch or (lambda: True)
        if on_change is not None and not callable(on_change):
            raise ValueError('action change callback must be callable')
        self.on_change = on_change
        self.pause_on_unavailable = pause_on_unavailable
        self.inventory = inventory
        self._shelf_request_id = None
        self._shelf_request_sent = False
        self._owner_available = threading.Event()
        self._owner_fast_lock = threading.Lock()
        # Orders and robot ticks use a common fence with owner invalidation.
        # An already-started in-process operation finishes before invalidation
        # returns; no later operation may begin with the invalidated owner.
        self._motion_fence = threading.RLock()
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
            if self.inventory is not None:
                self._shelf_request_id = self.inventory.begin_session(client)
                self._shelf_request_sent = False
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

    @contextmanager
    def motion_guard(self):
        with self._motion_fence:
            yield self._owner_available.is_set()

    def invalidate_owner(self, client):
        """Signal immediately, then fence any already-running simulated tick."""
        with self._owner_fast_lock:
            if client is not self._fast_owner:
                return
            self._owner_available.clear()
        self._changed()
        # Never wait for the shared action lock; this fence only waits for a
        # tick which had already acquired authorization before the signal.
        with self._motion_fence:
            pass

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
            if self.inventory is not None:
                self.inventory.invalidate(client)
                self._shelf_request_id = None
                self._shelf_request_sent = False
            self.owner = None
            self.action_id = self.observed_at = self.sent_at = None
            self._changed()

    def poll(self, client, now=None):
        with self.lock:
            if client is not self.owner:
                return None
            if self.inventory is not None and not self.inventory.ready:
                if not self._owner_available.is_set():
                    return None
                if not self._shelf_request_sent and self._shelf_request_id:
                    self._shelf_request_sent = True
                    return {'type': 'shelf.snapshot.request',
                            'data': {'request_id': self._shelf_request_id,
                                     'shelf_ids': list(self.inventory.shelf_ids)}}
                return None
            if not self._owner_available.is_set() or not self.can_dispatch():
                if self.pause_on_unavailable and self._owner_available.is_set():
                    self.engine.suspend(self.action_id if self.sent_at is not None else None)
                else:
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
            if client is not self.owner or not isinstance(message, dict) or not self._owner_available.is_set():
                return False
            if self.inventory is not None and message.get('type') == 'shelf.snapshot':
                data = message.get('data')
                if (not self._shelf_request_sent or not isinstance(data, dict)
                        or data.get('request_id') != self._shelf_request_id):
                    return False
                accepted = self.inventory.apply_snapshot(client, data)
                if accepted:
                    self._shelf_request_id = None
                    self._shelf_request_sent = False
                    self._changed()
                return bool(accepted)
            if self.sent_at is None:
                return False
            live_clock = now is None
            now = time.monotonic() if now is None else now
            if (not self._owner_available.is_set() or not self.can_dispatch()
                    or now - self.sent_at >= self.timeout_seconds):
                if (self.pause_on_unavailable and self._owner_available.is_set()
                        and not self.can_dispatch()):
                    self.engine.suspend(self.action_id)
                else:
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
                if self.inventory is not None:
                    self._shelf_request_id = self.inventory.request_snapshot(client)
                    self._shelf_request_sent = False
                self._changed()
            return bool(applied)
