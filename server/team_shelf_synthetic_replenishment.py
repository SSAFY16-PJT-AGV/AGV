"""Synthetic shortage approval into the existing team-shelf web workflow.

The six synthetic frames are derived only from the most recent authenticated
Unreal slot snapshot in the same process/world/session. They are not camera
observations or evidence of any physical shelf or robot.
"""
import threading
import time
import math

from server.order_demo_admission import AdmissionConflict, AdmissionOutcomeUnknown, AdmissionStopped
from server.simulated_shelf_perception import SimulatedShelfPerception
from server.team_shelf_order_slots import PRODUCT_MARKERS
from server.team_shelf_pickup_gate import pickup_is_clear
from server.team_shelf_web_adapter import TeamShelfSimInventory


class SyntheticReplenishment:
    def __init__(self, demo, *, clock=time.monotonic, max_age_seconds=5.0,
                 refresh_timeout_seconds=3.0):
        inventory = getattr(getattr(demo, 'engine', None), 'inventory', None)
        if (getattr(demo, 'team_shelves', False) is not True
                or type(inventory) is not TeamShelfSimInventory or not callable(clock)
                or type(refresh_timeout_seconds) not in (int, float)
                or not math.isfinite(refresh_timeout_seconds) or not 0 < refresh_timeout_seconds <= 10):
            raise ValueError('explicit team-shelf simulation required')
        if demo.journal is not None:
            from server.team_shelf_persistent_orders import TeamShelfPersistentOrders
            if type(demo.journal) is not TeamShelfPersistentOrders or demo.journal.slots is not inventory.store:
                raise ValueError('bound persistent team-shelf journal required')
        self.demo, self.inventory = demo, inventory
        self.clock, self.max_age = clock, max_age_seconds
        self.refresh_timeout = float(refresh_timeout_seconds)
        self._lock = threading.RLock()
        self._perception = self._owner = self._session = self._world = self._source = None
        self._durable_approvals = {}

    def _bound(self):
        owner = self.demo.actions.owner
        if (owner is None or owner is not self.inventory.owner or not self.demo.actions.available_fast()
                or not self.inventory.ready or self.inventory.session is None or not self.inventory.world_id):
            raise ConnectionError('a fresh authenticated simulated world is required')
        return owner, self.inventory.session, self.inventory.world_id

    def _ensure_fresh_source(self):
        """Request a correlated UE observation without blocking its receiver."""
        with self.demo.lock, self.demo.actions.motion_guard() as available:
            if not available or not self.demo._ready():
                raise ConnectionError('the simulated world is unavailable')
            owner, session, world = self._bound()
            if self.inventory.fresh_snapshot(self.max_age) is not None:
                return
            actions = self.demo.actions
            if actions.sent_at is not None:
                raise ConnectionError('a simulated action is still pending')
            if self.inventory.pending is None:
                actions._shelf_request_id = self.inventory.request_snapshot(owner)
                actions._shelf_request_sent = False
                actions._shelf_requested_at = None
            event = self.inventory.snapshot_event
        if not event.wait(self.refresh_timeout):
            raise ConnectionError('Unreal snapshot refresh timed out')
        with self.demo.lock, self.demo.actions.motion_guard() as available:
            if (not available or self._bound() != (owner, session, world)
                    or self.inventory.fresh_snapshot(self.max_age) is None):
                raise ConnectionError('Unreal snapshot refresh was not confirmed')

    def capture(self, *, threshold):
        """Derive a six-frame synthetic view from the current UE slot journal."""
        self._ensure_fresh_source()
        with self.demo.lock, self.demo.actions.motion_guard() as available, self._lock:
            if not available or not self.demo._ready():
                raise ConnectionError('the simulated world is unavailable')
            owner, session, world = self._bound()
            source = self.inventory.fresh_snapshot(self.max_age)
            if source is None:
                raise ValueError('a recent Unreal snapshot is required')
            store = self.inventory.store
            # The same store lock covers reading the UE snapshot and applying
            # the synthetic view; the capture's mutation fence rejects any
            # concurrent write. Claimed slots become unknown, never empty.
            with store._lock:
                current = store.inspect()
                held = {row['slot_id'] for row in current['claims']}
                perception = SimulatedShelfPerception(store, session, clock=self.clock,
                                                     max_age_seconds=self.max_age)
                token = perception.begin_capture()
                for number in range(1, 7):
                    shelf = 'N%02d' % number
                    slots = []
                    for row in current['slots']:
                        if row['shelf_id'] != shelf:
                            continue
                        state, product = row['state'], row['product_type']
                        if row['slot_id'] in held:
                            state, product = 'unknown', None
                        item = {'slot': row['slot_index'] + 1,
                                'state': product if state == 'occupied' else state}
                        if state == 'occupied':
                            item['marker_id'] = PRODUCT_MARKERS[product]
                        slots.append(item)
                    perception.accept_frame(token, shelf, {'frame': 'robot_base',
                        'shelf': {'ok': True, 'marker_id': (number - 1) * 4 + 1}, 'slots': slots})
                result = perception.shortages(threshold=threshold)
            if (self._bound() != (owner, session, world)
                    or self.inventory.fresh_snapshot(self.max_age) != source):
                raise ConnectionError('the simulated world changed during capture')
            self._perception, self._owner, self._session, self._world = perception, owner, session, world
            self._source = source
            return result

    def submit(self, product_id, *, threshold, approved, request_key):
        if approved is not True or not isinstance(request_key, str) or not request_key:
            raise ValueError('explicit approval and request key required')
        product = next((x for x in self.demo.products if x['id'] == product_id), None)
        if product is None:
            raise ValueError('unknown simulated product')
        if self.demo.journal is not None:
            return self._submit_durable(product, threshold, request_key)
        with self._lock:
            if self._perception is None or self._bound() != (self._owner, self._session, self._world):
                raise ValueError('a fresh capture in the same world and session is required')
            perception, owner, session, world = self._perception, self._owner, self._session, self._world
            source = self._source
            if source is None or self.inventory.fresh_snapshot(self.max_age) != source:
                raise ValueError('the underlying Unreal snapshot expired or changed')

        def operation(request_id):
            with self.demo.lock, self.demo.actions.motion_guard() as available, self._lock:
                if (not available or not self.demo._ready() or self._bound() != (owner, session, world)
                        or self._perception is not perception
                        or self.inventory.fresh_snapshot(self.max_age) != source):
                    raise ConnectionError('the approved simulated world changed')
                state = self.demo.engine.snapshot()
                robots = state.get('robots')
                if (not isinstance(robots, list) or any(x.get('status') in ('unknown', 'failed') for x in robots)
                        or state.get('accepting_orders') is not True
                        or not self.demo.engine.can_dispatch_destination(product['destination_node'])):
                    raise ValueError('the simulated fleet cannot accept this approval')
                store = self.inventory.store
                try:
                    runtime = self.demo.map_runtime
                    zones = {zone['id'] for zone in runtime.map_data['zones']
                             if zone.get('type') == 'shared' and set(zone.get('nodes', ())) & {'D01', 'N01'}}
                    clear = pickup_is_clear(store, runtime, zones)
                except (AttributeError, KeyError, TypeError, ValueError):
                    clear = False
                if not clear:
                    raise ValueError('the simulated pickup is occupied or unknown')
                with perception._lock, store._lock:
                    if perception.shortages(threshold=threshold)[product['product_type']]['status'] != 'shortage':
                        raise ValueError('a fresh known synthetic shortage is required')
                    store.reserve(session, request_id, product['product_type'])
                try:
                    return self.demo.create_product_order(product_id, request_id=request_id)
                except BaseException as exc:
                    # Reservation and engine state live in separate DBs. Any
                    # error after claiming is ambiguous: keep both claims and
                    # stop the session rather than retrying or releasing.
                    try:
                        fail = getattr(self.demo.actions, '_fail', None)
                        if callable(fail):
                            fail()
                        else:
                            self.demo.actions.invalidate_owner(owner)
                            self.demo.engine.action_unknown()
                            self.inventory.invalidate(owner)
                    except Exception as failure:
                        exc.add_note('synthetic session invalidation failed: ' + type(failure).__name__)
                    raise AdmissionOutcomeUnknown('synthetic dispatch outcome is unknown') from exc

        return self.demo._submit(operation, 'synthetic-replenishment:{}:{}'.format(product_id, threshold),
                                 request_key, pass_request_id=True)

    def _submit_durable(self, product, threshold, request_key):
        if type(threshold) is not int or not 1 <= threshold <= 4:
            raise ValueError('valid synthetic shortage threshold required')
        with self.demo.lock, self.demo.actions.motion_guard() as available, self._lock:
            journal = self.demo.journal
            try:
                old = journal.get_order_by_key(request_key)
            except KeyError:
                old = None
            if old is not None:
                if journal.get_approval(old['request_id']) != (product['id'], threshold):
                    raise AdmissionConflict('request key belongs to a different order')
                if self.demo._restart_blocked:
                    return {'status': 'unknown', 'request_id': old['request_id']}
                return self.demo.admission.get_by_key(request_key)
            if self.demo._restart_blocked:
                raise AdmissionStopped('simulation recovery requires investigation')
            if not available or not self.demo._ready():
                raise ConnectionError('the simulated world is unavailable')
            owner, session, world = self._bound()
            if (self._perception is None or (owner, session, world) != (self._owner, self._session, self._world)
                    or self._source is None or self.inventory.fresh_snapshot(self.max_age) != self._source):
                raise ValueError('a fresh capture in the same world and session is required')
            if self._perception.shortages(threshold=threshold)[product['product_type']]['status'] != 'shortage':
                raise ValueError('a fresh known synthetic shortage is required')
            state = self.demo.engine.snapshot()
            robots = state.get('robots')
            if (not isinstance(robots, list) or any(row.get('status') in ('unknown', 'failed') for row in robots)
                    or state.get('accepting_orders') is not True
                    or not self.demo.engine.can_dispatch_destination(product['destination_node'])):
                raise ValueError('the simulated fleet cannot accept this approval')
            try:
                runtime = self.demo.map_runtime
                zones = {zone['id'] for zone in runtime.map_data['zones']
                         if zone.get('type') == 'shared' and set(zone.get('nodes', ())) & {'D01', 'N01'}}
                clear = pickup_is_clear(self.inventory.store, runtime, zones)
            except (AttributeError, KeyError, TypeError, ValueError):
                clear = False
            if not clear:
                raise ValueError('the simulated pickup is occupied or unknown')
            order = journal.submit_approved(product['id'], request_key, threshold=threshold)
            if not order.get('created'):
                return self.demo.admission.get_by_key(request_key)
            self._durable_approvals[order['request_id']] = (self._perception, owner, session, world, self._source)
            self.demo.admission.resume()
            return {'status': 'pending', 'request_id': order['request_id']}

    def validate_durable(self, order):
        """Revalidate the in-process approval under the existing dispatch fence.

        The journal remembers the approval request, never authority to move.
        After process restart the temporary proof is absent and dispatch fails
        before claiming slots or publishing any command.
        """
        approval = self.demo.journal.get_approval(order['request_id'])
        if approval is None:
            return True
        product_id, threshold = approval
        product = next((x for x in self.demo.products if x['id'] == product_id), None)
        with self.demo.lock, self.demo.actions.motion_guard() as available, self._lock:
            saved = self._durable_approvals.get(order['request_id'])
            if (not available or not self.demo._ready() or product is None or saved is None
                    or self._bound() != saved[1:4] or self._perception is not saved[0]
                    or self.inventory.fresh_snapshot(self.max_age) != saved[4]):
                raise ValueError('synthetic approval expired or its world changed')
            if saved[0].shortages(threshold=threshold)[product['product_type']]['status'] != 'shortage':
                raise ValueError('the approved synthetic shortage is no longer current')
            return True
