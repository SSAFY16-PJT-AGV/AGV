"""Correlate simulated actions with one authenticated Unreal connection."""
from contextlib import contextmanager
import math
import re
import threading
import time

SNAPSHOT_RETRY_SECONDS = 2.0


def _validate_unreal_snapshot_envelope(data):
    """Check the extended Unreal snapshot before retaining only shelf data."""
    if set(data) != {'request_id', 'shelves', 'world_id', 'robots', 'pickups'}:
        raise ValueError('invalid Unreal snapshot fields')
    world = data['world_id']
    if not isinstance(world, str) or re.fullmatch(r'[0-9a-fA-F]{32}', world) is None:
        raise ValueError('invalid Unreal world identity')
    robots = data['robots']
    if not isinstance(robots, list) or len(robots) != 2:
        raise ValueError('two Unreal robots are required')
    seen = set()
    for robot in robots:
        if (not isinstance(robot, dict) or set(robot) !=
                {'agv_id', 'x', 'y', 'yaw', 'carrying', 'interpolating'}):
            raise ValueError('invalid Unreal robot snapshot')
        name = robot['agv_id']
        if (name not in ('AGV_01', 'AGV_02') or name in seen
                or type(robot['carrying']) is not bool or type(robot['interpolating']) is not bool):
            raise ValueError('invalid Unreal robot identity')
        for key in ('x', 'y', 'yaw'):
            value = robot[key]
            if type(value) not in (int, float) or abs(value) > 1_000_000 or not math.isfinite(value):
                raise ValueError('invalid Unreal robot position')
        seen.add(name)
    pickups = data['pickups']
    if not isinstance(pickups, list) or not 1 <= len(pickups) <= 128:
        raise ValueError('invalid Unreal pickups')
    sources = set()
    for item in pickups:
        if not isinstance(item, dict) or set(item) != {'source_id', 'product_type', 'available'}:
            raise ValueError('invalid Unreal pickup snapshot')
        source, product, count = item['source_id'], item['product_type'], item['available']
        if (not isinstance(source, str) or not source.strip() or len(source) > 128 or source in sources
                or not isinstance(product, str) or not product.strip() or len(product) > 128
                or type(count) is not int or not 0 <= count <= 1_000_000):
            raise ValueError('invalid Unreal pickup snapshot')
        sources.add(source)


class DemoActionSession:
    """No action is retried after uncertain delivery or disconnection.

    The caller passes the same reentrant lock used by the demo engine.
    """
    def __init__(self, engine, lock, settle_seconds=1.5, timeout_seconds=10, can_dispatch=None,
                 on_change=None, pause_on_unavailable=False, inventory=None,
                 validate_snapshot=None, on_snapshot=None, on_invalidate=None, can_send_state=None,
                 on_snapshot_request=None, on_prepare_action=None, on_action_result=None, on_detach=None,
                 pose_transform=None, action_source='unreal'):
        if type(action_source) is not str or action_source not in ('unreal', 'synthetic'):
            raise ValueError('invalid simulated action source')
        self._action_source = action_source
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
        self.validate_snapshot = validate_snapshot
        self.on_snapshot = on_snapshot
        self.on_invalidate = on_invalidate
        self.can_send_state = can_send_state
        self.on_snapshot_request = on_snapshot_request
        self.on_prepare_action = on_prepare_action
        self.on_action_result = on_action_result
        self.on_detach = on_detach
        if pose_transform is not None and not callable(pose_transform):
            raise ValueError('pose transform must be callable')
        self.pose_transform = pose_transform
        self._shelf_request_id = None
        self._shelf_request_sent = False
        self._shelf_requested_at = None
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

    def attach(self, client, authenticated, *, source='unreal'):
        with self.lock:
            if (type(source) is not str or source != self._action_source
                    or not authenticated or self.owner is not None):
                return False
            if self.inventory is not None:
                self._shelf_request_id = self.inventory.begin_session(client)
                self._shelf_request_sent = False
                self._shelf_requested_at = None
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

    @contextmanager
    def synthetic_effect_guard(self, client, action):
        """Fence one internal synthetic effect against owner/action expiry.

        Callers must not wait for new observations while holding this guard.
        This is not an authorization or acknowledgement for physical robots.
        """
        with self.lock, self._motion_fence:
            valid = (self._action_source == 'synthetic' and client is self.owner
                     and self._owner_available.is_set() and isinstance(action, dict)
                     and self.sent_at is not None and action.get('action_id') == self.action_id)
            if valid:
                pending = self.engine.snapshot().get('pending_action')
                keys = ('order_id', 'action_id', 'agv_id', 'action')
                valid = (isinstance(pending, dict) and all(action.get(key) == pending.get(key)
                         and isinstance(action.get(key), str) and action[key] for key in keys)
                         and self.can_dispatch() is True)
            if valid:
                age = time.monotonic() - self.sent_at
                valid = (self._owner_available.is_set() and client is self.owner
                         and self.sent_at is not None and action.get('action_id') == self.action_id
                         and 0 <= age < self.timeout_seconds)
            yield bool(valid)

    def invalidate_owner(self, client):
        """Signal immediately, then fence any already-running simulated tick."""
        with self._owner_fast_lock:
            if client is not self._fast_owner:
                return
            self._owner_available.clear()
            if self.on_invalidate is not None:
                self.on_invalidate()
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
            if self.on_detach is not None:
                self.on_detach()
            if self.inventory is not None:
                self.inventory.invalidate(client)
                self._shelf_request_id = None
                self._shelf_request_sent = False
                self._shelf_requested_at = None
            self.owner = None
            self.action_id = self.observed_at = self.sent_at = None
            self._changed()

    def poll(self, client, now=None):
        with self.lock, self._motion_fence:
            if client is not self.owner:
                return None
            if self.inventory is not None and not self.inventory.ready:
                if not self._owner_available.is_set():
                    return None
                snapshot_time = time.monotonic() if now is None else now
                retry_due = (self._shelf_request_sent and self._shelf_requested_at is not None
                             and snapshot_time - self._shelf_requested_at >= SNAPSHOT_RETRY_SECONDS)
                if self._shelf_request_id and (not self._shelf_request_sent or retry_due):
                    if not self._shelf_request_sent and self.on_snapshot_request is not None:
                        self.on_snapshot_request()
                    self._shelf_request_sent = True
                    self._shelf_requested_at = snapshot_time
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
            pose = snapshot.get('pose')
            if not pose or type(pose.get('x')) not in (int, float) or type(pose.get('y')) not in (int, float):
                self.engine.action_unknown()
                self._changed()
                return None
            try:
                point = self.pose_transform(pose) if self.pose_transform else {
                    'x': pose['x'] / 1000, 'y': pose['y'] / 1000}
            except (ValueError, TypeError, KeyError, OverflowError):
                self.engine.action_unknown()
                self._changed()
                return None
            if self.on_prepare_action is not None:
                try:
                    self.on_prepare_action(action)
                except Exception:
                    self.engine.action_unknown()
                    self._changed()
                    return None
            if not self._owner_available.is_set():
                self.engine.action_unknown()
                self._changed()
                return None
            self.sent_at = now
            return {'type': 'robot.action', 'data': dict(action, result='success',
                                                          expected_x=point['x'],
                                                          expected_y=point['y'])}

    def receive(self, client, message, now=None):
        with self.lock, self._motion_fence:
            if client is not self.owner or not isinstance(message, dict) or not self._owner_available.is_set():
                return False
            if self.inventory is not None and message.get('type') == 'shelf.snapshot':
                data = message.get('data')
                if (not self._shelf_request_sent or not isinstance(data, dict)
                        or data.get('request_id') != self._shelf_request_id):
                    return False
                if self.validate_snapshot is not None:
                    self.validate_snapshot(data)
                    data = {'request_id': data['request_id'], 'shelves': data['shelves']}
                elif any(key in data for key in ('world_id', 'robots', 'pickups')):
                    _validate_unreal_snapshot_envelope(data)
                    data = {'request_id': data['request_id'], 'shelves': data['shelves']}
                accepted = self.inventory.apply_snapshot(client, data)
                if accepted:
                    self._shelf_request_id = None
                    self._shelf_request_sent = False
                    self._shelf_requested_at = None
                    if self.on_snapshot is not None:
                        self.on_snapshot()
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
            if self.on_action_result is not None:
                try:
                    self.on_action_result(data)
                except Exception:
                    self.engine.action_unknown()
                    self.action_id = self.observed_at = self.sent_at = None
                    self._changed()
                    return False
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
