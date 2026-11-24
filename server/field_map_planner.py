"""Experimental direction-aware *calculation*, never an AGV drive permit.

The caller supplies an incoming directed edge on the same map version and is
responsible for proving where and how the robot is actually stopped. Results
are not integrated with scheduling, reservations, MQTT or recovery. Team-map
crossing data is validated fail-closed instead of reproducing NavMap's fallback
on missing pairs.

Spurs (destination edges ``parent -> spur_end``) follow the team robot's
behaviour: it parks at a spur end by pivoting at the parent node, and leaves by
backing out to the parent and pivoting to either direction of the main line.

- A spur-end goal plans to the parent node from any direction and appends the
  destination edge (its ID is the last edge ID).
- A spur-end start uses the destination edge ID as ``incoming_edge`` (the robot
  is parked at its end). The first returned edge is the spur edge traversed
  back to the parent (``<edge>~r`` for a ``parent -> end`` edge), and any main
  arc leaving the parent may follow without a U-turn cost.
- Spurs whose parent is an explicit crossing are rejected: which crossing line
  the robot joins is not defined by the map.
"""
from collections import namedtuple
import heapq
import itertools
import math

from server.routing import MapGraph


class NoFieldRouteError(ValueError):
    """No route satisfying the direction and crossing rules exists."""


FieldRoute = namedtuple('FieldRoute', ['map_ref', 'incoming_edge', 'nodes', 'edge_ids', 'distance_mm'])
_UTURN_MM = 500.0
_REVERSE = '~r'


def _positive(value):
    if type(value) not in (int, float):
        raise ValueError('edge length must be a finite positive number')
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValueError('edge length must be a finite positive number')
    if not math.isfinite(result) or result <= 0:
        raise ValueError('edge length must be a finite positive number')
    return result


def _length(edge):
    # Match field-run NavMap's measured-length precedence rather than the
    # existing server's geometry-only path cost.
    if edge.get('length_mm') is not None:
        return _positive(edge['length_mm'])
    if edge.get('length_m') is not None:
        result = _positive(edge['length_m']) * 1000.0
        return _positive(result)
    shape = edge.get('shape')
    if not isinstance(shape, list) or len(shape) < 2:
        raise ValueError('edge needs measured length or a valid shape')
    coords = []
    for point in shape:
        if (not isinstance(point, list) or len(point) != 2
                or any(type(value) not in (int, float) for value in point)):
            raise ValueError('edge shape must contain 2-D numeric points')
        try:
            coord = tuple(float(value) for value in point)
        except (ValueError, OverflowError):
            raise ValueError('edge coordinates must be finite')
        if not all(math.isfinite(value) for value in coord):
            raise ValueError('edge coordinates must be finite')
        coords.append(coord)
    try:
        return _positive(math.fsum(math.hypot(b[0] - a[0], b[1] - a[1])
                                   for a, b in zip(coords, coords[1:])))
    except OverflowError:
        raise ValueError('edge length must be finite')


def _kind(node):
    kind = node.get('kind')
    if kind:
        return kind
    if node.get('type') == 'destination':
        return 'spur_end'
    if node.get('type') == 'access':
        return 'patch'
    return node.get('type') or 'unknown'


def plan_field_route(data, *, map_ref, incoming_edge, goal):
    """Compute a candidate path from the end of a known directed entry edge.

    Costs use team NavMap's edge-length precedence and 500 mm U-turn cost.
    An edge with ``~r`` means the inverse of a bidirectional main edge. The
    returned edge IDs exclude the already traversed incoming edge; repeated
    nodes are preserved. Neither input state freshness nor physical safety is
    checked. No code here publishes a command or alters a reservation.
    """
    if not isinstance(data, dict):
        raise ValueError('map must be an object')
    map_id, version = data.get('map_id'), data.get('version')
    if (not isinstance(map_id, str) or not map_id.strip() or '@' in map_id
            or type(version) is not int or version < 0 or data.get('unit') != 'mm'):
        raise ValueError('map id, version and mm unit are required')
    expected_ref = '{}@v{}'.format(map_id, version)
    if map_ref != expected_ref:
        raise ValueError('map reference does not match the provided map')
    raw_nodes, raw_edges = data.get('nodes'), data.get('edges')
    if (not isinstance(raw_nodes, list) or not raw_nodes or len(raw_nodes) > 4096
            or not isinstance(raw_edges, list) or len(raw_edges) > 8192):
        raise ValueError('invalid map nodes or edges')
    # Validate the same node coordinates, directed geometry and units used by
    # the server even when measured lengths take precedence for route cost.
    # MapGraph construction only validates; its node-only routing guard stays
    # enabled and is never bypassed by this experimental calculation.
    MapGraph(data)
    nodes = {}
    for node in raw_nodes:
        if not isinstance(node, dict):
            raise ValueError('node must be an object')
        node_id = node.get('id')
        if not isinstance(node_id, str) or not node_id.strip() or node_id in nodes:
            raise ValueError('node ids must be unique strings')
        if 'crossing' in node and type(node['crossing']) is not bool:
            raise ValueError('crossing must be boolean')
        if 'uturn' in node and type(node['uturn']) is not bool:
            raise ValueError('uturn must be boolean')
        nodes[node_id] = node

    # An arc is (from, to, direction, cost, reverse-id). Source edge IDs
    # ending in the reverse marker are forbidden to prevent collisions.
    arcs, outs, inbound, base = {}, {n: [] for n in nodes}, {n: [] for n in nodes}, {}
    spurs, spur_of = {}, {}
    all_ids = set()
    for edge in raw_edges:
        if not isinstance(edge, dict):
            raise ValueError('edge must be an object')
        eid, source, target = edge.get('id'), edge.get('from'), edge.get('to')
        if (not isinstance(eid, str) or not eid.strip() or eid.endswith(_REVERSE)
                or eid in all_ids or source not in nodes or target not in nodes or source == target):
            raise ValueError('edges require unique IDs and known distinct endpoints')
        if type(edge.get('bidirectional')) is not bool:
            raise ValueError('edge bidirectional must be boolean')
        if 'directed' in edge and type(edge['directed']) is not bool:
            raise ValueError('edge directed must be boolean')
        all_ids.add(eid)
        if (edge.get('type') == 'destination' or _kind(nodes[target]) == 'spur_end'
                or _kind(nodes[source]) == 'spur_end'):
            parent, end = (source, target) if _kind(nodes[target]) == 'spur_end' else (target, source)
            if _kind(nodes[end]) != 'spur_end' or _kind(nodes[parent]) == 'spur_end' or end in spur_of:
                raise ValueError('a spur edge must join one main node to one spur end')
            # Edge IDs as traversed: towards the end and backing out to the parent.
            inward, outward = (eid, eid + _REVERSE) if target == end else (eid + _REVERSE, eid)
            spurs[eid] = (parent, end, _length(edge), inward, outward)
            spur_of[end] = eid
            continue
        cost = _length(edge)
        reverse = eid + _REVERSE if edge['bidirectional'] and edge.get('directed') is not True else None
        arcs[eid] = (source, target, 'f', cost, reverse)
        base[eid] = (source, target)
        outs[source].append(eid)
        inbound[target].append(eid)
        if reverse:
            arcs[reverse] = (target, source, 'r', cost, eid)
            outs[target].append(reverse)
            inbound[source].append(reverse)

    start_spur = incoming_edge if isinstance(incoming_edge, str) and incoming_edge in spurs else None
    if start_spur is None and (not isinstance(incoming_edge, str) or incoming_edge not in arcs):
        raise ValueError('incoming edge must be a known directed main edge or a spur edge')
    if not isinstance(goal, str) or goal not in nodes:
        raise ValueError('goal must be a known node')
    goal_spur = spur_of.get(goal) if _kind(nodes[goal]) == 'spur_end' else None
    if _kind(nodes[goal]) == 'spur_end' and goal_spur is None:
        raise ValueError('spur end has no destination edge')
    for spur in (start_spur, goal_spur):
        if spur is not None and nodes[spurs[spur][0]].get('crossing'):
            raise ValueError('spurs at an explicit crossing are unsupported')
    if start_spur is not None and goal == spurs[start_spur][1]:
        # Already parked at the goal: the self-route used to validate candidates.
        return FieldRoute(expected_ref, incoming_edge, (goal,), (), 0.0)
    main_goal = spurs[goal_spur][0] if goal_spur is not None else goal

    # Validate the complete forward crossing table, including its incident
    # edges. The reverse table follows mechanically via reverse arcs.
    crossing = {}
    for node_id, node in nodes.items():
        if not node.get('crossing'):
            if node.get('through'):
                raise ValueError('through rules require an explicit crossing')
            continue
        pairs = node.get('through')
        if not isinstance(pairs, list) or not pairs:
            raise ValueError('crossing requires explicit through rules')
        forwards, exits = {}, set()
        for pair in pairs:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValueError('crossing pairs must contain two edge IDs')
            first, second = pair
            if (not isinstance(first, str) or not isinstance(second, str)
                    or first not in base or second not in base
                    or base[first][1] != node_id or base[second][0] != node_id
                    or first in forwards or second in exits):
                raise ValueError('crossing pairs are missing, duplicate or inconsistent')
            forwards[first] = second
            exits.add(second)
        expected_in = {eid for eid, (_, target) in base.items() if target == node_id}
        expected_out = {eid for eid, (source, _) in base.items() if source == node_id}
        if set(forwards) != expected_in or exits != expected_out:
            raise ValueError('crossing through rules do not cover all incident directions')
        mapping = dict(forwards)
        for first, second in forwards.items():
            incoming_reverse = arcs[second][4]
            outgoing_reverse = arcs[first][4]
            if incoming_reverse is not None and outgoing_reverse is not None:
                mapping[incoming_reverse] = outgoing_reverse
        crossing[node_id] = mapping

    # Search state is a directed incoming edge, not merely a node. This
    # preserves the information needed to validate crossings and U-turns.
    counter = itertools.count()
    if start_spur is None:
        seeds = [(incoming_edge, 0.0)]
    else:
        # Backing out puts the robot on the parent node, where it pivots to
        # either direction: every main arc leaving the parent is a first step.
        parent = spurs[start_spur][0]
        if main_goal == parent:
            seeds = []
        else:
            seeds = [(eid, arcs[eid][3]) for eid in outs[parent]]
            if not seeds:
                raise NoFieldRouteError('spur parent has no main-line arc')
    distance = {eid: cost for eid, cost in seeds}
    previous = {eid: None for eid, _cost in seeds}
    queue = [(cost, next(counter), eid) for eid, cost in seeds]
    heapq.heapify(queue)
    terminal = None
    while queue:
        dist, _, eid = heapq.heappop(queue)
        if dist != distance.get(eid):
            continue
        _, node_id, direction, _, reverse = arcs[eid]
        if node_id == main_goal:
            terminal = eid
            break
        candidates = [out for out in outs[node_id] if arcs[out][2] == direction]
        if node_id in crossing:
            allowed = crossing[node_id].get(eid)
            candidates = [out for out in candidates if out == allowed]
        node = nodes[node_id]
        can_uturn = node.get('uturn')
        if can_uturn is None:
            kind = _kind(node)
            can_uturn = (kind == 'patch' and not node.get('crossing')) or kind == 'spur_end'
        if reverse is not None and can_uturn:
            candidates.append(reverse)
        for candidate in candidates:
            extra = arcs[candidate][3] + (_UTURN_MM if candidate == reverse else 0.0)
            value = dist + extra
            if not math.isfinite(value):
                raise ValueError('route cost must be finite')
            if value < distance.get(candidate, math.inf):
                distance[candidate] = value
                previous[candidate] = eid
                heapq.heappush(queue, (value, next(counter), candidate))
    if start_spur is not None and main_goal == spurs[start_spur][0]:
        total, result_edges = 0.0, []
    else:
        if terminal is None:
            raise NoFieldRouteError('no direction-compatible route exists')
        total = distance[terminal]
        result_edges = []
        while terminal is not None and terminal != incoming_edge:
            result_edges.append(terminal)
            terminal = previous[terminal]
        result_edges.reverse()
    if start_spur is None:
        result_nodes = [arcs[incoming_edge][1]] + [arcs[eid][1] for eid in result_edges]
    else:
        parent, end, length, _inward, outward = spurs[start_spur]
        result_nodes = [end, parent] + [arcs[eid][1] for eid in result_edges]
        result_edges.insert(0, outward)
        total += length
    if goal_spur is not None:
        parent, end, length, inward, _outward = spurs[goal_spur]
        result_edges.append(inward)
        result_nodes.append(end)
        total += length
    return FieldRoute(expected_ref, incoming_edge, tuple(result_nodes), tuple(result_edges),
                      total if result_edges else 0.0)
