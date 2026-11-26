"""Compare field and Unreal snapshots; produce a clearly synthetic projection.

The Unreal loader reads metre-based node x/y and edge points.  Matching here
means geometry and listed semantics only, not navigation compatibility.  This
does not calibrate the authored Unreal level or establish physical safety.
"""
import copy
import math


_SEMANTIC_FIELDS = ('type', 'color', 'role', 'purpose', 'group')


def _name(value, name):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
        raise ValueError('invalid ' + name)
    return value


def _number(value, name):
    if type(value) not in (int, float) or abs(value) > 1000000 or not math.isfinite(value):
        raise ValueError('invalid ' + name)
    return float(value)


def _nodes(data, *, field):
    if not isinstance(data, dict) or not isinstance(data.get('nodes'), list) or not data['nodes']:
        raise ValueError('map nodes are required')
    if field and (data.get('unit') != 'mm' or data.get('coordinate_unit') != 'meter'):
        raise ValueError('field map must use millimetre pos and metre x/y')
    if not field and data.get('coordinate_unit') != 'meter':
        raise ValueError('Unreal map must use metres')
    _name(data.get('map_id'), 'map id')
    result = {}
    for node in data['nodes']:
        if not isinstance(node, dict):
            raise ValueError('invalid map node')
        name = _name(node.get('id'), 'node id')
        if name in result:
            raise ValueError('duplicate map node')
        xy = (_number(node.get('x'), 'node x'), _number(node.get('y'), 'node y'))
        if field:
            pos = node.get('pos')
            if not isinstance(pos, list) or len(pos) != 2:
                raise ValueError('field node pos must be a pair')
            metric = tuple(_number(v, 'field position') / 1000 for v in pos)
            if math.hypot(xy[0] - metric[0], xy[1] - metric[1]) > 1e-6:
                raise ValueError('field x/y disagrees with millimetre pos')
        for key in _SEMANTIC_FIELDS:
            if key in node:
                _name(node[key], 'node ' + key)
        result[name] = (xy, node)
    return result


def _edges(data, node_ids, *, field=False):
    if not isinstance(data.get('edges'), list):
        raise ValueError('map edges are required')
    result = {}
    for edge in data['edges']:
        if not isinstance(edge, dict):
            raise ValueError('invalid map edge')
        if field and (edge.get('directed') is True or edge.get('bidirectional') is False):
            raise ValueError('Unreal projection cannot represent a one-way edge')
        for key in ('directed', 'bidirectional'):
            if field and key in edge and type(edge[key]) is not bool:
                raise ValueError('invalid edge direction')
        source = _name(edge.get('from'), 'edge from')
        target = _name(edge.get('to'), 'edge to')
        kind = _name(edge.get('type'), 'edge type')
        if source not in node_ids or target not in node_ids or source == target:
            raise ValueError('invalid edge endpoints')
        points = edge.get('points')
        if not isinstance(points, list):
            raise ValueError('edge points are required')
        normalized = []
        for point in points:
            if not isinstance(point, dict):
                raise ValueError('invalid edge point')
            normalized.append((_number(point.get('x'), 'edge x'), _number(point.get('y'), 'edge y')))
        # The current Unreal loader traverses its edges in both directions.
        # Preserve geometry order while allowing the equivalent reverse edge.
        if source > target:
            source, target = target, source
            normalized.reverse()
        key = (source, target, kind)
        if key in result:
            raise ValueError('ambiguous duplicate edge')
        result[key] = normalized
    return result


def project_field_map_to_twin(field_map):
    """Project one explicit field snapshot into the UE loader's JSON schema.

    The projection is synthetic: it changes neither level actors nor the
    Unreal project, and it does not demonstrate scene or physical alignment.
    """
    nodes = _nodes(field_map, field=True)
    _edges(field_map, nodes, field=True)
    result_nodes = []
    for node in field_map['nodes']:
        projected = {key: copy.deepcopy(node[key]) for key in ('id', 'x', 'y') + _SEMANTIC_FIELDS if key in node}
        if 'type' not in projected:
            raise ValueError('Unreal projection needs node type')
        result_nodes.append(projected)
    result_edges = [dict((key, copy.deepcopy(edge[key])) for key in ('from', 'to', 'type', 'points'))
                    for edge in field_map['edges']]
    return {'map_id': field_map['map_id'], 'coordinate_unit': 'meter',
            'nodes': result_nodes, 'edges': result_edges}


def compare_field_twin_maps(field_map, unreal_map, *, tolerance_m=1e-6):
    """Report geometry and listed semantics; this is not a navigation proof."""
    tolerance = _number(tolerance_m, 'comparison tolerance')
    if not 0 <= tolerance <= .25:
        raise ValueError('comparison tolerance must be between zero and 0.25 m')
    field_nodes, twin_nodes = _nodes(field_map, field=True), _nodes(unreal_map, field=False)
    field_edges, twin_edges = _edges(field_map, field_nodes, field=True), _edges(unreal_map, twin_nodes)
    mismatch = {}
    for name in sorted(field_nodes.keys() & twin_nodes.keys()):
        (field_xy, field_node), (twin_xy, twin_node) = field_nodes[name], twin_nodes[name]
        errors = {}
        distance = math.hypot(field_xy[0] - twin_xy[0], field_xy[1] - twin_xy[1])
        if distance > tolerance:
            errors['coordinates_m'] = {'field': list(field_xy), 'unreal': list(twin_xy), 'distance': distance}
        for key in _SEMANTIC_FIELDS:
            if field_node.get(key) != twin_node.get(key):
                errors[key] = {'field': field_node.get(key), 'unreal': twin_node.get(key)}
        if errors:
            mismatch[name] = errors
    field_only = sorted(field_nodes.keys() - twin_nodes.keys())
    twin_only = sorted(twin_nodes.keys() - field_nodes.keys())
    only_field_edges = sorted(field_edges.keys() - twin_edges.keys())
    only_twin_edges = sorted(twin_edges.keys() - field_edges.keys())
    geometry = []
    for key in sorted(field_edges.keys() & twin_edges.keys()):
        left, right = field_edges[key], twin_edges[key]
        if (len(left) != len(right) or any(math.hypot(a[0] - b[0], a[1] - b[1]) > tolerance
                                           for a, b in zip(left, right))):
            geometry.append({'edge': list(key), 'field': left, 'unreal': right})
    metadata = {}
    if field_map['map_id'] != unreal_map['map_id']:
        metadata['map_id'] = {'field': field_map['map_id'], 'unreal': unreal_map['map_id']}
    return {'compatible': not (metadata or field_only or twin_only or mismatch or only_field_edges
                                or only_twin_edges or geometry),
            'metadata_mismatches': metadata, 'field_only_nodes': field_only, 'twin_only_nodes': twin_only,
            'node_mismatches': mismatch, 'field_only_edges': [list(e) for e in only_field_edges],
            'twin_only_edges': [list(e) for e in only_twin_edges], 'edge_geometry_mismatches': geometry}
