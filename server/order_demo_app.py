"""Separate, loopback-only web ordering demo for a synthetic AGV.

This process publishes simulated observations, never physical robot commands.
Launch it via the isolated demo launcher, not the production server.
"""
from contextlib import asynccontextmanager, ExitStack, nullcontext
import copy
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import secrets
import threading
import time
import tempfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr
import paho.mqtt.client as mqtt

from server.mqtt_client import start_mqtt, stop_mqtt
from server.order_demo_actions import DemoActionSession
from server.order_demo_admission import (AdmissionBusy, AdmissionConflict, AdmissionOutcomeUnknown,
                                         AdmissionStopped, OrderAdmission)
from server.twin_websocket import router as twin_router

PRODUCTS = (
    {'id': 'red', 'label': '빨간 상품', 'product_type': 'Red', 'pickup_node': 'D01',
     'destination_node': 'D04', 'shelf_id': 'SHELF_RED', 'slot_index': 0, 'slot_count': 6},
    {'id': 'blue', 'label': '파란 상품', 'product_type': 'Blue', 'pickup_node': 'D06',
     'destination_node': 'D05', 'shelf_id': 'SHELF_BLUE', 'slot_index': 0, 'slot_count': 6},
)

# Labels for local simulated observations only; they do not authorize a real
# robot to move or assert its physical state.
SIMULATION_STATUS = {'moving': 'MOVING', 'waiting': 'WAITING', 'failed': 'ERROR',
                     'unknown': 'ERROR', 'idle': 'IDLE', 'completed': 'IDLE'}


class ShelfFull(RuntimeError):
    """No confirmed empty slot remains in the simulated shelf."""


class DemoRuntime:
    def __init__(self, engine, port, nodes, observer=None, *, snapshot_max_age=1.0, clock=time.monotonic,
                 auto_recovery=False, journal=None, recovery_blocked=False, pickup_store=None,
                 field_mapping=None, products=None, action_source='unreal', map_runtime=None):
        self.engine = engine
        self.map_runtime = map_runtime
        self._action_source = action_source
        self.field_mapping = field_mapping
        self.products = copy.deepcopy(products if products is not None else PRODUCTS)
        self.port = port
        self._nodes = nodes
        self.observer = observer
        self.lock = threading.RLock()
        if not callable(clock) or not isinstance(snapshot_max_age, (int, float)) or snapshot_max_age <= 0:
            raise ValueError('a positive snapshot maximum age and clock are required')
        self.clock = clock
        self.snapshot_max_age = snapshot_max_age
        self._snapshot_lock = threading.Lock()
        try:
            self._cached_snapshot = copy.deepcopy(engine.snapshot())
        except Exception:
            self._cached_snapshot = {'status': 'unknown'}
        self._fleet_mode = self._cached_snapshot.get('mode') == 'fleet'
        self._snapshot_at = self.clock()
        self._snapshot_valid = True
        self._cached_ready = False
        self.auto_recovery = auto_recovery is True
        self.journal = journal
        self.pickup_store = pickup_store
        self._pickup_token = None
        self._pickup_fence = None
        self._restart_blocked = recovery_blocked
        self._world_ready = threading.Event()
        self._bootstrap_pending = False
        self._bootstrap_checked_at = 0.0
        self._last_persisted_observation = {}
        self._recovering = threading.Event()
        self._recovery_checkpoints = None
        self._recovery_owner = None
        self.actions = DemoActionSession(engine, self.lock, can_dispatch=self._ready,
                                         on_change=self._invalidate_snapshot,
                                         pause_on_unavailable=self.auto_recovery,
                                         inventory=getattr(engine, 'inventory', None),
                                         validate_snapshot=self._validate_world if journal else None,
                                         on_snapshot=self._accept_world if journal else None,
                                         on_invalidate=self._invalidate_world if journal else None,
                                         can_send_state=self._may_send_state if journal else None,
                                         on_snapshot_request=self._pickup_snapshot_request if pickup_store else None,
                                         on_prepare_action=self._pickup_action_prepare if pickup_store else None,
                                         on_action_result=self._pickup_action_result if pickup_store else None,
                                         on_detach=self._pickup_detach if pickup_store else None,
                                         pose_transform=field_mapping.pose if field_mapping is not None else None,
                                         action_source=action_source)
        self.connected = threading.Event()
        self.stop_event = threading.Event()
        if journal is None:
            self.admission = OrderAdmission()
        else:
            from server.order_persistent_admission import PersistentAdmission
            self.admission = PersistentAdmission(journal, self._dispatch_persistent, self.stop_event)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.thread = None

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            self.connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()

    def start(self):
        self.client.connect('127.0.0.1', self.port, keepalive=15)
        self.client.loop_start()
        if not self.connected.wait(10):
            self.client.loop_stop()
            self.client.disconnect()
            raise RuntimeError('isolated demo publisher failed to connect')
        self.thread = threading.Thread(target=self._run, name='order-demo', daemon=True)
        self.thread.start()
        if self.journal is not None:
            self.admission.resume()

    def stop(self):
        self.stop_event.set()
        self._invalidate_snapshot()
        self.admission.shutdown()
        # Never tear down shared MQTT/DB resources while an accepted request
        # is still using them; it remains unknown after shutdown.
        self.admission.join()
        if self.thread is not None:
            self.thread.join()
        self.client.disconnect()
        self.client.loop_stop()

    def nodes(self):
        return [dict(node) for node in self._nodes]

    def _ready(self):
        return (self._transport_ready()
                and (not self.auto_recovery or self.actions.available_fast())
                and (self.journal is None or (self._world_ready.is_set()
                     and not self._restart_blocked and self.engine.inventory.ready)))

    def _may_send_state(self):
        # Nonblocking outbound fence. No restored server pose may reach the UE
        # actor before its same-world position has been independently checked.
        return (not self._restart_blocked and self.actions.available_fast()
                and (self._world_ready.is_set() or self._bootstrap_pending))

    def _invalidate_world(self):
        self._world_ready.clear()
        self._bootstrap_pending = False

    def _validate_world(self, data):
        """Validate an authenticated Unreal snapshot before allowing any tick."""
        expected_keys = {'request_id', 'shelves', 'world_id', 'robots'}
        if self.pickup_store is not None:
            expected_keys.add('pickups')
        if not isinstance(data, dict) or set(data) != expected_keys:
            raise ValueError('simulated world proof required')
        world_id = data['world_id']
        if not isinstance(world_id, str) or len(world_id) != 32 or any(c not in '0123456789abcdefABCDEF' for c in world_id):
            raise ValueError('invalid simulated world identity')
        previous = self.journal.world_id
        if previous is not None and previous != world_id.lower():
            raise ValueError('Unreal world changed since the saved simulation')
        robots = data['robots']
        if not isinstance(robots, list) or len(robots) != 2:
            raise ValueError('two simulated robot observations required')
        observed = {}
        for robot in robots:
            if (not isinstance(robot, dict) or set(robot) != {'agv_id', 'x', 'y', 'yaw', 'carrying', 'interpolating'}
                    or robot['agv_id'] not in ('AGV_01', 'AGV_02') or robot['agv_id'] in observed
                    or any(type(robot[k]) not in (int, float) or not math.isfinite(robot[k]) for k in ('x', 'y', 'yaw'))
                    or type(robot['carrying']) is not bool or type(robot['interpolating']) is not bool):
                raise ValueError('invalid simulated robot observation')
            observed[robot['agv_id']] = robot
        # During a running session later inventory refreshes are allowed while
        # robots are moving. After a restart, compare actual actors to the
        # independently restored terminal positions before the first tick.
        self._validated_bootstrap = False
        if not self._world_ready.is_set() and not self._restart_blocked:
            mismatch = False
            for engine in self.engine.engines.values():
                seen, state = observed[engine.TWIN], engine.snapshot()
                pose = state.get('pose')
                if (seen['carrying'] or not isinstance(pose, dict)
                        or any(type(pose.get(key)) not in (int, float)
                               or not math.isfinite(pose[key]) for key in ('x', 'y', 'th'))):
                    raise ValueError('Unreal robot does not match the saved stopped position')
                # Unreal normalizes rotations to [-180, 180]; compare on a
                # circle so +180 and -180 describe the same direction.
                transformed = (self.field_mapping.pose(pose) if self.field_mapping is not None else
                               {'x': pose['x'] / 1000, 'y': pose['y'] / 1000, 'yaw': pose['th']})
                yaw_difference = abs((seen['yaw'] - transformed['yaw'] + 180.0) % 360.0 - 180.0)
                mismatch |= (seen['interpolating'] or
                             math.hypot(seen['x'] - transformed['x'], seen['y'] - transformed['y']) > .25 or
                             yaw_difference > 1.0)
            if mismatch:
                # A pristine synthetic world needs an initial placement: the
                # UE map's editor positions differ from the demo start nodes.
                # Once any task or reservation exists this is never permitted.
                pristine = (all(row['state'] == 'queued' for row in self.journal.list_orders())
                            and not self.journal.load_shelves()['reservations']
                            and all(item.snapshot()['status'] == 'idle' for item in self.engine.engines.values()))
                if not pristine:
                    raise ValueError('Unreal robot does not match the saved stopped position')
                self._validated_bootstrap = True
        self._validated_world = world_id
        if self.pickup_store is not None:
            self._validated_pickups = (self.field_mapping.pickups_to_field(data['pickups'])
                                       if self.field_mapping is not None else data['pickups'])

    def _accept_world(self):
        try:
            self.journal.bind_world_id(self._validated_world)
            if self.pickup_store is not None:
                if self._pickup_token is None:
                    self._pickup_token = self.pickup_store.begin_session(self._validated_world)
                    self._pickup_fence = self.pickup_store.begin_snapshot(self._pickup_token)
                if self._pickup_fence is None:
                    raise ValueError('pickup snapshot request fence missing')
                self.pickup_store.apply_snapshot(self._pickup_token, self._validated_pickups,
                                                 snapshot_fence=self._pickup_fence)
                self._pickup_fence = None
        except Exception:
            self._restart_blocked = True
            self._invalidate_world()
            self.engine.action_unknown()
            raise
        self._bootstrap_pending = self._validated_bootstrap
        if not self._bootstrap_pending:
            self._world_ready.set()
        self._store_snapshot(self.engine.snapshot(), self._transport_ready())

    def _pickup_snapshot_request(self):
        if self._pickup_token is not None:
            self._pickup_fence = self.pickup_store.begin_snapshot(self._pickup_token)

    def _pickup_detach(self):
        self._invalidate_world()
        if self._pickup_token is not None:
            self.pickup_store.end_session(self._pickup_token)
            self._pickup_token = self._pickup_fence = None
        self._record_orders(self.engine.snapshot())

    def _pickup_order(self, action):
        matches = [row for row in self.journal.list_orders()
                   if row['state'] == 'active' and isinstance(row['result'], dict)
                   and row['result'].get('order_id') == action.get('order_id')]
        if len(matches) != 1:
            raise ValueError('pickup action has no matching saved order')
        reservation = matches[0]['result'].get('pickup_reservation_id')
        if not isinstance(reservation, str):
            raise ValueError('pickup reservation missing')
        return reservation

    def _pickup_action_prepare(self, action):
        if action.get('action') == 'grab':
            self.pickup_store.mark_dispatched(self._pickup_token, self._pickup_order(action),
                                              action['order_id'], action['action_id'], action['agv_id'])

    def _pickup_action_result(self, action):
        if action.get('action') == 'grab':
            self.pickup_store.confirm(self._pickup_token, self._pickup_order(action), action['order_id'],
                                      action['action_id'], action['agv_id'], applied=action['applied'])

    def _bootstrap_world(self):
        """Only place a pristine, idle synthetic fleet; never advance a task."""
        with self.lock:
            if (not self._bootstrap_pending or self._restart_blocked or not self.actions.available_fast()
                    or not self._transport_ready() or self.journal.load_shelves()['reservations']
                    or any(row['state'] != 'queued' for row in self.journal.list_orders())):
                return
            with self.actions.motion_guard() as available:
                if not available:
                    return
                for item in self.engine.snapshot()['robots']:
                    if item['status'] != 'idle':
                        self._restart_blocked = True
                        return
                    payload = {'map_id': self.field_mapping.map_ref if self.field_mapping is not None else 'twin-validation-v1',
                               'last_node': item['current_node'],
                               'status': 'IDLE', 'battery': item['battery'], 'pose': item['pose']}
                    self.client.publish('agv/{}/state'.format(item['robot_id']), json.dumps(payload), qos=0, retain=False)
                now = self.clock()
                if now - self._bootstrap_checked_at >= 1 and self.engine.inventory.ready:
                    self.actions._shelf_request_id = self.engine.inventory.request_snapshot(self.actions.owner)
                    self.actions._shelf_request_sent = False
                    self._bootstrap_checked_at = now

    def _dispatch_persistent(self, order):
        product = self.journal.products[order['product_id']]
        while not self.stop_event.is_set():
            with self.lock:
                state = self.engine.snapshot()
                self._record_orders(state)
                if self._ready() and self.actions.available_fast():
                    inventory = self.engine.inventory
                    if not inventory.can_reserve(product['shelf_id'], product['product_type']):
                        raise ShelfFull('no confirmed empty simulated shelf slot')
                    if (state.get('accepting_orders') is True
                            and self.engine.can_dispatch_destination(product['destination_node'])):
                        with self.actions.motion_guard() as owner_available:
                            if not owner_available or not self._ready():
                                continue
                            pickup_reservation = None
                            if self.pickup_store is not None:
                                pickup_reservation = self.pickup_store.reserve(
                                    self._pickup_token, order['request_id'], product['pickup_node'],
                                    product['product_type'])
                                if pickup_reservation.get('state') != 'reserved':
                                    self._restart_blocked = True
                                    raise RuntimeError('saved pickup reservation requires reconciliation')
                            try:
                                self.journal.claim(order['request_id'])
                            except Exception:
                                if pickup_reservation is not None:
                                    try:
                                        self.pickup_store.cancel_reserved(
                                            self._pickup_token, pickup_reservation['reservation_id'])
                                    except Exception:
                                        self._restart_blocked = True
                                raise
                            try:
                                result = self.engine.create_product_order(
                                    product['product_type'], product['pickup_node'], product['destination_node'],
                                    product['shelf_id'], product['slot_index'], request_id=order['request_id'])
                                if result.get('status') in ('unknown', 'failed'):
                                    raise RuntimeError('simulated dispatch outcome unavailable')
                                if pickup_reservation is not None:
                                    result['pickup_reservation_id'] = pickup_reservation['reservation_id']
                                self.journal.mark_active(order['request_id'], result)
                            except Exception:
                                self._restart_blocked = True
                                self.engine.action_unknown()
                                current = self.journal.get_order(order['request_id'])
                                if current['state'] in ('dispatching', 'active'):
                                    self.journal.mark_unknown(order['request_id'], 'dispatch outcome unavailable')
                                raise
                            self._store_snapshot(self.engine.snapshot(), self._ready())
                            return
            self.stop_event.wait(.05)
        raise AdmissionStopped('demo stopped before dispatch')

    def _transport_ready(self):
        if self.stop_event.is_set() or self._recovering.is_set() or not self.connected.is_set():
            return False
        try:
            if getattr(self.engine, 'control_ready', True) is not True:
                return False
        except Exception:
            return False
        if self.observer is None:
            return True
        try:
            state = self.observer.snapshot()
            return state.get('connected') is True and state.get('ready') is True
        except Exception:
            return False

    def _refresh_waiting_snapshot(self):
        # The launcher must be able to see broker readiness before starting
        # Unreal. This cached observation does not authorize any tick or order.
        with self.lock:
            state = self.engine.snapshot()
            states = state.get('robots', [state])
            if (self._transport_ready() and (self.journal is not None or
                    (not self.actions.available_fast() and
                     all(item.get('status') in ('idle', 'completed') for item in states)))):
                self._store_snapshot(state, True)

    def _tick_if_ready(self, elapsed):
        with self.lock:
            if self.auto_recovery:
                with self.actions.motion_guard() as owner_available:
                    if not owner_available or not self._ready():
                        return None
                    state = self.engine.tick(elapsed)
                    self._record_orders(state)
                    self._store_snapshot(state, self._ready())
                    return state
            if not self._ready():
                return None
            state = self.engine.tick(elapsed)
            self._record_orders(state)
            self._store_snapshot(state, self._ready())
            return state

    def _record_orders(self, state):
        if self.journal is None:
            return
        robots = {item['robot_id']: item for item in state.get('robots', [])}
        for order in self.journal.list_orders():
            if order['state'] != 'active' or not isinstance(order['result'], dict):
                continue
            result = order['result']
            robot = robots.get(result.get('assigned_robot'), {})
            if robot.get('order_id') != result.get('order_id'):
                self.journal.mark_unknown(order['request_id'], 'simulated order identity changed')
                self._restart_blocked = True
            elif robot.get('status') == 'completed':
                checkpoint = self.engine.engines[result['assigned_robot']].export_completed_checkpoint()
                observation = {key: robot.get(key) for key in ('status', 'phase', 'current_node')}
                self.journal.mark_completed(order['request_id'], {'checkpoint': checkpoint,
                                                                    'last_observation': observation})
            elif robot.get('status') in ('unknown', 'failed'):
                if self.pickup_store is not None and self._pickup_token is not None:
                    reservation_id = result.get('pickup_reservation_id')
                    held = self.pickup_store.snapshot()['reservations']
                    if any(item['reservation_id'] == reservation_id and item['state'] == 'inflight'
                           for item in held):
                        self.pickup_store.mark_unknown(self._pickup_token, reservation_id)
                self.journal.mark_unknown(order['request_id'], 'simulated action outcome unavailable')
                self._restart_blocked = True
            else:
                observation = {key: robot.get(key) for key in ('status', 'phase', 'current_node')}
                if observation != self._last_persisted_observation.get(order['request_id']):
                    self.journal.update_active(order['request_id'], {'last_observation': observation})
                    self._last_persisted_observation[order['request_id']] = observation
    def _invalidate_snapshot(self):
        with self._snapshot_lock:
            self._snapshot_valid = False

    def _store_snapshot(self, state, ready):
        snapshot = copy.deepcopy(state)
        with self._snapshot_lock:
            self._cached_snapshot = snapshot
            self._snapshot_at = self.clock()
            self._snapshot_valid = True
            self._cached_ready = ready is True

    def snapshot(self):
        # Observation must not wait for MQTT/DB operations held under the
        # control lock. Cached state is never used for dispatch or safety.
        acquired = self.lock.acquire(blocking=False)
        try:
            with self._snapshot_lock:
                result = copy.deepcopy(self._cached_snapshot)
                age = max(0.0, self.clock() - self._snapshot_at)
                stale = not acquired or not self._snapshot_valid or age > self.snapshot_max_age
                ready = self._cached_ready
        finally:
            if acquired:
                self.lock.release()
        result['stale'] = stale
        result['connected'] = not stale and ready and self.connected.is_set() and not self.stop_event.is_set()
        result['unreal_connected'] = not stale and self.actions.available_fast()
        if self._action_source == 'synthetic':
            result['action_source'] = 'synthetic'
        if self.journal is not None:
            result['recovery_required'] = self._restart_blocked or not self._world_ready.is_set()
        if stale or not result['connected'] or not result['unreal_connected']:
            result['accepting_orders'] = False
        if result.get('mode') == 'fleet':
            robots = result.get('robots')
            result['accepting_queued_orders'] = (
                not stale and result['connected'] and result['unreal_connected']
                and not self._recovering.is_set() and result.get('shelf_inventory_ready') is True
                and isinstance(robots, list) and bool(robots)
                and all(item.get('status') not in ('unknown', 'failed') for item in robots)
                and any(item.get('orders_remaining', 0) > 0 for item in robots)
                and getattr(self.admission, 'can_accept', False) is True)
            if self.journal is not None and result['recovery_required']:
                result['accepting_orders'] = result['accepting_queued_orders'] = False
        return result

    def create_order(self, pickup, destination):
        # The admission worker may wait for the control lock after its HTTP
        # caller receives a pending ID. The operation remains exactly-once.
        self.lock.acquire()
        try:
            with self.actions.motion_guard() if self.auto_recovery else nullcontext(True) as owner_available:
                if not owner_available or self.stop_event.is_set() or not self._ready():
                    raise ConnectionError('isolated broker unavailable')
                self._invalidate_snapshot()
                try:
                    result = self.engine.create_order(pickup, destination)
                    self._store_snapshot(self.engine.snapshot(), self._ready())
                    return result
                except Exception as exc:
                    raise AdmissionOutcomeUnknown('simulated order outcome is unknown') from exc
        finally:
            self.lock.release()

    def create_product_order(self, product_id):
        product = next((item for item in self.products if item['id'] == product_id), None)
        if product is None:
            raise ValueError('unknown product')
        self.lock.acquire()
        try:
            with self.actions.motion_guard() if self.auto_recovery else nullcontext(True) as owner_available:
                if (not owner_available or self.stop_event.is_set() or not self._ready()
                        or not self.actions.available()):
                    raise ConnectionError('authenticated Unreal connection unavailable')
                self._invalidate_snapshot()
                try:
                    result = self.engine.create_product_order(product['product_type'], product['pickup_node'],
                                                              product['destination_node'], product['shelf_id'],
                                                              product['slot_index'])
                    self._store_snapshot(self.engine.snapshot(), self._ready())
                    return result
                except Exception as exc:
                    raise AdmissionOutcomeUnknown('simulated order outcome is unknown') from exc
        finally:
            self.lock.release()

    def _submit(self, operation, fingerprint, request_key=None):
        # A request accepted by admission is never silently cancelled or
        # retried. The operation itself rejects before mutation if busy.
        return self.admission.submit(operation, fingerprint, request_key=request_key)

    def submit_product_order(self, product_id, request_key=None):
        if not any(item['id'] == product_id for item in self.products):
            raise ValueError('unknown product')
        if self.journal is not None:
            return self.admission.submit_product(product_id, request_key)
        if self._fleet_mode:
            # Capture the owner without waiting for the potentially slow
            # control lock. The worker revalidates it under the lock before
            # making any reservation or publishing any order.
            with self.actions._owner_fast_lock:
                owner = self.actions._fast_owner if self.actions.available_fast() else None
            operation = lambda: self._wait_for_product_order(product_id, owner)
        else:
            operation = lambda: self.create_product_order(product_id)
        return self._submit(operation,
                            'product:' + product_id, request_key)

    def _wait_for_product_order(self, product_id, owner):
        """Wait outside the control lock until an admitted fleet order can start."""
        product = next(item for item in self.products if item['id'] == product_id)
        while not self.stop_event.is_set():
            with self.lock:
                if (owner is None or self.actions.owner is not owner
                        or not self.actions.available_fast()):
                    raise ConnectionError('authenticated Unreal connection changed before dispatch')
                state = self.engine.snapshot()
                robots = state.get('robots')
                if not isinstance(robots, list) or not robots:
                    raise RuntimeError('simulated fleet state unavailable')
                if any(item.get('status') in ('unknown', 'failed') for item in robots):
                    raise RuntimeError('simulated fleet requires recovery')
                if all(item.get('orders_remaining', 0) <= 0 for item in robots):
                    raise RuntimeError('simulated fleet order limit reached')
                inventory = getattr(self.engine, 'inventory', None)
                if (inventory is not None and inventory.ready
                        and not inventory.can_reserve(product['shelf_id'], product['product_type'])):
                    raise ShelfFull('no confirmed empty simulated shelf slot')
                destination_ready = getattr(self.engine, 'can_dispatch_destination', lambda _destination: True)
                if (self._ready() and state.get('accepting_orders') is True
                        and destination_ready(product['destination_node'])):
                    # RLock holds readiness, the chosen owner and dispatch in
                    # one serialized operation. The existing method applies
                    # its own motion fence and repeats safety checks.
                    return self.create_product_order(product_id)
            self.stop_event.wait(.05)
        raise AdmissionStopped('demo stopped before dispatch')

    def submit_order(self, pickup, destination, request_key=None):
        return self._submit(lambda: self.create_order(pickup, destination),
                            json.dumps(['nodes', pickup, destination], ensure_ascii=True), request_key)

    def _enter_recovery(self):
        with self.lock:
            if not self._recovering.is_set():
                if (not self.actions.available_fast() and
                        all(item.snapshot()['status'] in ('idle', 'completed')
                            for item in self.engine.engines.values())):
                    return
                self._recovering.set()
                self._recovery_owner = self.actions.owner if self.actions.available_fast() else None
                uncertain = self.actions.action_id if self.actions.sent_at is not None else None
                self.engine.suspend(uncertain)
                if self._recovery_owner is None:
                    self.engine.action_unknown()
                self.actions.action_id = self.actions.observed_at = self.actions.sent_at = None
                self._invalidate_snapshot()

    def _try_recovery(self):
        """Recover only this running fleet's frozen, in-memory simulation."""
        from server.sim_order_authorization import SimOrderAuthorizer
        with self.lock:
            if self.stop_event.is_set():
                return False
            if (self._recovery_owner is None or not self.actions.available_fast()
                    or self.actions.owner is not self._recovery_owner):
                self.engine.action_unknown()
                return False
            engines = self.engine.engines
            # An ambiguous in-flight Unreal action or workflow remains unknown.
            if any(item.snapshot()['status'] in ('unknown', 'failed') for item in engines.values()):
                return False
            if self._recovery_checkpoints is None:
                self._recovery_checkpoints = {
                    key: (item.robot.connected_recovery_checkpoint() if item.robot.ready
                          else item.robot.recovery_checkpoint()) for key, item in engines.items()}
            checkpoints = self._recovery_checkpoints
            runtime = next(iter(engines.values())).runtime
            if any(item.runtime is not runtime for item in engines.values()):
                raise RuntimeError('fleet runtime changed during recovery')
            if not runtime.status()['ready']:
                runtime.reconnect()
            if self.stop_event.is_set():
                return False
            # Freeze and revoke both robots' previous proofs even when only
            # one transport dropped; each active segment is then revalidated.
            for key in engines:
                runtime.invalidate_robot(key)
            for key, item in engines.items():
                if not item.robot.ready:
                    authorizer = SimOrderAuthorizer(runtime, robot_id=key)
                    item.robot.reconnect(authorization_guard=authorizer.guard)
                if self.stop_event.is_set():
                    return False
            if not self.connected.is_set():
                self.client.loop_stop()
                if self.client.reconnect() != mqtt.MQTT_ERR_SUCCESS:
                    raise ConnectionError('demo publisher reconnect failed')
                self.client.loop_start()
                if not self.connected.wait(5):
                    raise ConnectionError('demo publisher reconnect timed out')
            if self.observer is not None:
                observation = self.observer.snapshot()
                if not observation.get('connected') or not observation.get('ready'):
                    return False
            if (self.stop_event.is_set() or not self.actions.available_fast()
                    or self.actions.owner is not self._recovery_owner):
                self.engine.action_unknown()
                return False
            if any(not item.robot.ready or not item.robot.check_recovery_checkpoint(checkpoints[key])
                   for key, item in engines.items()) or not runtime.status()['ready']:
                return False
            for key, item in engines.items():
                if not item.robot.check_recovery_checkpoint(checkpoints[key]):
                    raise RuntimeError('simulated checkpoint changed during recovery')
                state = item.robot.executor.snapshot()
                if item.snapshot()['status'] == 'suspended':
                    order = state.get('order')
                    if state.get('status') != 'moving' or not isinstance(order, dict):
                        raise RuntimeError('only confirmed in-progress segments can resume automatically')
                    runtime.reconcile_moving(key, order['order_id'], item.SESSION, item.robot, checkpoints[key])
            if (self.stop_event.is_set() or not self.actions.available_fast()
                    or self.actions.owner is not self._recovery_owner):
                self.engine.action_unknown()
                return False
            if any(not item.robot.ready or not item.robot.check_recovery_checkpoint(checkpoints[key])
                   for key, item in engines.items()) or not runtime.status()['ready']:
                return False
            if not self.engine.resume_after_reconnect():
                raise RuntimeError('fleet state could not resume')
            if (self.stop_event.is_set() or not self.actions.available_fast()
                    or self.actions.owner is not self._recovery_owner):
                self.engine.action_unknown()
                return False
            self._recovery_checkpoints = None
            self._recovery_owner = None
            self._recovering.clear()
            return True

    def _run(self):
        try:
            previous = time.monotonic()
            while not self.stop_event.is_set():
                now = time.monotonic()
                if self._ready():
                    state = self._tick_if_ready(min(now - previous, 0.5))
                    if state is None:
                        previous = now
                        continue
                    states = state.get('robots') if state.get('mode') == 'fleet' else [dict(state, robot_id='sim-r1')]
                    if (not isinstance(states, list) or not states or
                            any(item.get('robot_id') not in ('sim-r1', 'sim-r2') for item in states)):
                        raise ValueError('invalid isolated robot states')
                    for item in states:
                        payload = {'map_id': self.field_mapping.map_ref if self.field_mapping is not None else 'twin-validation-v1',
                                   'last_node': item['current_node'],
                                   'status': SIMULATION_STATUS.get(item['status'], 'ERROR'),
                                   'battery': item['battery'], 'pose': item['pose']}
                        result = self.client.publish('agv/{}/state'.format(item['robot_id']), json.dumps(payload),
                                                     qos=0, retain=False)
                        if result.rc != mqtt.MQTT_ERR_SUCCESS:
                            self.connected.clear()
                            if self.auto_recovery:
                                self._enter_recovery()
                            else:
                                with self.lock:
                                    self.engine.action_unknown()
                            break
                else:
                    if self.journal is not None and (not self._world_ready.is_set() or self._restart_blocked):
                        self._bootstrap_world()
                        self._refresh_waiting_snapshot()
                        previous = now
                        self.stop_event.wait(0.2)
                        continue
                    if (self.journal is not None and self.actions.available_fast()
                            and self._transport_ready() and not self.engine.inventory.ready):
                        # A fresh shelf snapshot is pending after an ACK; this
                        # is an observation barrier, not a transport failure.
                        self._refresh_waiting_snapshot()
                        previous = now
                        self.stop_event.wait(0.2)
                        continue
                    if self.auto_recovery:
                        if not self.actions.available_fast():
                            self._refresh_waiting_snapshot()
                        self._enter_recovery()
                        if self._recovering.is_set():
                            try:
                                self._try_recovery()
                            except Exception:
                                # A disconnected test-owned broker can take time to
                                # return. No tick or new order is allowed meanwhile.
                                logging.getLogger(__name__).debug('fleet recovery pending')
                    else:
                        with self.lock:
                            self.engine.action_unknown()
                            self._invalidate_snapshot()
                previous = now
                self.stop_event.wait(0.2)
        except Exception:
            logging.getLogger(__name__).error('isolated demo worker stopped')
            self.connected.clear()
            self._invalidate_snapshot()
            with self.lock:
                if hasattr(self.engine, 'action_unknown'):
                    self.engine.action_unknown()


@asynccontextmanager
async def lifespan(app):
    # Only an explicitly configured, isolated local demo can start this app.
    if (os.environ.get('AGV_ORDER_DEMO_ENABLED') != '1'
            or os.environ.get('PYTHON_DOTENV_DISABLED') != '1'
            or os.environ.get('MQTT_HOST') != '127.0.0.1'
            or any(os.environ.get(key) for key in ('MQTT_USERNAME', 'MQTT_PASSWORD', 'MQTT_TLS_CA'))):
        raise RuntimeError('explicit isolated local demo configuration required')
    action_source = os.environ.get('AGV_ORDER_ACTION_SOURCE', 'unreal')
    if action_source not in ('unreal', 'synthetic'):
        raise ValueError('invalid simulated action source')
    if action_source == 'synthetic' and (not os.environ.get('AGV_ORDER_PERSIST_DIR')
            or any(os.environ.get(key) != '1' for key in
                   ('AGV_ORDER_COORDINATED', 'AGV_ORDER_FLEET', 'AGV_ORDER_SHELF_SYNC'))):
        raise ValueError('synthetic world requires a fresh fleet state path')
    port = int(os.environ['MQTT_PORT'])
    if not 1 <= port <= 65535:
        raise ValueError('invalid broker port')
    raw = Path(os.environ['AGV_ORDER_DEMO_MAP']).read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('demo map too large')
    map_data = json.loads(raw.decode('utf-8'))
    field_mapping = field_map = None
    if os.environ.get('AGV_ORDER_FIELD_CONFIG'):
        if os.environ.get('AGV_ORDER_COORDINATED') != '1' or os.environ.get('AGV_ORDER_FLEET') != '1':
            raise ValueError('field mapping requires the isolated coordinated fleet')
        from server.field_twin_mapping import load_field_twin_mapping
        field_map, field_mapping = load_field_twin_mapping(os.environ['AGV_ORDER_FIELD_CONFIG'], map_data)
    products = field_mapping.products if field_mapping is not None else PRODUCTS
    node_map = field_map if field_mapping is not None else map_data
    nodes = [{'id': node['id'], 'type': node.get('type', '')} for node in node_map['nodes']]
    with ExitStack() as cleanup:
        journal = None
        pickup_store = None
        runtime = None
        recovery_blocked = False
        if os.environ.get('AGV_ORDER_COORDINATED') == '1':
            # A fresh, disposable DB and dedicated robot are only for the
            # launcher-owned isolated broker; never share them with hardware.
            from server.order_sim_map import make_order_sim_map
            from server.sim_handoff_store import SimHandoffStore
            from server.sim_runtime import SimRuntime
            from server.sim_runtime import _DatabaseOwner
            from server.sim_order_authorization import SimOrderAuthorizer
            from server.order_product_control import ProductControlEngine
            from server.order_fleet import FleetProductControlEngine
            from server.order_shelf_inventory import ShelfInventory
            from sim.order_inbox import SimOrderInbox
            from sim.order_executor import SimOrderExecutor
            from sim.mqtt_order_robot import SimMqttOrderRobot
            sim_map = field_map if field_mapping is not None else make_order_sim_map(map_data)
            fleet_enabled = os.environ.get('AGV_ORDER_FLEET') == '1'
            persistent_dir = os.environ.get('AGV_ORDER_PERSIST_DIR')
            if persistent_dir:
                if not fleet_enabled or os.environ.get('AGV_ORDER_SHELF_SYNC') != '1':
                    raise ValueError('persistent demo requires fleet and shelf synchronization')
                from server.order_persistence import PersistentOrderStore
                folder = Path(persistent_dir).resolve()
                if action_source == 'synthetic':
                    # A previous in-memory world cannot be reconstructed from
                    # the DB, even if the prior run appeared to complete.
                    folder.mkdir(parents=False, exist_ok=False)
                else:
                    folder.mkdir(parents=True, exist_ok=True)
                owner = _DatabaseOwner(folder / 'orders.sqlite3')
                owner.acquire()
                cleanup.callback(owner.release)
                fingerprint = (field_mapping.identity if field_mapping is not None else
                               hashlib.sha256(json.dumps(sim_map, sort_keys=True, separators=(',', ':'),
                                                         allow_nan=False).encode('utf-8')).hexdigest())
                journal = PersistentOrderStore(folder / 'orders.sqlite3', fingerprint, products,
                                               action_source=action_source)
                from server.order_pickup_inventory import PickupInventory
                pickup_path = folder / 'pickup-inventory.sqlite3'
                pickup_sources = {item['pickup_node']: item['product_type'] for item in products}
                if journal.pickup_id is not None:
                    pickup_store = PickupInventory.open_existing(pickup_path, pickup_sources,
                                                                 expected_creation_id=journal.pickup_id)
                elif pickup_path.exists() or journal.simulation_id is not None:
                    raise RuntimeError('unbound pickup inventory requires manual recovery')
                else:
                    pickup_store = PickupInventory.create_new(pickup_path, pickup_sources,
                                                              action_source=action_source)
                    journal.bind_pickup_id(pickup_store.creation_id)
                journal.recover_interrupted()
                saved_orders = journal.list_orders()
                recovery_blocked = any(row['state'] == 'unknown' for row in saved_orders)
                saved_pickups = pickup_store.snapshot()['reservations']
                pickup_by_request = {item['request_id']: item for item in saved_pickups}
                order_by_request = {item['request_id']: item for item in saved_orders}
                for row in saved_orders:
                    reservation = pickup_by_request.get(row['request_id'])
                    if row['state'] == 'queued' and reservation and reservation['state'] != 'reserved':
                        recovery_blocked = True
                    if row['state'] == 'completed':
                        result = row.get('result') or {}
                        if (not reservation or reservation['state'] != 'consumed'
                                or reservation['reservation_id'] != result.get('pickup_reservation_id')):
                            recovery_blocked = True
                if any(item['request_id'] not in order_by_request for item in saved_pickups):
                    recovery_blocked = True
                db_path = folder / 'simulation.sqlite3'
            else:
                folder = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix='agv-web-workflow-')))
                db_path = folder / 'server.sqlite3'
            if fleet_enabled:
                starts = ({robot: row['node'] for robot, row in field_mapping.robots.items()}
                          if field_mapping is not None else {'sim-r1': 'J01', 'sim-r2': 'J05'})
                if not set(starts.values()).issubset({node['id'] for node in sim_map['nodes']}):
                    raise ValueError('fleet demo start nodes are absent from the map')
                if journal is not None and journal.simulation_id is not None:
                    store = SimHandoffStore.open_existing(db_path, expected_creation_id=journal.simulation_id,
                                                          expected_idle_enabled=True, simulation_enabled=True)
                elif journal is not None and db_path.exists():
                    raise RuntimeError('unbound simulation database requires manual recovery')
                else:
                    store = SimHandoffStore.create_new(db_path, simulation_enabled=True, idle_positions=starts)
                    if journal is not None:
                        journal.bind_simulation_id(store.creation_id)
            else:
                starts = {'sim-r1': sim_map['nodes'][0]['id']}
                store = SimHandoffStore.create_new(db_path, simulation_enabled=True)
            # Fixed bounded task/session bindings keep the existing event and
            # recovery allowlists immutable during this isolated run.
            prefixes = {'sim-r1': 'web', 'sim-r2': 'r2-web'}
            session_names = {'sim-r1': 'demo-session', 'sim-r2': 'demo-session-r2'}
            sessions = {}
            for robot_id in starts:
                prefix = prefixes[robot_id]
                task_ids = ['{}-pickup'.format(prefix), '{}-delivery'.format(prefix)]
                for index in range(2, 17):
                    task_ids.extend(('{}-{}-pickup'.format(prefix, index), '{}-{}-delivery'.format(prefix, index)))
                sessions[robot_id] = {task: session_names[robot_id] for task in task_ids}
            runtime = SimRuntime(db_path, sim_map, sessions, port=port,
                                 client_id='web-order-workflow', simulation_store=store,
                                 **({'field_simulation': True, 'field_initial_positions': starts}
                                    if field_mapping is not None else {}))
            runtime.start()
            cleanup.callback(runtime.stop)
            map_id = '{}@v{}'.format(sim_map['map_id'], sim_map['version'])
            product_lookup = {item['product_type']: dict(item) for item in products}
            engines = {}
            for robot_id, start in starts.items():
                filename = '{}.sqlite3'.format(robot_id) if fleet_enabled else 'robot.sqlite3'
                inbox = SimOrderInbox(folder / filename, robot_id, map_id)
                inbox.initialize()
                authorizer = SimOrderAuthorizer(runtime, robot_id=robot_id)
                executor_options = ({'experimental_field_map': True,
                                     'field_incoming_edge': field_mapping.robots[robot_id]['incoming_edge']}
                                    if field_mapping is not None else {})
                robot = SimMqttOrderRobot(SimOrderExecutor(inbox, sim_map, start, **executor_options),
                                          port, authorization_guard=authorizer.guard)
                engines[robot_id] = ProductControlEngine(
                    runtime, robot, sim_map, product_lookup, robot_id=robot_id,
                    twin_id={'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}[robot_id],
                    session=session_names[robot_id], task_prefix=prefixes[robot_id],
                    wait_for_reservation=fleet_enabled)
                if journal is not None:
                    completed = [row for row in journal.list_orders() if row['state'] == 'completed'
                                 and isinstance(row['result'], dict)
                                 and row['result'].get('assigned_robot') == robot_id]
                    if completed:
                        checkpoint = completed[-1]['result'].get('checkpoint')
                        try:
                            engines[robot_id].restore_completed_checkpoint(checkpoint)
                        except (ValueError, RuntimeError, KeyError, TypeError):
                            recovery_blocked = True
                            logging.getLogger(__name__).exception('saved simulated stop could not be verified')
                robot.start()
                cleanup.callback(robot.stop)
            inventory = (ShelfInventory({item['shelf_id']: item['product_type'] for item in products}, store=journal)
                         if fleet_enabled and os.environ.get('AGV_ORDER_SHELF_SYNC') == '1' else None)
            engine = FleetProductControlEngine(engines, sim_map, inventory=inventory) if fleet_enabled else engines['sim-r1']
        else:
            from server.order_demo_engine import OrderDemoEngine
            engine = OrderDemoEngine(map_data)
        service = start_mqtt()
        cleanup.callback(stop_mqtt, service)
        demo = DemoRuntime(engine, port, nodes, service,
                           auto_recovery=(os.environ.get('AGV_ORDER_COORDINATED') == '1'
                                          and os.environ.get('AGV_ORDER_FLEET') == '1'),
                           journal=journal, recovery_blocked=recovery_blocked, pickup_store=pickup_store,
                           field_mapping=field_mapping, products=products, action_source=action_source,
                           map_runtime=runtime)
        app.state.mqtt_service = service
        try:
            demo.start()
            cleanup.callback(demo.stop)
            if action_source == 'synthetic':
                from server.synthetic_peer import SyntheticPeer
                if field_mapping is not None:
                    by_id = {node['id']: node['pos'] for node in field_map['nodes']}
                    initial = {}
                    for robot, twin in (('sim-r1', 'AGV_01'), ('sim-r2', 'AGV_02')):
                        x, y = by_id[field_mapping.robots[robot]['node']]
                        # The simulated executor starts at yaw 0. Its explicit
                        # incoming edge is a routing input, not a measured yaw.
                        initial[robot] = dict(agv_id=twin, **field_mapping.pose({'x': x, 'y': y, 'th': 0.}))
                else:
                    by_id = {node['id']: node for node in map_data['nodes']}
                    initial = {robot: {'agv_id': twin, 'x': by_id[node]['x'], 'y': by_id[node]['y'], 'yaw': 0.}
                               for robot, twin, node in (('sim-r1', 'AGV_01', 'J01'), ('sim-r2', 'AGV_02', 'J05'))}
                peer = SyntheticPeer(demo.actions, service, products, initial, secrets.token_hex(16),
                                     field_mapping=field_mapping)
                peer.start()
                cleanup.callback(peer.stop)
            app.state.demo = demo
            app.state.demo_actions = demo.actions
            app.state.field_twin_mapping = field_mapping
            yield
        finally:
            app.state.field_twin_mapping = None
            app.state.demo = None
            app.state.demo_actions = None


app = FastAPI(title='격리 가상 주문 시연', lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)
app.include_router(twin_router)


def _demo(request, *, mutate=False):
    # A fixed host and origin prevent another website from submitting local jobs.
    if (request.client is None or request.client.host not in ('127.0.0.1', '::1')
            or request.headers.getlist('host') != ['127.0.0.1:8000']):
        raise HTTPException(403, '로컬 시연에서만 사용할 수 있습니다')
    if mutate and request.headers.getlist('origin') != ['http://127.0.0.1:8000']:
        raise HTTPException(403, '같은 화면에서 주문해 주세요')
    runtime = getattr(request.app.state, 'demo', None)
    if runtime is None:
        raise HTTPException(503, '시연 서버가 준비되지 않았습니다')
    return runtime


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    pickup_node: StrictStr
    destination_node: StrictStr


class ProductOrderRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    product_id: StrictStr


@app.get('/order-demo/', response_class=HTMLResponse)
def page(request: Request):
    _demo(request)
    from server.order_demo_ui import HTML_PAGE
    return HTMLResponse(HTML_PAGE, headers={'Cache-Control': 'no-store'})


@app.get('/order-demo/api/nodes')
def nodes(request: Request):
    return {'nodes': _demo(request).nodes()}


@app.get('/order-demo/api/status')
def status(request: Request):
    return _demo(request).snapshot()


@app.get('/order-demo/api/map')
def map_observation(request: Request):
    demo = _demo(request)
    try:
        from server.order_demo_map import read_map
        value = read_map(demo.map_runtime)
        return JSONResponse(value, headers={'Cache-Control': 'no-store'})
    except Exception:
        raise HTTPException(503, '가상 지도 및 점유를 확인할 수 없습니다',
                            headers={'Cache-Control': 'no-store'}) from None


@app.get('/order-demo/api/products')
def products(request: Request):
    demo = _demo(request)
    automatic = getattr(getattr(demo, 'engine', None), 'inventory', None) is not None
    journal = getattr(demo, 'journal', None)
    catalog = journal.products.values() if journal is not None else getattr(demo, 'products', PRODUCTS)
    return {'products': [{key: value for key, value in product.items()
                          if not automatic or key != 'slot_index'} for product in catalog]}


def _public_order(row):
    result = row.get('result') or {}
    return dict(request_id=row['request_id'], product_id=row['product_id'], status=row['state'],
                assigned_robot=result.get('assigned_robot'), shelf_id=result.get('shelf_id'),
                slot_index=result.get('slot_index'), last_observation=result.get('last_observation'),
                error=row.get('error'))


@app.get('/order-demo/api/orders')
def order_history(request: Request):
    journal = getattr(_demo(request), 'journal', None)
    if journal is None:
        raise HTTPException(404, '영속 저장이 활성화되지 않았습니다')
    return {'orders': [_public_order(row) for row in journal.list_orders()]}


@app.get('/order-demo/api/metrics')
def metrics(request: Request):
    journal = getattr(_demo(request), 'journal', None)
    if journal is None:
        raise HTTPException(404, '영속 저장이 활성화되지 않았습니다',
                            headers={'Cache-Control': 'no-store'})
    try:
        from server.order_metrics import order_metrics
        return JSONResponse(order_metrics(journal), headers={'Cache-Control': 'no-store'})
    except Exception:
        raise HTTPException(503, '가상 시연 지표를 확인할 수 없습니다',
                            headers={'Cache-Control': 'no-store'}) from None


@app.get('/order-demo/api/orders/{request_id}')
def order_status(request_id: str, request: Request):
    journal = getattr(_demo(request), 'journal', None)
    if journal is None:
        raise HTTPException(404, '영속 저장이 활성화되지 않았습니다')
    try:
        return _public_order(journal.get_order(request_id))
    except (KeyError, ValueError):
        raise HTTPException(404, '주문을 찾을 수 없습니다') from None


@app.get('/order-demo/api/inventory')
def inventory_status(request: Request):
    demo = _demo(request)
    journal = getattr(demo, 'journal', None)
    if journal is None:
        raise HTTPException(404, '영속 저장이 활성화되지 않았습니다')
    result = journal.load_shelves()
    result['fresh'] = bool(demo.actions.available_fast() and demo._world_ready.is_set()
                           and demo.engine.inventory.ready)
    if demo.pickup_store is not None:
        result['pickup'] = demo.pickup_store.snapshot()
    return result


@app.get('/order-demo/api/submissions/{request_id}')
def submission(request_id: str, request: Request):
    demo = _demo(request)
    try:
        return demo.admission.get(request_id)
    except (KeyError, ValueError):
        raise HTTPException(404, '요청을 찾을 수 없습니다') from None


@app.get('/order-demo/api/submission-lookup')
def submission_lookup(request: Request):
    """Read the original attempt only; a missing key does not permit retry."""
    demo = _demo(request)
    values = request.headers.getlist('idempotency-key')
    if len(values) != 1:
        raise HTTPException(422, '요청 키가 필요합니다', headers={'Cache-Control': 'no-store'})
    try:
        result = demo.admission.get_by_key(values[0])
    except (KeyError, ValueError):
        raise HTTPException(404, '요청을 확인할 수 없습니다', headers={'Cache-Control': 'no-store'}) from None
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


def _request_key(request):
    values = request.headers.getlist('idempotency-key')
    if len(values) > 1:
        raise HTTPException(422, '요청 키가 올바르지 않습니다')
    return values[0] if values else None


def _submission_response(result):
    if isinstance(result, dict) and result.get('status') in ('pending', 'unknown') and result.get('request_id'):
        return JSONResponse(result, status_code=202)
    return result


@app.post('/order-demo/api/product-orders')
def product_order(body: ProductOrderRequest, request: Request):
    demo = _demo(request, mutate=True)
    try:
        submit = getattr(demo, 'submit_product_order', None)
        result = (submit(body.product_id, _request_key(request)) if submit is not None
                  else demo.create_product_order(body.product_id))
        return _submission_response(result)
    except AdmissionConflict:
        raise HTTPException(409, '요청 키가 다른 주문에 사용됐습니다') from None
    except (AdmissionBusy, AdmissionStopped):
        raise HTTPException(503, '가상 주문을 접수할 수 없습니다') from None
    except ValueError:
        raise HTTPException(422, '알 수 없는 상품입니다') from None
    except ShelfFull:
        raise HTTPException(409, '선반에 빈 슬롯이 없습니다') from None
    except RuntimeError:
        raise HTTPException(409, '가상 작업을 계속할 수 없습니다') from None
    except ConnectionError:
        raise HTTPException(503, 'Unreal 연결이 준비되지 않았습니다') from None


@app.post('/order-demo/api/orders')
def order(body: OrderRequest, request: Request):
    demo = _demo(request, mutate=True)
    try:
        submit = getattr(demo, 'submit_order', None)
        result = (submit(body.pickup_node, body.destination_node, _request_key(request)) if submit is not None
                  else demo.create_order(body.pickup_node, body.destination_node))
        return _submission_response(result)
    except AdmissionConflict:
        raise HTTPException(409, '요청 키가 다른 주문에 사용됐습니다') from None
    except (AdmissionBusy, AdmissionStopped):
        raise HTTPException(503, '가상 주문을 접수할 수 없습니다') from None
    except ValueError:
        raise HTTPException(422, '선택한 지점으로 경로를 만들 수 없습니다') from None
    except RuntimeError:
        raise HTTPException(409, '현재 가상 주문을 처리 중입니다') from None
    except ConnectionError:
        raise HTTPException(503, '가상 브로커에 연결되지 않았습니다') from None
