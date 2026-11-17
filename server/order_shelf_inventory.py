"""Conservative shelf-slot allocation for a single authenticated demo session.

The data comes from an explicitly authenticated Unreal snapshot and applies
only to this in-process simulation; it is not a physical inventory system.
"""
import copy
import threading
import uuid


class ShelfInventory:
    """Track observed occupied slots and preserve unresolved reservations."""

    def __init__(self, shelves, *, slot_count=6, store=None):
        if (not isinstance(shelves, dict) or not shelves or len(shelves) > 64
                or type(slot_count) is not int or not 1 <= slot_count <= 128):
            raise ValueError('explicit bounded simulated shelves are required')
        for shelf, product in shelves.items():
            if (not isinstance(shelf, str) or not shelf.strip() or len(shelf) > 128
                    or not isinstance(product, str) or not product.strip() or len(product) > 128):
                raise ValueError('invalid simulated shelf catalog')
        self._shelves = dict(shelves)
        self._slot_count = slot_count
        self._lock = threading.RLock()
        self._owner = None
        self._generation = 0
        self._revision = 0
        self._pending = None
        self._observed = None
        self._reservations = {}
        self._store = store
        self._store_revision = None
        if store is not None:
            saved = store.load_shelves()
            if not isinstance(saved, dict) or type(saved.get('revision')) is not int:
                raise ValueError('invalid durable simulated shelf state')
            rows = saved.get('reservations')
            if not isinstance(rows, list):
                raise ValueError('invalid durable simulated reservations')
            self._store_revision = saved['revision']
            for item in rows:
                if (not isinstance(item, dict) or item.get('shelf_id') not in self._shelves
                        or type(item.get('slot_index')) is not int
                        or not 0 <= item['slot_index'] < self._slot_count
                        or item.get('state') not in ('reserved', 'confirmed')
                        or not isinstance(item.get('reservation_id'), str)
                        or not item['reservation_id']
                        or item['reservation_id'] in self._reservations):
                    raise ValueError('invalid durable simulated reservation')
                self._reservations[item['reservation_id']] = {
                    'shelf_id': item['shelf_id'], 'slot_index': item['slot_index'],
                    # Prior session acknowledgements can never confirm an
                    # unresolved reservation in a new connection generation.
                    'generation': None, 'state': item['state']}

    @property
    def shelf_ids(self):
        return tuple(sorted(self._shelves))

    @property
    def ready(self):
        with self._lock:
            return self._owner is not None and self._observed is not None and self._pending is None

    def _require_owner(self, owner):
        if owner is None or self._owner is not owner:
            raise ValueError('stale or unauthenticated Unreal owner')

    def _new_request(self):
        request_id = uuid.uuid4().hex
        self._pending = (request_id, self._generation, self._revision)
        return request_id

    def begin_session(self, owner):
        if owner is None:
            raise ValueError('authenticated Unreal owner required')
        with self._lock:
            if self._owner is not None and self._owner is not owner:
                raise ValueError('previous Unreal owner is still active')
            self._owner = owner
            self._generation += 1
            self._observed = None
            return self._new_request()

    def request_snapshot(self, owner):
        with self._lock:
            self._require_owner(owner)
            return self._new_request()

    def invalidate(self, owner):
        with self._lock:
            if owner is not None and self._owner is owner:
                self._owner = None
                self._pending = None
                self._observed = None
                self._generation += 1

    def apply_snapshot(self, owner, data):
        with self._lock:
            self._require_owner(owner)
            if (not isinstance(data, dict) or set(data) != {'request_id', 'shelves'}
                    or self._pending is None or data['request_id'] != self._pending[0]
                    or self._pending[1:] != (self._generation, self._revision)):
                raise ValueError('stale or invalid simulated shelf snapshot')
            shelves = data['shelves']
            if not isinstance(shelves, list) or len(shelves) != len(self._shelves):
                raise ValueError('incomplete simulated shelf snapshot')
            observed = {}
            for item in shelves:
                if not isinstance(item, dict) or set(item) != {'shelf_id', 'product_type', 'slots'}:
                    raise ValueError('invalid simulated shelf entry')
                shelf, product, slots = item['shelf_id'], item['product_type'], item['slots']
                if (not isinstance(shelf, str) or shelf not in self._shelves or shelf in observed
                        or product != self._shelves[shelf] or not isinstance(slots, list)
                        or len(slots) != self._slot_count or any(type(value) is not bool for value in slots)):
                    raise ValueError('invalid simulated shelf contents')
                observed[shelf] = tuple(slots)
            if set(observed) != set(self._shelves):
                raise ValueError('missing simulated shelf')
            if self._store is not None:
                # DB validation is authoritative and preserves unresolved
                # reservations; no observed empty slot may clear them.
                self._store_revision = self._store.sync_shelves(shelves)
            self._observed = observed
            self._pending = None
            return True

    def _free_index(self, shelf_id, product_type):
        if not self.ready:
            raise RuntimeError('current Unreal shelf snapshot is not available')
        if (not isinstance(shelf_id, str) or shelf_id not in self._shelves
                or self._shelves[shelf_id] != product_type):
            raise ValueError('shelf and product do not match')
        held = {item['slot_index'] for item in self._reservations.values()
                if item['shelf_id'] == shelf_id and item['state'] == 'reserved'}
        return next((i for i, occupied in enumerate(self._observed[shelf_id])
                     if not occupied and i not in held), None)

    def can_reserve(self, shelf_id, product_type):
        """Report availability only from the currently synchronized snapshot."""
        with self._lock:
            return self._free_index(shelf_id, product_type) is not None

    def reserve(self, shelf_id, product_type, *, request_id=None):
        with self._lock:
            index = self._free_index(shelf_id, product_type)
            if index is None:
                raise RuntimeError('no confirmed empty simulated shelf slot')
            if self._store is None:
                reservation_id = uuid.uuid4().hex
            else:
                stored = self._store.reserve_slot(shelf_id, product_type,
                                                   self._store_revision, request_id=request_id)
                if (not isinstance(stored, dict) or stored.get('shelf_id') != shelf_id
                        or not isinstance(stored.get('reservation_id'), str)
                        or not stored['reservation_id']
                        or type(stored.get('slot_index')) is not int
                        or not 0 <= stored['slot_index'] < self._slot_count
                        or self._observed[shelf_id][stored['slot_index']]
                        or stored['slot_index'] in {item['slot_index'] for item in self._reservations.values()
                                                    if item['shelf_id'] == shelf_id and item['state'] == 'reserved'}):
                    raise RuntimeError('invalid durable simulated slot reservation')
                index, reservation_id = stored['slot_index'], stored['reservation_id']
            self._reservations[reservation_id] = {'shelf_id': shelf_id, 'slot_index': index,
                                                   'generation': self._generation, 'state': 'reserved'}
            return {'reservation_id': reservation_id, 'shelf_id': shelf_id, 'slot_index': index}

    def confirm(self, reservation_id):
        with self._lock:
            item = self._reservations.get(reservation_id)
            if (self._owner is None or self._observed is None or not isinstance(item, dict)
                    or item['generation'] != self._generation or item['state'] != 'reserved'):
                raise ValueError('unknown, stale, or already confirmed reservation')
            if self._store is not None:
                self._store.confirm_slot(reservation_id)
            slots = list(self._observed[item['shelf_id']])
            slots[item['slot_index']] = True
            self._observed[item['shelf_id']] = tuple(slots)
            item['state'] = 'confirmed'
            self._revision += 1
            return True

    def snapshot(self):
        with self._lock:
            return {'ready': self.ready, 'shelves': copy.deepcopy(self._observed),
                    'reservations': copy.deepcopy(self._reservations)}
