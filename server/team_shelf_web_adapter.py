"""Slot-aware Unreal adapter for the existing isolated web fleet.

All observations and movements are synthetic. The existing fleet and its
reservation/MQTT workflow remain responsible for route execution. This
adapter never publishes an MQTT command itself.
"""
import math
import secrets
import threading
import time

from server.order_demo_actions import DemoActionSession, SNAPSHOT_RETRY_SECONDS
from server.team_shelf_order_slots import PRODUCT_SHELVES, TeamShelfOrderSlots
from server.team_shelf_twin_protocol import TeamShelfTwinSession


class TeamShelfSimInventory:
    """Adapt the 24-slot journal to the existing fleet inventory interface."""
    def __init__(self, store, *, validate_initial=None, claim_world=None, clock=time.monotonic):
        if type(store) is not TeamShelfOrderSlots:
            raise ValueError('explicit team slot journal required')
        if validate_initial is not None and not callable(validate_initial):
            raise ValueError('initial world validator must be callable')
        if claim_world is not None and not callable(claim_world):
            raise ValueError('world claim must be callable')
        if not callable(clock):
            raise ValueError('monotonic snapshot clock required')
        self.store = store
        self.validate_initial = validate_initial
        self.claim_world = claim_world
        self.owner = self.session = self.world_id = self.fence = None
        self.pending = None
        self._clock, self._last_clock = clock, None
        self._snapshot_at, self._snapshot_generation = None, 0
        self.snapshot_event = threading.Event()

    def _now(self):
        now = self._clock()
        if (type(now) not in (int, float) or not math.isfinite(now)
                or (self._last_clock is not None and now < self._last_clock)):
            self._snapshot_at = None
            raise ValueError('Unreal snapshot clock unavailable or moved backwards')
        self._last_clock = float(now)
        return float(now)

    def fresh_snapshot(self, max_age):
        """Return the unchanged UE observation generation only while fresh."""
        if not self.ready or self._snapshot_at is None:
            return None
        age = self._now() - self._snapshot_at
        if age > max_age:
            return None
        return self._snapshot_generation

    @property
    def shelf_ids(self):
        return tuple('N%02d' % i for i in range(1, 7))

    @property
    def ready(self):
        return self.owner is not None and self.session is not None and self.pending is None

    def begin_session(self, owner):
        if owner is None or self.owner is not None:
            raise ValueError('one authenticated owner required')
        self.owner = owner
        self._snapshot_at = None
        self.snapshot_event.clear()
        self.pending = secrets.token_hex(16)
        return self.pending

    def request_snapshot(self, owner):
        if owner is not self.owner or self.session is None:
            raise ValueError('stale simulated owner')
        self.fence = self.store.begin_snapshot(self.session, invalidate=True)
        self._snapshot_at = None
        self.snapshot_event.clear()
        self.pending = secrets.token_hex(16)
        return self.pending

    def apply_snapshot(self, owner, data):
        if (owner is not self.owner or not isinstance(data, dict)
                or set(data) != {'request_id', 'world_id', 'robots', 'slots'}
                or data['request_id'] != self.pending):
            raise ValueError('stale simulated slot snapshot')
        world, robots = data['world_id'], data['robots']
        self._now()
        if (not isinstance(world, str) or len(world) != 32
                or any(c not in '0123456789abcdefABCDEF' for c in world)
                or not isinstance(robots, list) or len(robots) != 2):
            raise ValueError('invalid simulated world')
        seen = set()
        for robot in robots:
            if (not isinstance(robot, dict) or set(robot) != {'agv_id', 'x', 'y', 'yaw', 'carrying', 'interpolating'}
                    or robot['agv_id'] not in ('AGV_01', 'AGV_02') or robot['agv_id'] in seen
                    or any(type(robot[k]) not in (int, float) or not math.isfinite(robot[k])
                           or abs(robot[k]) > 1000000 for k in ('x', 'y', 'yaw'))
                    or type(robot['carrying']) is not bool or type(robot['interpolating']) is not bool):
                raise ValueError('invalid simulated robot snapshot')
            seen.add(robot['agv_id'])
        initial = self.session is None
        if initial:
            if self.validate_initial is not None and self.validate_initial(data) is not True:
                raise ValueError('initial Unreal world differs from the synthetic fleet')
            self.world_id = world
            self.session = self.store.begin_session(self.world_id)
            self.fence = self.store.begin_snapshot(self.session)
        elif world != self.world_id:
            raise ValueError('Unreal world changed')
        self.store.apply_snapshot(self.session, data['slots'], fence=self.fence)
        if initial and self.claim_world is not None:
            try:
                self.claim_world(world)
            except BaseException:
                self.store.end_session(self.session)
                self.session = None
                raise
        observed_at = self._now()
        self.pending = self.fence = None
        self._snapshot_generation += 1
        self._snapshot_at = observed_at
        self.snapshot_event.set()
        return True

    def invalidate(self, owner):
        if owner is self.owner:
            try:
                if self.session is not None:
                    self.store.end_session(self.session)
            finally:
                self.owner = self.session = self.world_id = self.pending = self.fence = None
                self._snapshot_at = None
                self.snapshot_event.set()

    def can_reserve(self, shelf_id, product_type):
        if not self.ready or PRODUCT_SHELVES.get(product_type) != shelf_id:
            return False
        data = self.store.inspect()
        held = {row['slot_id'] for row in data['claims']}
        return (any(row['shelf_id'] == 'N01' and row['state'] == 'occupied'
                    and row['product_type'] == product_type and row['slot_id'] not in held for row in data['slots'])
                and any(row['shelf_id'] == shelf_id and row['state'] == 'empty'
                        and row['slot_id'] not in held for row in data['slots']))

    def reserve(self, shelf_id, product_type, *, request_id=None):
        if not self.ready or PRODUCT_SHELVES.get(product_type) != shelf_id:
            raise ValueError('fresh matching synthetic inventory required')
        request_id = request_id or secrets.token_hex(16)
        row = self.store.reserve(self.session, request_id, product_type)
        return {'reservation_id': request_id, 'shelf_id': shelf_id,
                'slot_index': (row['destination_slot_id'] - 1) % 4}

    def confirm(self, request_id):
        # Fleet calls this inside its place ACK callback. The slot journal is
        # committed by TeamShelfTwinSession after the callback returns.
        if not self.ready or self.store.get(request_id)['state'] != 'placing':
            raise ValueError('correlated place action required')
        return True

    def snapshot(self):
        return {'ready': self.ready, **self.store.inspect()}


class TeamShelfActionSession(DemoActionSession):
    """Bind exact UE slot results to the existing engine's action IDs."""
    def __init__(self, engine, lock, inventory, *, arrival_guard, **kwargs):
        if not callable(arrival_guard):
            raise ValueError('independent simulated arrival guard required')
        super().__init__(engine, lock, inventory=inventory, **kwargs)
        self.arrival_guard = arrival_guard
        self.team_twin = None
        self.original_action = None

    def detach(self, client):
        super().detach(client)
        self.team_twin = self.original_action = None

    def _fail(self):
        self._owner_available.clear()
        self.engine.action_unknown()
        # Fleet.action_unknown() leaves terminal controllers alone. A place
        # ACK may have set completed just before a slot DB commit failed; that
        # controller must not advertise success or be dispatched again.
        engines = getattr(self.engine, 'engines', {})
        if isinstance(engines, dict):
            for item in engines.values():
                lock = getattr(item, 'lock', None)
                mark = getattr(item, '_mark_unknown', None)
                if lock is not None and callable(mark):
                    with lock:
                        mark()
        try:
            if self.owner is not None:
                self.inventory.invalidate(self.owner)
        except Exception:
            # The underlying protocol may already have invalidated this
            # session. Never restore readiness merely because cleanup failed.
            pass
        self.team_twin = self.original_action = None
        self.action_id = self.observed_at = self.sent_at = None
        self._changed()

    def poll(self, client, now=None):
        with self.lock, self._motion_fence:
            if client is not self.owner or not self._owner_available.is_set():
                return None
            now = time.monotonic() if now is None else now
            if not self.inventory.ready:
                retry = self._shelf_request_sent and now - self._shelf_requested_at >= SNAPSHOT_RETRY_SECONDS
                if self._shelf_request_id and (not self._shelf_request_sent or retry):
                    self._shelf_request_sent, self._shelf_requested_at = True, now
                    return {'type': 'team.shelf.snapshot.request.v1',
                            'data': {'request_id': self._shelf_request_id}}
                return None
            if not self.can_dispatch():
                self.engine.suspend(self.action_id if self.sent_at is not None else None)
                return None
            action = self.engine.snapshot().get('pending_action')
            if not action:
                self.action_id = self.observed_at = self.sent_at = None
                self.original_action = None
                return None
            if action['action_id'] != self.action_id:
                self.action_id, self.observed_at, self.sent_at = action['action_id'], now, None
            if self.sent_at is not None:
                if now - self.sent_at >= self.timeout_seconds:
                    self._fail()
                return None
            if now - self.observed_at < self.settle_seconds:
                return None
            try:
                selected = self.engine._selected_action
                reservation = self.engine._slot_reservations[selected]
                request_id = reservation['reservation_id']
                if action.get('action') not in ('grab', 'place'):
                    raise ValueError('unexpected simulated action')
                if self.team_twin is None:
                    self.team_twin = TeamShelfTwinSession(self.inventory.store, self.inventory.session,
                        self.inventory.world_id, client, self.arrival_guard, timeout=self.timeout_seconds)
                output = self.team_twin.begin(request_id, action['agv_id'],
                                              'pick' if action['action'] == 'grab' else 'place')
                self.original_action = dict(action)
                if not self._owner_available.is_set():
                    self._fail()
                    return None
                self.sent_at = now
                return output
            except Exception:
                self._fail()
                return None

    def receive(self, client, message, now=None):
        with self.lock, self._motion_fence:
            if client is not self.owner or not self._owner_available.is_set() or not isinstance(message, dict):
                return False
            if message.get('type') == 'team.shelf.snapshot.v1':
                if not self._shelf_request_sent:
                    return False
                try:
                    accepted = self.inventory.apply_snapshot(client, message.get('data'))
                except (TypeError, ValueError):
                    return False
                self._shelf_request_id = self._shelf_requested_at = None
                self._shelf_request_sent = False
                self._changed()
                return accepted
            now = time.monotonic() if now is None else now
            if (self.sent_at is None or now - self.sent_at >= self.timeout_seconds
                    or not self.can_dispatch() or self.team_twin is None or self.original_action is None):
                if self.sent_at is not None:
                    self._fail()
                return False
            original = self.original_action
            def apply(data):
                if (not self._owner_available.is_set() or client is not self.owner
                        or self.engine.snapshot().get('pending_action') != original):
                    return False
                accepted = self.engine.apply_action_result(*(original[key] for key in
                    ('order_id', 'action_id', 'agv_id', 'action')), data['applied'])
                return accepted is True and self._owner_available.is_set()
            try:
                accepted = self.team_twin.receive_with_effect(client, message, apply)
            except Exception:
                self._fail()
                return False
            if not accepted:
                return False
            self.original_action = None
            self.action_id = self.observed_at = self.sent_at = None
            try:
                self._shelf_request_id = self.inventory.request_snapshot(client)
            except Exception:
                self._fail()
                return False
            self._shelf_request_sent = False
            self._changed()
            return True
