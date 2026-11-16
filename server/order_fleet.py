"""Two-robot coordination for the isolated product-order simulation only."""
import copy
import threading

from server.routing import MapGraph


class FleetProductControlEngine:
    """Dispatch to a free robot and serialize correlated Unreal actions.

    Occupancy and authorization remain the responsibility of the shared
    reservation runtime; proximity here is only a dispatch preference.
    """

    def __init__(self, engines, map_data):
        if set(engines) != {'sim-r1', 'sim-r2'}:
            raise ValueError('exactly two explicit isolated robots required')
        self.engines = dict(engines)
        self.graph = MapGraph(map_data)
        self.lock = threading.RLock()
        self._selected_action = None
        self._last_robot = None
        self._used_slots = set()

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
            healthy = all(state.get('status') not in ('unknown', 'failed') for state in states.values())
            result['accepting_orders'] = healthy and any(self._available(state) for state in states.values())
            return result

    def create_order(self, pickup, destination):
        raise RuntimeError('direct node orders are disabled for the fleet')

    def create_product_order(self, product_type, pickup, destination, shelf, slot):
        with self.lock:
            states = self._snapshots()
            if any(state.get('status') in ('unknown', 'failed') for state in states.values()):
                raise RuntimeError('a simulated robot requires recovery')
            if (shelf, slot) in self._used_slots:
                raise RuntimeError('the simulated shelf slot is already in use')
            candidates = []
            for robot_id, state in states.items():
                if not self._available(state):
                    continue
                try:
                    route = self.graph.shortest_path(state['current_node'], pickup)
                    if len(route.nodes) >= 2:
                        candidates.append((route.distance_mm, robot_id))
                except (KeyError, ValueError):
                    continue
            if not candidates:
                raise RuntimeError('no simulated robot is available')
            robot_id = min(candidates)[1]
            result = self.engines[robot_id].create_product_order(product_type, pickup, destination, shelf, slot)
            self._used_slots.add((shelf, slot))
            self._last_robot = robot_id
            return dict(result, assigned_robot=robot_id)

    def tick(self, dt):
        with self.lock:
            for engine in self.engines.values():
                engine.tick(dt)
            return self.snapshot()

    def action_unknown(self):
        with self.lock:
            for engine in self.engines.values():
                engine.action_unknown()
            self._selected_action = None

    def apply_action_result(self, order_id, action_id, agv_id, action, applied):
        with self.lock:
            if self._selected_action is None:
                return False
            engine = self.engines[self._selected_action]
            pending = engine.snapshot().get('pending_action')
            if not pending or pending.get('agv_id') != agv_id:
                return False
            result = engine.apply_action_result(order_id, action_id, agv_id, action, applied)
            if result:
                self._selected_action = None
            return result
