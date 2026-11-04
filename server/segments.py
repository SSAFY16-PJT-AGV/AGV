"""Conservative resource planning for one path segment, not a drive permit.

Requires externally validated zones and physical clearance.  All edges are
treated as exclusive, and only an explicitly marked waiting node may be the
end of the released segment.  MapGraph imposes zero geometry tolerance.
"""
from collections import namedtuple
import copy
import json

from server.routing import MapGraph


Segment = namedtuple('Segment', ['nodes', 'released_until', 'resources'])


def _text_id(value):
    return isinstance(value, str) and bool(value.strip())


def _edge_id(start, end):
    return json.dumps(sorted((start, end)), ensure_ascii=True, separators=(',', ':'))


def plan_segment(map_data, nodes):
    """Validate a directed path and return resources through its next waiting node."""
    data = copy.deepcopy(map_data)
    MapGraph(data)
    if (not isinstance(nodes, (list, tuple)) or len(nodes) < 2
            or any(not _text_id(node) for node in nodes)
            or len(set(nodes)) != len(nodes)):
        raise ValueError('path must contain at least two distinct node IDs')
    nodes = tuple(nodes)
    node_ids = {node['id'] for node in data['nodes']}
    if any(node not in node_ids for node in nodes):
        raise ValueError('path references an unknown node')

    directed_edges = set()
    for edge in data['edges']:
        lanes = edge.get('lanes')
        if type(lanes) is not int or lanes <= 0:
            raise ValueError('every edge must specify a positive lane count')
        directed_edges.add((edge['from'], edge['to']))
        if edge['bidirectional']:
            directed_edges.add((edge['to'], edge['from']))
    if any((start, end) not in directed_edges for start, end in zip(nodes, nodes[1:])):
        raise ValueError('path must follow direct edges in the allowed direction')

    zones = data.get('zones')
    if not isinstance(zones, list):
        raise ValueError('map zones must be explicitly specified')
    waiting, shared, seen_zone_ids = set(), [], set()
    for zone in zones:
        if not isinstance(zone, dict) or not _text_id(zone.get('id')):
            raise ValueError('zone needs a non-empty id')
        zone_id = zone['id']
        if zone_id in seen_zone_ids:
            raise ValueError('zone IDs must be unique')
        seen_zone_ids.add(zone_id)
        if zone.get('type') == 'waiting':
            node = zone.get('node')
            if not _text_id(node) or node not in node_ids:
                raise ValueError('waiting zone must specify a known node')
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

    end = next((i for i in range(1, len(nodes)) if nodes[i] in waiting), None)
    if end is None:
        raise ValueError('no waiting node exists on the route after the start')
    segment = nodes[:end + 1]
    resources = {('node', node) for node in segment}
    resources.update(('edge', _edge_id(start, finish))
                     for start, finish in zip(segment, segment[1:]))
    resources.update(('zone', zone_id) for zone_id, members in shared
                     if members.intersection(segment))
    return Segment(segment, segment[-1], tuple(sorted(resources)))
