"""Experimental resource calculations indexed by route occurrence.

This module does not validate a robot's heading or turn rules, grant a drive
permit, verify physical rear clearance, persist a visit index, or publish an
order. The existing simulation and recovery workflow does not call it. A
caller must retain occupancy when a calculation or external proof fails.
"""
from collections import namedtuple
import copy
import json

from server.routing import MapGraph
from server.field_robot_contract import validate_field_robot_order


OccurrenceSegment = namedtuple(
    'OccurrenceSegment', ['start_index', 'end_index', 'nodes', 'released_until', 'resources']
)


def _text_id(value):
    return isinstance(value, str) and bool(value.strip())


def _validated(data, nodes, start_index):
    data = copy.deepcopy(data)
    MapGraph(data)  # validates geometry, not robot motion or pose
    if (not isinstance(nodes, (list, tuple)) or len(nodes) < 2
            or any(not _text_id(node) for node in nodes)):
        raise ValueError('route must contain at least two node IDs')
    nodes = tuple(nodes)
    if type(start_index) is not int or not 0 <= start_index < len(nodes) - 1:
        raise ValueError('start index must identify a nonfinal route occurrence')
    node_ids = {node['id'] for node in data['nodes']}
    if any(node not in node_ids for node in nodes):
        raise ValueError('route references an unknown node')
    directed = set()
    for edge in data['edges']:
        if type(edge.get('lanes')) is not int or edge['lanes'] <= 0:
            raise ValueError('every edge must specify a positive lane count')
        if 'directed' in edge and type(edge['directed']) is not bool:
            raise ValueError('edge directed must be boolean')
        a, b = edge['from'], edge['to']
        directed.add((a, b))
        if edge['bidirectional'] and edge.get('directed') is not True:
            directed.add((b, a))
    if any((a, b) not in directed for a, b in zip(nodes, nodes[1:])):
        raise ValueError('route must follow direct edges in their allowed direction')

    zones = data.get('zones')
    if not isinstance(zones, list):
        raise ValueError('map zones must be explicitly specified')
    waiting, shared, zone_ids = set(), [], set()
    for zone in zones:
        if not isinstance(zone, dict) or not _text_id(zone.get('id')):
            raise ValueError('zone needs a non-empty id')
        zone_id = zone['id']
        if zone_id in zone_ids:
            raise ValueError('zone IDs must be unique')
        zone_ids.add(zone_id)
        if zone.get('type') == 'waiting':
            node = zone.get('node')
            if not _text_id(node) or node not in node_ids:
                raise ValueError('waiting zone must specify a known node')
            if 'nodes' in zone:
                aliases = zone['nodes']
                if not isinstance(aliases, list) or aliases not in ([], [node]):
                    raise ValueError('waiting zone node and nodes must agree')
            waiting.add(node)
        elif zone.get('type') == 'shared':
            members = zone.get('nodes')
            if (not isinstance(members, list) or not members
                    or any(not _text_id(node) or node not in node_ids for node in members)
                    or len(set(members)) != len(members)):
                raise ValueError('shared zone must specify unique known nodes')
            shared.append((zone_id, set(members)))
        else:
            raise ValueError('unrecognized zone type')
    if any(waiting.intersection(members) for _, members in shared):
        raise ValueError('waiting nodes must not belong to a shared zone')
    if start_index and nodes[start_index] not in waiting:
        raise ValueError('a later segment must start at a waiting occurrence')
    return nodes, waiting, shared


def _segment(nodes, waiting, shared, start):
    end = next((i for i in range(start + 1, len(nodes)) if nodes[i] in waiting), None)
    if end is None:
        raise ValueError('no waiting node exists on the remaining route')
    path = nodes[start:end + 1]
    resources = {('node', node) for node in path}
    resources.update(('edge', json.dumps(sorted((a, b)), ensure_ascii=True, separators=(',', ':')))
                     for a, b in zip(path, path[1:]))
    resources.update(('zone', zone_id) for zone_id, members in shared if members.intersection(path))
    return OccurrenceSegment(start, end, path, nodes[end], tuple(sorted(resources)))


def plan_occurrence_segment(map_data, nodes, start_index):
    """Calculate the next segment, distinguishing each occurrence of a node."""
    route, waiting, shared = _validated(map_data, nodes, start_index)
    return _segment(route, waiting, shared, start_index)


def occurrence_clearance(map_data, nodes, start_index):
    """Return candidates to release only after independently proved clearance.

    The current and next segment are computed from the same validated map
    snapshot. If the rest of the route cannot reach a waiting point, fail
    instead of dropping current occupancy.
    """
    route, waiting, shared = _validated(map_data, nodes, start_index)
    current = _segment(route, waiting, shared, start_index)
    if current.end_index == len(route) - 1:
        keep = {('node', current.released_until)}
    else:
        keep = set(_segment(route, waiting, shared, current.end_index).resources)
    return tuple(sorted(set(current.resources) - keep))


def visit_indices_from_orders(orders):
    """Reconstruct releases from a complete consecutive team-order history.

    This is a pure consistency check, not proof that orders were received or
    that the robot moved or stopped. The caller must separately establish that
    the supplied history is authentic, complete and current before using it.
    """
    if not isinstance(orders, (list, tuple)) or not orders:
        raise ValueError('a complete nonempty order history is required')
    reference = None
    last = 0
    result = []
    for sequence, order in enumerate(orders, start=1):
        validate_field_robot_order(order)
        if order['seq'] != sequence:
            raise ValueError('order sequences must start at 1 and be consecutive')
        # JSON booleans and numbers compare equal in Python (True == 1), but
        # they are different wire values and must not hide a changed action.
        action = json.dumps(order['action_at_end'], sort_keys=True, ensure_ascii=True,
                            allow_nan=False, separators=(',', ':'))
        identity = (order['order_id'], order['map_id'], order['nodes'], action)
        if reference is None:
            reference = copy.deepcopy(identity)
        elif identity != reference:
            raise ValueError('order identity, map, route or action changed')
        index = next((i for i in range(last + 1, len(order['nodes']))
                      if order['nodes'][i] == order['released_until']), None)
        if index is None:
            raise ValueError('released node has no later occurrence')
        result.append(index)
        last = index
    return tuple(result)
