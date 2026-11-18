"""Two-robot coordination for the isolated product-order simulation only."""
import copy
import threading

from server.routing import MapGraph


class FleetProductControlEngine:
    """Dispatch to a free robot and serialize correlated Unreal actions.

    Occupancy and authorization remain the responsibility of the shared
    reservation runtime; proximity here is only a dispatch preference.
    """

    def __init__(self, engines, map_data, *, inventory=None):
        if set(engines) != {'sim-r1', 'sim-r2'}:
            raise ValueError('exactly two explicit isolated robots required')
        self.engines = dict(engines)
        self.graph = MapGraph(map_data)
        self.lock = threading.RLock()
        self._selected_action = None
        self._last_robot = None
        self._used_slots = set()
        self.inventory = inventory
        self._slot_reservations = {}

    @property
    def control_ready(self):
        return all(engine.control_ready is True for engine in self.engines.values())

    def _snapshots(self):
        return {robot_id: engine.snapshot() for robot_id, engine in self.engines.items()}

    @staticmethod
    def _available(state):
        return state.get('status') in ('idle', 'completed') and state.get('orders_remaining', 0) > 0

    def snapshot(self):
        with self.lock:
            states = self._snapshots()
            if self._selected_action is not None:
                current = states[self._selected_action].get('pending_action')
                if not current:
                    self._selected_action = None
            if self._selected_action is None:
                self._selected_action = next((key for key in sorted(states)
                                              if states[key].get('pending_action')), None)
            selected = self._selected_action or self._last_robot or 'sim-r1'
            result = copy.deepcopy(states[selected])
            result['mode'] = 'fleet'
            result['pending_action'] = copy.deepcopy(states[selected].get('pending_action')) if self._selected_action else None
            result['robots'] = [dict(copy.deepcopy(states[key]), robot_id=key)
                                for key in sorted(states)]
            result['shelf_inventory_ready'] = self.inventory is None or self.inventory.ready
            healthy = all(state.get('status') not in ('unknown', 'failed') for state in states.values())
            result['accepting_orders'] = (healthy and (self.inventory is None or self.inventory.ready)
                                          and any(self._available(state) for state in states.values()))
            return result

    def create_order(self, pickup, destination):
        raise RuntimeError('direct node orders are disabled for the fleet')

    def can_dispatch_destination(self, destination):
        """Serialize one destination so a stopped robot can leave it first."""
        with self.lock:
            return not any(state.get('destination_node') == destination
                           and state.get('status') in ('moving', 'waiting', 'suspended')
                           for state in self._snapshots().values())

    def create_product_order(self, product_type, pickup, destination, shelf, slot, *, request_id=None):
        with self.lock:
            states = self._snapshots()
            if any(state.get('status') in ('unknown', 'failed') for state in states.values()):
                raise RuntimeError('a simulated robot requires recovery')
            if self.inventory is None and (shelf, slot) in self._used_slots:
                raise RuntimeError('the simulated shelf slot is already in use')
            if self.inventory is not None and not self.can_dispatch_destination(destination):
                raise RuntimeError('the simulated destination is in use')
            candidates = []
            for robot_id, state in states.items():
                if not self._available(state):
                    continue
                try:
                    engine = self.engines[robot_id]
                    if getattr(getattr(engine, 'runtime', None), '_field_simulation', False) is True:
                        route = engine._field_route_to(pickup)
                        if route.nodes[0] != state['current_node']:
                            raise ValueError('field route does not match observed simulated node')
                    else:
                        route = self.graph.shortest_path(state['current_node'], pickup)
                    if len(route.nodes) >= 2:
                        # If a completed robot holds the destination node, it
                        # must depart for the pickup before another can enter.
                        departing_owner = self.inventory is not None and state.get('current_node') == destination
                        candidates.append((0 if departing_owner else 1, route.distance_mm, robot_id))
                except (KeyError, ValueError):
                    continue
            if not candidates:
                raise RuntimeError('no simulated robot is available')
            robot_id = min(candidates)[2]
            reservation = None
            if self.inventory is not None:
                reservation = self.inventory.reserve(shelf, product_type, request_id=request_id)
                slot = reservation['slot_index']
                self._slot_reservations[robot_id] = reservation
            result = self.engines[robot_id].create_product_order(product_type, pickup, destination, shelf, slot)
            if self.inventory is None:
                self._used_slots.add((shelf, slot))
            self._last_robot = robot_id
            result = dict(result, assigned_robot=robot_id, slot_index=slot)
            if reservation is not None:
                result.update(shelf_id=shelf, reservation_id=reservation['reservation_id'])
            return result

    def tick(self, dt):
        with self.lock:
            for engine in self.engines.values():
                # A disconnect can arrive while an earlier robot is being
                # advanced. Let the demo freeze and reconcile the whole fleet
                # before touching another engine with unavailable transport.
                if not self.control_ready:
                    break
                tick = getattr(engine, 'tick_recoverable', engine.tick)
                tick(dt)
            return self.snapshot()

    def action_unknown(self):
        with self.lock:
            for engine in self.engines.values():
                engine.action_unknown()
            self._selected_action = None

    def suspend(self, uncertain_action_id=None):
        with self.lock:
            return all([engine.suspend(uncertain_action_id) for engine in self.engines.values()])

    def resume_after_reconnect(self):
        with self.lock:
            resumed = []
            for engine in self.engines.values():
                if engine.snapshot()['status'] == 'suspended' and not engine.resume_after_reconnect():
                    for previous in resumed:
                        previous.suspend()
                    return False
                if engine.snapshot()['status'] in ('moving', 'waiting'):
                    resumed.append(engine)
            return all(engine.snapshot()['status'] not in ('unknown', 'failed')
                       for engine in self.engines.values())

    def apply_action_result(self, order_id, action_id, agv_id, action, applied):
        with self.lock:
            if self._selected_action is None:
                return False
            engine = self.engines[self._selected_action]
            pending = engine.snapshot().get('pending_action')
            if not pending or pending.get('agv_id') != agv_id:
                return False
            if (self.inventory is not None and action == 'place' and applied is True
                    and all(pending.get(key) == value for key, value in (
                        ('order_id', order_id), ('action_id', action_id), ('action', action)))):
                reservation = self._slot_reservations.get(self._selected_action)
                if (not reservation or pending.get('shelf_id') != reservation['shelf_id']
                        or pending.get('slot_index') != reservation['slot_index']):
                    return False
                try:
                    self.inventory.confirm(reservation['reservation_id'])
                except ValueError:
                    return False
            result = engine.apply_action_result(order_id, action_id, agv_id, action, applied)
            if result:
                self._selected_action = None
            return result
