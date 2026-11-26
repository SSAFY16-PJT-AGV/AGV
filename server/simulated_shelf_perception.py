"""Explicitly synthetic team shelf perception integration.

The producer receives a fresh in-process capture token before generating its
six shelf frames. This does not authenticate cameras, physical robots, MQTT
senders, clocks, or real inventory. It never starts a physical action.
"""
import copy
import json
import math
import secrets
import threading
import time

from server.team_shelf_order_slots import PRODUCT_MARKERS, PRODUCT_SHELVES, TeamShelfOrderSlots


class SimulatedShelfPerception:
    """Collect one bounded synthetic six-shelf view under the journal fence."""

    def __init__(self, store, session, *, clock=time.monotonic, max_age_seconds=5.0):
        if (type(store) is not TeamShelfOrderSlots or not isinstance(session, str) or not session
                or not callable(clock) or type(max_age_seconds) not in (int, float)
                or not math.isfinite(max_age_seconds) or not 0 < max_age_seconds <= 60):
            raise ValueError('explicit simulated store, session and bounded age required')
        self._store, self._session = store, session
        self._clock, self._max_age = clock, float(max_age_seconds)
        self._lock = threading.RLock()
        self._batch, self._fresh_at, self._last_clock = None, None, None

    def _now(self):
        value = self._clock()
        if (type(value) not in (int, float) or not math.isfinite(value)
                or (self._last_clock is not None and value < self._last_clock)):
            self._batch, self._fresh_at = None, None
            self._store.begin_snapshot(self._session, invalidate=True)
            raise ValueError('synthetic observation clock unavailable or moved backwards')
        self._last_clock = float(value)
        return float(value)

    def begin_capture(self):
        with self._lock:
            now = self._now()
            fence = self._store.begin_snapshot(self._session, invalidate=True)
            token = secrets.token_urlsafe(32)
            self._fresh_at = None
            self._batch = {'token': token, 'fence': fence, 'started': now, 'frames': {}}
            return token

    @staticmethod
    def _parse(shelf_id, frame):
        if shelf_id not in {'N01', 'N02', 'N03', 'N04', 'N05', 'N06'}:
            raise ValueError('unknown simulated shelf')
        if not isinstance(frame, dict) or frame.get('frame') != 'robot_base':
            raise ValueError('synthetic robot-frame observation required')
        try:
            encoded = json.dumps(frame, allow_nan=False, ensure_ascii=True, separators=(',', ':'))
        except (TypeError, ValueError, RecursionError):
            raise ValueError('invalid synthetic observation') from None
        if len(encoded) > 65536:
            raise ValueError('synthetic observation is too large')
        shelf, slots = frame.get('shelf'), frame.get('slots')
        if (not isinstance(shelf, dict) or type(shelf.get('ok')) is not bool
                or not isinstance(slots, list) or len(slots) > 4):
            raise ValueError('invalid synthetic shelf observation')
        index = int(shelf_id[1:]) - 1
        if shelf['ok']:
            marker = shelf.get('marker_id')
            if type(marker) is not int or not 4 * index + 1 <= marker <= 4 * index + 4:
                raise ValueError('synthetic shelf marker differs from selected shelf')
        parsed = [{'slot_id': 4 * index + i, 'state': 'unknown', 'product_type': None}
                  for i in range(1, 5)]
        if not shelf['ok']:
            return parsed
        seen = set()
        for item in slots:
            if not isinstance(item, dict):
                raise ValueError('invalid synthetic slot')
            local, state = item.get('slot'), item.get('state')
            if type(local) is not int or not 1 <= local <= 4 or local in seen or type(state) is not str:
                raise ValueError('invalid or duplicate synthetic slot')
            seen.add(local)
            if state in PRODUCT_MARKERS:
                if type(item.get('marker_id')) is not int or item['marker_id'] != PRODUCT_MARKERS[state]:
                    raise ValueError('synthetic product marker differs from product')
                parsed[local - 1].update(state='occupied', product_type=state)
            elif state in ('empty', 'unknown'):
                if 'marker_id' in item:
                    raise ValueError('unexpected product marker for empty or unknown slot')
                parsed[local - 1].update(state=state, product_type=None)
            else:
                raise ValueError('unknown synthetic slot state')
        return parsed

    def accept_frame(self, token, shelf_id, frame):
        with self._lock:
            now = self._now()
            batch = self._batch
            if not batch or token != batch['token'] or now - batch['started'] > self._max_age:
                self._batch = None
                raise ValueError('synthetic capture is expired or does not match')
            try:
                parsed = self._parse(shelf_id, frame)
                previous = batch['frames'].get(shelf_id)
                if previous is not None and previous != parsed:
                    raise ValueError('conflicting synthetic shelf retransmission')
                batch['frames'][shelf_id] = parsed
                if len(batch['frames']) != 6:
                    return False
                rows = [row for name in sorted(batch['frames']) for row in batch['frames'][name]]
                self._store.apply_snapshot(self._session, rows, fence=batch['fence'])
            except BaseException:
                self._batch = None
                raise
            self._batch = None
            self._fresh_at = now
            return True

    def _require_fresh(self):
        now = self._now()
        if self._batch is not None or self._fresh_at is None or now - self._fresh_at > self._max_age:
            self._fresh_at = None
            self._store.begin_snapshot(self._session, invalidate=True)
            raise ValueError('a fresh completed synthetic capture is required')

    def shortages(self, *, threshold):
        if type(threshold) is not int or not 1 <= threshold <= 4:
            raise ValueError('synthetic threshold must be between 1 and 4')
        with self._lock:
            self._require_fresh()
            with self._store._write() as db:
                self._store._active(db, self._session, fresh=True)
                self._store._admission()
                claims = {row[0] for row in db.execute('SELECT slot_id FROM claims')}
                slots = db.execute('SELECT slot_id,shelf_id,state,product_type FROM slots').fetchall()
                result = {}
                for product, shelf in PRODUCT_SHELVES.items():
                    selected = [row for row in slots if row[1] == shelf]
                    unknown = sum(row[2] == 'unknown' or row[0] in claims for row in selected)
                    mismatch = sum(row[2] == 'occupied' and row[3] != product for row in selected)
                    known = sum(row[2] == 'occupied' and row[3] == product and row[0] not in claims
                                for row in selected)
                    empty = sum(row[2] == 'empty' and row[0] not in claims for row in selected)
                    status = ('unknown' if unknown else 'mismatch' if mismatch else
                              'shortage' if known < threshold else 'sufficient')
                    result[product] = {'shelf_id': shelf, 'status': status, 'known_count': known,
                                       'empty_slots': empty, 'unknown_slots': unknown,
                                       'threshold': threshold, 'source': 'synthetic_observation'}
                return copy.deepcopy(result)

    def approve_and_dispatch(self, dispatcher, request_id, product_type, task_id, candidate,
                             *, threshold, approved):
        """Explicitly approve one shortage and publish a simulated route only.

        This reserves both slots before the existing loopback MQTT navigation.
        Neither the observation nor broker acknowledgement proves an actual
        pick, place, physical stock, or a robot's arrival.
        """
        from server.team_shelf_sim_dispatch import TeamShelfSimDispatch
        if (approved is not True or type(dispatcher) is not TeamShelfSimDispatch
                or dispatcher.slots is not self._store or type(product_type) is not str
                or product_type not in PRODUCT_SHELVES):
            raise ValueError('explicit approval and the bound simulated dispatcher are required')
        dispatcher.validate_candidate(task_id, candidate)
        # Serialize the shortage check with the slot claim in this store. The
        # journal transaction checks the active session and current claims.
        # No store lock is held across the network publish.
        with self._lock, self._store._lock:
            if self.shortages(threshold=threshold)[product_type]['status'] != 'shortage':
                raise ValueError('a known simulated shortage is required')
            self._store.reserve(self._session, request_id, product_type)
        return dispatcher.publish_reserved_pickup(self._session, request_id, task_id, candidate)
