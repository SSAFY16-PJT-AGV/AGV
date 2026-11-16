"""Correlate simulated actions with one authenticated Unreal connection."""
import time


class DemoActionSession:
    """No action is retried after uncertain delivery or disconnection.

    The caller passes the same reentrant lock used by the demo engine.
    """
    def __init__(self, engine, lock, settle_seconds=1.5, timeout_seconds=10, can_dispatch=None):
        self.engine = engine
        self.lock = lock
        self.settle_seconds = settle_seconds
        self.timeout_seconds = timeout_seconds
        self.can_dispatch = can_dispatch or (lambda: True)
        self.owner = None
        self.action_id = None
        self.observed_at = None
        self.sent_at = None

    def attach(self, client, authenticated):
        with self.lock:
            if not authenticated or self.owner is not None:
                return False
            self.owner = client
            return True

    def available(self):
        with self.lock:
            return self.owner is not None

    def detach(self, client):
        with self.lock:
            if client is not self.owner:
                return
            self.engine.action_unknown()
            self.owner = None
            self.action_id = self.observed_at = self.sent_at = None

    def poll(self, client, now):
        with self.lock:
            if client is not self.owner:
                return None
            if not self.can_dispatch():
                self.engine.action_unknown()
                self.action_id = self.observed_at = self.sent_at = None
                return None
            action = self.engine.snapshot().get('pending_action')
            if not action:
                self.action_id = self.observed_at = self.sent_at = None
                return None
            if action['action_id'] != self.action_id:
                self.action_id = action['action_id']
                self.observed_at, self.sent_at = now, None
            if self.sent_at is not None:
                if now - self.sent_at >= self.timeout_seconds:
                    self.engine.action_unknown()
                    self.action_id = self.observed_at = self.sent_at = None
                return None
            if now - self.observed_at < self.settle_seconds:
                return None
            self.sent_at = now
            pose = self.engine.snapshot().get('pose')
            if not pose or type(pose.get('x')) not in (int, float) or type(pose.get('y')) not in (int, float):
                self.engine.action_unknown()
                return None
            return {'type': 'robot.action', 'data': dict(action, result='success',
                                                          expected_x=pose['x'] / 1000,
                                                          expected_y=pose['y'] / 1000)}

    def receive(self, client, message, now=None):
        with self.lock:
            if client is not self.owner or self.sent_at is None or not isinstance(message, dict):
                return False
            now = time.monotonic() if now is None else now
            if not self.can_dispatch() or now - self.sent_at >= self.timeout_seconds:
                self.engine.action_unknown()
                self.action_id = self.observed_at = self.sent_at = None
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
            applied = self.engine.apply_action_result(*(data[key] for key in keys), data['applied'])
            if applied:
                self.action_id = self.observed_at = self.sent_at = None
            return bool(applied)
