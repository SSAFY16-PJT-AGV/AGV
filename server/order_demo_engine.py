"""Single synthetic robot for local order-to-twin demonstrations only.

This is a geometric simulation, not a robot controller or a reservation system.
The supplied Unreal metre map is converted locally to an undirected mm graph;
the conversion does not establish the team's production map semantics.
Callers must serialize access to each engine instance.
"""
import math
import uuid

from server.routing import MapGraph


SPEED_MM_PER_SECOND = 1000.0


def _coordinate(value):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError('map coordinates must be finite numbers')
    result = value * 1000.0
    if not math.isfinite(result):
        raise ValueError('map coordinate is too large')
    return result


def _point(item):
    if not isinstance(item, dict):
        raise ValueError('map point must be an object')
    return (_coordinate(item.get('x')), _coordinate(item.get('y')))


def _convert_map(data):
    if not isinstance(data, dict) or data.get('coordinate_unit') != 'meter':
        raise ValueError('a metre-based Unreal map is required')
    nodes = data.get('nodes')
    edges = data.get('edges')
    if not isinstance(nodes, list) or not nodes or not isinstance(edges, list):
        raise ValueError('nodes and edges must be lists')
    positions, node_rows = {}, []
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError('node must be an object')
        name = node.get('id')
        if not isinstance(name, str) or not name.strip() or name in positions:
            raise ValueError('nodes require distinct nonempty ids')
        positions[name] = _point(node)
        node_rows.append({'id': name, 'pos': list(positions[name])})
    edge_rows, shapes = [], {}
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError('edge must be an object')
        source, target = edge.get('from'), edge.get('to')
        if (not isinstance(source, str) or not isinstance(target, str)
                or source not in positions or target not in positions or source == target):
            raise ValueError('edge must connect two existing distinct nodes')
        points = edge.get('points')
        if not isinstance(points, list):
            raise ValueError('edge points must be a list')
        raw_shape = [positions[source]] + [_point(p) for p in points] + [positions[target]]
        shape = [raw_shape[0]]
        for point in raw_shape[1:]:
            if point != shape[-1]:
                shape.append(point)
        if len(shape) < 2:
            raise ValueError('edge must have positive length')
        edge_rows.append({'from': source, 'to': target, 'shape': [list(p) for p in shape],
                          'bidirectional': True})
        shapes[(source, target)] = shape
        shapes[(target, source)] = list(reversed(shape))
    graph = MapGraph({'map_id': data.get('map_id'), 'version': 1, 'unit': 'mm',
                      'nodes': node_rows, 'edges': edge_rows})
    return graph, positions, shapes, node_rows[0]['id']


class OrderDemoEngine:
    """Drive one simulated pose along shortest-path map polylines."""

    def __init__(self, map_data):
        self._graph, self._positions, self._shapes, first = _convert_map(map_data)
        self._current_node = first
        self._x, self._y = self._positions[first]
        self._yaw = 0.0
        self._status, self._phase = 'idle', None
        self._order_id = self._pickup = self._destination = None
        self._route, self._waypoints = [], []
        self._next_waypoint = 0
        self._product_type = self._shelf_id = self._slot_index = None
        self._pending_action = None

    def create_order(self, pickup_node, destination_node):
        if self._status in ('moving', 'waiting', 'failed', 'unknown'):
            raise RuntimeError('the simulated robot is busy')
        first = self._graph.shortest_path(self._current_node, pickup_node).nodes
        second = self._graph.shortest_path(pickup_node, destination_node).nodes
        route = list(first) + list(second[1:])
        waypoints = []
        pickup_index = len(first) - 1
        for route_index, (source, target) in enumerate(zip(route, route[1:]), start=1):
            shape = self._shapes[(source, target)]
            for i, (x, y) in enumerate(shape[1:], start=1):
                is_last = i == len(shape) - 1
                waypoints.append((x, y, target if is_last else None,
                                  is_last and route_index == pickup_index))
        self._order_id = uuid.uuid4().hex
        self._pickup, self._destination = pickup_node, destination_node
        self._product_type = self._shelf_id = self._slot_index = None
        self._pending_action = None
        self._route, self._waypoints, self._next_waypoint = route, waypoints, 0
        self._status = 'moving' if waypoints else 'completed'
        self._phase = ('done' if not waypoints else
                       'to_destination' if pickup_index == 0 else 'to_pickup')
        return self.snapshot()

    def create_product_order(self, product_type, pickup_node, destination_node, shelf_id, slot_index):
        if (product_type not in ('Red', 'Green', 'Blue')
                or not isinstance(shelf_id, str) or not shelf_id.strip()
                or type(slot_index) is not int or not 0 <= slot_index <= 2147483647):
            raise ValueError('invalid simulated product action')
        self.create_order(pickup_node, destination_node)
        self._product_type = product_type
        self._shelf_id = shelf_id
        self._slot_index = slot_index
        if self._current_node == pickup_node:
            self._begin_action('grab')
        else:
            self._phase = 'to_pickup'
        return self.snapshot()

    def _begin_action(self, action):
        event = {'order_id': self._order_id, 'action_id': uuid.uuid4().hex,
                 'agv_id': 'AGV_01', 'action': action}
        if action == 'grab':
            event['product_type'] = self._product_type
        else:
            event.update(shelf_id=self._shelf_id, slot_index=self._slot_index)
        self._pending_action = event
        self._status = 'waiting'
        self._phase = 'waiting_' + action

    def apply_action_result(self, order_id, action_id, agv_id, action, applied):
        pending = self._pending_action
        if (self._status != 'waiting' or pending is None or type(applied) is not bool
                or order_id != pending['order_id'] or action_id != pending['action_id']
                or agv_id != pending['agv_id'] or action != pending['action']):
            return False
        self._pending_action = None
        if not applied:
            self._status = 'failed'
        elif action == 'place':
            self._status, self._phase = 'completed', 'done'
        elif self._next_waypoint == len(self._waypoints):
            self._begin_action('place')
        else:
            self._status, self._phase = 'moving', 'to_destination'
        return True

    def action_unknown(self):
        if self._product_type is None or self._status not in ('waiting', 'moving'):
            return False
        self._status = 'unknown'
        self._pending_action = None
        return True

    def tick(self, dt_seconds):
        try:
            valid = (type(dt_seconds) in (int, float) and math.isfinite(dt_seconds)
                     and dt_seconds >= 0)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError('elapsed time must be a finite nonnegative number')
        remaining = dt_seconds * SPEED_MM_PER_SECOND
        if not math.isfinite(remaining):
            raise ValueError('elapsed time is too large')
        while self._status == 'moving' and remaining > 0:
            x, y, node, is_pickup = self._waypoints[self._next_waypoint]
            dx, dy = x - self._x, y - self._y
            distance = math.hypot(dx, dy)
            if distance:
                self._yaw = math.degrees(math.atan2(dy, dx))
            if remaining + 1e-9 < distance:
                self._x += dx * remaining / distance
                self._y += dy * remaining / distance
                remaining = 0
                break
            self._x, self._y = x, y
            remaining = max(0.0, remaining - distance)
            self._next_waypoint += 1
            if node is not None:
                self._current_node = node
                if is_pickup:
                    if self._product_type is not None:
                        self._begin_action('grab')
                        break
                    self._phase = 'to_destination'
            if self._next_waypoint == len(self._waypoints):
                if self._product_type is not None:
                    self._begin_action('place')
                else:
                    self._status, self._phase = 'completed', 'done'
        return self.snapshot()

    def snapshot(self):
        return {'status': self._status, 'order_id': self._order_id,
                'pickup_node': self._pickup, 'destination_node': self._destination,
                'phase': self._phase, 'route': list(self._route),
                'current_node': self._current_node,
                'pose': {'x': self._x, 'y': self._y, 'th': self._yaw},
                'battery': 0.8, 'product_type': self._product_type,
                'pending_action': dict(self._pending_action) if self._pending_action else None}
