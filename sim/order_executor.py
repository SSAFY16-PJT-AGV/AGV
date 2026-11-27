"""Geometry-only simulated order executor; never connects to robot hardware.

The network, recovery confirmation and rear-clear event are managed by the
caller. Reaching a node here is a simulated result, not physical evidence.
"""
import copy
import json
import math
import threading

from server.routing import MapGraph, UnsupportedRobotRoutingError
from server.field_map_planner import plan_field_route, validate_field_route
from sim.order_inbox import OrderRejected, SimOrderInbox, Receipt, _decode


SPEED_MM_PER_SECOND = 1000.0
# Absolute coordinate tolerance for floating-point integration in millimetres.
# It is intentionally unrelated to a physical localization allowance.
_FIELD_POSE_TOLERANCE_MM = 1e-5


def _field_pose_on_segment(x, y, start, finish):
    """Check a simulated position against one current polyline segment."""
    dx, dy = finish[0] - start[0], finish[1] - start[1]
    rx, ry = x - start[0], y - start[1]
    length = math.hypot(dx, dy)
    if not all(math.isfinite(v) for v in (dx, dy, rx, ry, length)):
        return False
    tolerance = _FIELD_POSE_TOLERANCE_MM
    if not length:
        return math.hypot(rx, ry) <= tolerance
    ux, uy = dx / length, dy / length
    along, across = rx * ux + ry * uy, rx * uy - ry * ux
    return (math.isfinite(along) and math.isfinite(across)
            and -tolerance <= along <= length + tolerance and abs(across) <= tolerance)


class SimOrderExecutor:
    """Move one virtual pose only within a durably accepted order release."""

    def __init__(self, inbox, map_data, start_node, *, experimental_field_map=False,
                 field_incoming_edge=None):
        if not isinstance(inbox, SimOrderInbox):
            raise ValueError('a simulated order inbox is required')
        if type(experimental_field_map) is not bool:
            raise ValueError('experimental field-map mode must be boolean')
        data = copy.deepcopy(map_data)
        graph = MapGraph(data)
        if graph.requires_robot_navigation and not experimental_field_map:
            raise UnsupportedRobotRoutingError('robot heading and direction-aware turn constraints are required')
        if not graph.requires_robot_navigation and (experimental_field_map or field_incoming_edge is not None):
            raise ValueError('field-map inputs require a direction-sensitive map')
        if not experimental_field_map and field_incoming_edge is not None:
            raise ValueError('field-map direction requires explicit simulation opt-in')
        if inbox.map_id != '{}@v{}'.format(graph.map_id, graph.version):
            raise ValueError('inbox map and executor map differ')
        positions = {node['id']: tuple(float(v) for v in node['pos']) for node in data['nodes']}
        if not isinstance(start_node, str) or start_node not in positions:
            raise ValueError('start node must be in the map')
        if experimental_field_map:
            # A supplied direction is a simulation input, not physical pose or
            # freshness evidence.  It must end at the selected start node.
            at_start = plan_field_route(data, map_ref=inbox.map_id,
                                        incoming_edge=field_incoming_edge, goal=start_node)
            if at_start.nodes != (start_node,):
                raise ValueError('field incoming edge must end at the simulated start node')
        shapes = {}
        for edge in data['edges']:
            if 'directed' in edge and type(edge['directed']) is not bool:
                raise ValueError('directed flag must be boolean')
            source, destination = edge['from'], edge['to']
            points = tuple(tuple(float(v) for v in point) for point in edge['shape'])
            shapes[(source, destination)] = points
            if edge['bidirectional'] and edge.get('directed') is not True:
                shapes[(destination, source)] = tuple(reversed(points))
        self._inbox, self._shapes, self._positions = inbox, shapes, positions
        self._current_node = start_node
        self._x, self._y = positions[start_node]
        self._yaw = 0.0
        self._status = 'idle'
        self._order = self._receipt = None
        self._release_index = None
        self._visit_index = None
        self._segment_start_index = None
        self._field_map = data if experimental_field_map else None
        self._field_map_canonical = (json.dumps(data, sort_keys=True, ensure_ascii=True,
                                                allow_nan=False, separators=(',', ':'))
                                     if experimental_field_map else None)
        self._field_incoming_edge = field_incoming_edge
        self._field_guard_incoming = field_incoming_edge
        self._field_order_entry = None
        self._field_route_edges = None
        self._reroute_transition = None
        self._reroute_guard_thread = None
        self._recovery_evidence = None
        self._waypoints = ()
        self._waypoint_visits = ()
        self._next_waypoint = 0
        self._lock = threading.RLock()

    def _verify_field_execution(self):
        """Check immutable simulated map, route and visit cursor before motion.

        This only checks in-process data, not real robot position or authority.
        """
        if self._field_map_canonical is None:
            if (self._field_map is not None or self._field_incoming_edge is not None
                    or self._field_guard_incoming is not None or self._field_route_edges is not None):
                raise OrderRejected('field-map simulation guard is inconsistent')
            return
        try:
            if (json.dumps(self._field_map, sort_keys=True, ensure_ascii=True, allow_nan=False,
                           separators=(',', ':')) != self._field_map_canonical):
                raise ValueError('field map changed')
            if self._field_incoming_edge != self._field_guard_incoming:
                raise ValueError('field incoming edge guard changed')
            if any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in (self._x, self._y, self._yaw)):
                raise ValueError('field pose must be finite')
            if self._order is None:
                origin = next((node['pos'] for node in self._field_map['nodes']
                               if node['id'] == self._current_node), None)
                if (self._status != 'idle' or origin is None
                        or not _field_pose_on_segment(self._x, self._y, origin, origin)):
                    raise ValueError('field initial pose differs from start node')
                return
            nodes = self._order['nodes']
            if self._reroute_transition is None:
                route = plan_field_route(self._field_map, map_ref=self._inbox.map_id,
                                         incoming_edge=self._field_order_entry, goal=nodes[-1])
            else:
                if self._receipt is None:
                    raise ValueError('reroute receipt is missing')
                self._inbox._verify_reroute_motion(self._order, self._receipt, self._reroute_transition)
                route = validate_field_route(self._field_map, map_ref=self._inbox.map_id,
                    incoming_edge=self._field_order_entry, nodes=nodes, edge_ids=self._field_route_edges)
            if tuple(nodes) != route.nodes or self._field_route_edges != route.edge_ids:
                raise ValueError('field route changed')
            start, end = self._segment_start_index, self._release_index
            if (type(start) is not int or type(end) is not int or not 0 <= start < end < len(nodes)
                    or nodes[end] != self._order['released_until']
                    or next((i for i in range(start + 1, len(nodes))
                             if nodes[i] == self._order['released_until']), None) != end):
                raise ValueError('field segment cursor changed')
            shapes = {}
            for edge in self._field_map['edges']:
                points = tuple(tuple(float(v) for v in point) for point in edge['shape'])
                shapes[(edge['from'], edge['to'])] = points
                if edge['bidirectional'] and edge.get('directed') is not True:
                    shapes[(edge['to'], edge['from'])] = tuple(reversed(points))
            expected, visits = [], []
            for visit, (source, target) in enumerate(zip(nodes[start:end], nodes[start + 1:end + 1]), start + 1):
                points = shapes[(source, target)]
                for index, (x, y) in enumerate(points[1:], 1):
                    final = index == len(points) - 1
                    expected.append((x, y, target if final else None))
                    visits.append(visit if final else None)
            cursor = self._next_waypoint
            if (self._waypoints != tuple(expected) or self._waypoint_visits != tuple(visits)
                    or type(cursor) is not int or not 0 <= cursor <= len(expected)):
                raise ValueError('field waypoints changed')
            origin = (next(node['pos'] for node in self._field_map['nodes'] if node['id'] == nodes[start])
                      if cursor == 0 else expected[cursor - 1][:2])
            target = expected[cursor][:2] if cursor < len(expected) else expected[-1][:2]
            if not _field_pose_on_segment(self._x, self._y, origin, target):
                raise ValueError('field pose is outside the current segment')
            seen = [index for index in visits[:cursor] if index is not None]
            visit = seen[-1] if seen else start
            if self._visit_index != visit or self._current_node != nodes[visit]:
                raise ValueError('field visit index changed')
            if self._status == 'moving':
                if cursor >= len(expected):
                    raise ValueError('moving segment has no waypoint')
                incoming = self._field_order_entry if start == 0 else route.edge_ids[start - 1]
            elif self._status in ('stopped', 'error'):
                if self._status == 'stopped' and cursor != len(expected):
                    raise ValueError('stopped segment is incomplete')
                incoming = route.edge_ids[end - 1] if self._status == 'stopped' else self._field_incoming_edge
            else:
                raise ValueError('field executor status is inconsistent')
            if self._field_incoming_edge != incoming:
                raise ValueError('field incoming edge changed')
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
            raise OrderRejected('field-map simulation state is inconsistent') from exc

    def accept(self, payload):
        """Validate before persisting; duplicates never re-execute a movement."""
        order, _canonical, digest = _decode(payload, self._inbox.robot_id, self._inbox.map_id)
        with self._lock:
            self._verify_field_execution()
            if (self._reroute_transition is not None and order['seq'] != 1
                    and self._reroute_guard_thread != threading.get_ident()):
                raise OrderRejected('a generation-aware server guard is required')
            if self._status == 'error':
                raise OrderRejected('executor requires explicit reconciliation')
            if (self._receipt is not None and order['order_id'] == self._receipt.order_id
                    and order['seq'] == self._receipt.seq):
                if digest != self._receipt.digest:
                    raise OrderRejected('order ID and sequence reused with different content')
                return self._inbox.accept(payload)
            if self._status == 'moving':
                raise OrderRejected('the current released segment is still moving')

            nodes, stop = order['nodes'], order['released_until']
            if any((source, target) not in self._shapes for source, target in zip(nodes, nodes[1:])):
                raise OrderRejected('route contains an invalid directed edge')
            if order['seq'] == 1:
                if nodes[0] != self._current_node:
                    raise OrderRejected('first order must start at the current simulated node')
                start = 0
                field_edges = None
                if self._field_map is not None:
                    try:
                        route = plan_field_route(self._field_map, map_ref=self._inbox.map_id,
                                                 incoming_edge=self._field_incoming_edge,
                                                 goal=nodes[-1])
                    except ValueError as exc:
                        raise OrderRejected('field-map route cannot be verified') from exc
                    if route.nodes != tuple(nodes):
                        raise OrderRejected('field-map route differs from the direction-aware plan')
                    field_edges = route.edge_ids
            else:
                if (self._receipt is None or self._order is None or self._status != 'stopped'
                        or order['order_id'] != self._receipt.order_id
                        or order['seq'] != self._receipt.seq + 1
                        or self._current_node != self._order['released_until']
                        or nodes != self._order['nodes']
                        or type(self._release_index) is not int
                        or not 0 < self._release_index < len(nodes)
                        or nodes[self._release_index] != self._current_node):
                    raise OrderRejected('next segment does not follow the stopped order')
                start = self._release_index
            end = next((i for i in range(start + 1, len(nodes)) if nodes[i] == stop), None)
            if end is None:
                raise OrderRejected('release must advance beyond the current node')
            waypoints, waypoint_visits = [], []
            for visit, (source, target) in enumerate(zip(nodes[start:end], nodes[start + 1:end + 1]), start + 1):
                points = self._shapes[(source, target)]
                for index, (x, y) in enumerate(points[1:], 1):
                    waypoints.append((x, y, target if index == len(points) - 1 else None))
                    waypoint_visits.append(visit if index == len(points) - 1 else None)
            if not waypoints:
                raise OrderRejected('released segment contains no geometry')

            # SimOrderInbox checks duplicate content, order continuity, map and
            # persistence before this instance ever starts moving the pose.
            receipt = self._inbox.accept(payload)
            if not receipt.new:
                return receipt
            self._order = copy.deepcopy(order)
            self._receipt = receipt
            self._release_index = end
            self._visit_index = start
            self._segment_start_index = start
            if order['seq'] == 1:
                self._field_route_edges = field_edges
                self._field_order_entry = self._field_incoming_edge
                self._reroute_transition = None
            self._recovery_evidence = None
            self._waypoints = tuple(waypoints)
            self._waypoint_visits = tuple(waypoint_visits)
            self._next_waypoint = 0
            self._status = 'moving'
            return receipt

    def _begin_reroute(self, payload, *, transition_id, incoming_edge, edge_ids):
        """Start one virtual generation while caller holds the server guard."""
        order, _canonical, _digest = _decode(payload, self._inbox.robot_id, self._inbox.map_id)
        with self._lock:
            self._verify_field_execution()
            if self._reroute_guard_thread != threading.get_ident():
                raise OrderRejected('a generation-aware server guard is required')
            if (self._field_map is None or self._reroute_transition is not None
                    or self._status != 'stopped' or self._receipt is None or self._order is None
                    or self._receipt.state != 'completed' or self._current_node != self._order['released_until']
                    or order['order_id'] != self._receipt.order_id or order['seq'] != self._receipt.seq + 1
                    or not order['nodes'] or order['nodes'][0] != self._current_node
                    or order['nodes'][-1] != self._order['nodes'][-1]
                    or json.dumps(order['action_at_end'], sort_keys=True, ensure_ascii=True, separators=(',', ':'))
                       != json.dumps(self._order['action_at_end'], sort_keys=True, ensure_ascii=True, separators=(',', ':'))
                    or incoming_edge != self._field_incoming_edge):
                raise OrderRejected('reroute does not follow the stopped parent')
            route = validate_field_route(self._field_map, map_ref=self._inbox.map_id,
                incoming_edge=incoming_edge, nodes=order['nodes'], edge_ids=edge_ids)
            end = next((index for index in range(1, len(order['nodes']))
                        if order['nodes'][index] == order['released_until']), None)
            if end is None:
                raise OrderRejected('reroute release must advance from the stopped node')
            waypoints, visits = [], []
            for visit, (source, target) in enumerate(zip(order['nodes'][:end], order['nodes'][1:end + 1]), 1):
                points = self._shapes[(source, target)]
                for index, (x, y) in enumerate(points[1:], 1):
                    final = index == len(points) - 1
                    waypoints.append((x, y, target if final else None))
                    visits.append(visit if final else None)
            if not waypoints:
                raise OrderRejected('reroute segment contains no geometry')
            receipt = self._inbox._accept_reroute(payload, transition_id)
            if not receipt.new:
                return receipt
            self._order, self._receipt = copy.deepcopy(order), receipt
            self._release_index, self._visit_index, self._segment_start_index = end, 0, 0
            self._field_order_entry, self._field_route_edges = incoming_edge, route.edge_ids
            self._field_incoming_edge = self._field_guard_incoming = incoming_edge
            self._reroute_transition = transition_id
            self._recovery_evidence = None
            self._waypoints, self._waypoint_visits, self._next_waypoint = tuple(waypoints), tuple(visits), 0
            self._status = 'moving'
            try:
                self._verify_field_execution()
            except BaseException:
                self._status = 'error'
                raise
            return receipt

    def tick(self, dt_seconds):
        try:
            valid = (type(dt_seconds) in (int, float) and math.isfinite(dt_seconds)
                     and dt_seconds >= 0)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError('elapsed time must be finite and nonnegative')
        remaining = dt_seconds * SPEED_MM_PER_SECOND
        if not math.isfinite(remaining):
            raise ValueError('elapsed time is too large')
        with self._lock:
            self._verify_field_execution()
            if (self._reroute_transition is not None and self._status == 'moving'
                    and self._reroute_guard_thread != threading.get_ident()):
                raise OrderRejected('a generation-aware server guard is required')
            while self._status == 'moving' and remaining > 0:
                x, y, node = self._waypoints[self._next_waypoint]
                dx, dy = x - self._x, y - self._y
                distance = math.hypot(dx, dy)
                if distance:
                    self._yaw = math.degrees(math.atan2(dy, dx))
                if remaining + 1e-9 < distance:
                    self._x += dx * remaining / distance
                    self._y += dy * remaining / distance
                    break
                self._x, self._y = x, y
                remaining = max(0.0, remaining - distance)
                self._next_waypoint += 1
                if node is not None:
                    self._current_node = node
                    self._visit_index = self._waypoint_visits[self._next_waypoint - 1]
                if self._next_waypoint == len(self._waypoints):
                    try:
                        self._inbox.mark_completed(self._receipt.order_id, self._receipt.seq,
                                                   self._receipt.digest, self._current_node,
                                                   rear_clear_confirmed=True)
                    except Exception:
                        self._status = 'error'
                        raise
                    self._receipt = self._receipt._replace(state='completed')
                    if self._field_map is not None:
                        self._field_incoming_edge = self._field_route_edges[self._release_index - 1]
                        self._field_guard_incoming = self._field_incoming_edge
                    self._status = 'stopped'
            return self.snapshot()

    def field_stop_direction(self):
        """Return checked synthetic direction only while the simulator is stopped."""
        with self._lock:
            if self._field_map is None or self._status not in ('idle', 'stopped'):
                raise OrderRejected('field direction requires a stopped field simulation')
            self._verify_field_execution()
            return self._current_node, self._field_incoming_edge

    def snapshot(self):
        with self._lock:
            result = {'status': self._status, 'current_node': self._current_node,
                    'pose': {'x': self._x, 'y': self._y, 'th': self._yaw},
                    'order': copy.deepcopy(self._order),
                    'receipt': self._receipt._asdict() if self._receipt else None}
            if self._order is not None:
                result['visit_index'] = self._visit_index
                result['released_index'] = self._release_index
            if self._recovery_evidence is not None:
                result['recovered'] = True
            if self._reroute_transition is not None:
                result['reroute_transition_id'] = self._reroute_transition
            return result

    def _restore_completed(self, order, digest, evidence, *, field_entry=None, field_history=None):
        """Install already validated historical stop; never execute an order."""
        with self._lock:
            if self._status != 'idle' or self._order is not None or self._receipt is not None:
                raise OrderRejected('executor is not fresh and idle')
            self._verify_field_execution()
            field_route = field_segment_start = None
            if self._field_map is not None:
                from server.route_occurrence import visit_indices_from_orders, plan_occurrence_segment
                try:
                    indices = visit_indices_from_orders(field_history)
                    if field_history[-1] != order or indices[-1] != len(order['nodes']) - 1:
                        raise ValueError('field completed order history does not reach the final occurrence')
                    field_route = plan_field_route(self._field_map, map_ref=self._inbox.map_id,
                                                   incoming_edge=field_entry, goal=order['nodes'][-1])
                    if field_route.nodes != tuple(order['nodes']) or not field_route.edge_ids:
                        raise ValueError('field completed path differs from verified route')
                    start = 0
                    for index in indices:
                        if plan_occurrence_segment(self._field_map, order['nodes'], start).end_index != index:
                            raise ValueError('field completed history skipped a stop occurrence')
                        start = index
                    field_segment_start = 0 if len(indices) == 1 else indices[-2]
                except (ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
                    raise OrderRejected('field completed route proof is invalid') from exc
            elif field_entry is not None or field_history is not None:
                raise OrderRejected('field proof cannot restore a generic simulation')
            node = order['released_until']
            if node != order['nodes'][-1]:
                raise OrderRejected('only a finished route can be restored')
            if node not in self._positions:
                raise OrderRejected('finished node geometry is unavailable')
            # The validated final order and map preserve the exact oriented
            # polyline used by tick(). Its last nonzero segment determines the
            # last heading even when the shape ends with duplicate points.
            nodes = order.get('nodes')
            if not isinstance(nodes, list) or len(nodes) < 2:
                raise OrderRejected('finished route geometry is unavailable')
            if self._field_map is None and len(set(nodes)) != len(nodes):
                raise OrderRejected('repeated route restore requires visit-index proof')
            points = self._shapes.get((nodes[-2], nodes[-1]))
            if not points or len(points) < 2:
                raise OrderRejected('finished edge geometry is unavailable')
            yaw = None
            for start, end in reversed(tuple(zip(points, points[1:]))):
                dx, dy = end[0] - start[0], end[1] - start[1]
                if not math.isfinite(dx) or not math.isfinite(dy):
                    raise OrderRejected('finished direction is not finite')
                if dx or dy:
                    yaw = math.degrees(math.atan2(dy, dx))
                    break
            if yaw is None or not math.isfinite(yaw):
                raise OrderRejected('finished direction is unavailable')
            self._current_node = node
            self._x, self._y = self._positions[node]
            self._yaw = yaw
            self._order = copy.deepcopy(order)
            self._receipt = Receipt(False, order['order_id'], order['seq'], digest, 'completed')
            self._release_index = len(nodes) - 1
            self._visit_index = self._release_index
            self._segment_start_index = field_segment_start
            if field_route is not None:
                waypoints, visits = [], []
                for visit, (source, target) in enumerate(zip(nodes[field_segment_start:-1],
                                                               nodes[field_segment_start + 1:]),
                                                        field_segment_start + 1):
                    for index, (x, y) in enumerate(self._shapes[(source, target)][1:], 1):
                        final = index == len(self._shapes[(source, target)]) - 1
                        waypoints.append((x, y, target if final else None))
                        visits.append(visit if final else None)
                self._waypoints, self._waypoint_visits = tuple(waypoints), tuple(visits)
                self._next_waypoint = len(waypoints)
                self._field_order_entry = field_entry
                self._field_route_edges = field_route.edge_ids
                self._field_incoming_edge = self._field_guard_incoming = field_route.edge_ids[-1]
            self._recovery_evidence = copy.deepcopy(evidence)
            self._status = 'stopped'
            self._verify_field_execution()

    def matches_recovered(self, evidence):
        """Accept only this executor's validated, still-current historical stop."""
        with self._lock:
            return (self._status == 'stopped' and self._recovery_evidence is not None
                    and evidence == self._recovery_evidence)

    def matches_recovered_stop(self, task, node, receipt):
        with self._lock:
            evidence = self._recovery_evidence
            return (self._status == 'stopped' and evidence is not None
                    and evidence['task_id'] == task and evidence['stopped_at'] == node
                    and self._receipt is not None and receipt == self._receipt._asdict()
                    and receipt['new'] is False and receipt['state'] == 'completed'
                    and receipt['seq'] == evidence['seq'] and receipt['digest'] == evidence['digest'])
