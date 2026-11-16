"""Convert an Unreal map to a *simulation-only* reservation map.

The source scene has no operational lane, shared-zone or safe-waiting data.
For this isolated demonstration every edge is assumed bidirectional and has
one exclusive lane, and every node is treated as a waiting point. No shared
zones are inferred. These assumptions do not describe a physical warehouse,
establish collision safety or replace the team's map. Never use this output
as authorization to move a real robot.
"""
from server.order_demo_engine import _convert_map
from server.routing import MapGraph
from server.segments import plan_segment


def make_order_sim_map(unreal_map):
    """Return a new mm map accepted by the isolated scheduling components.

    Unreal map version is retained when present; the existing demo map has no
    version, so this isolated conversion labels it version 1. Explicit one-way
    edges are rejected rather than silently turned into two-way connections.
    """
    graph, positions, shapes, _ = _convert_map(unreal_map)
    map_id = graph.map_id
    if '@' in map_id or any(c.isspace() for c in map_id):
        raise ValueError('simulation map ID must not contain @ or whitespace')
    version = unreal_map.get('version', 1)
    if type(version) is not int or version < 0:
        raise ValueError('simulation map version must be a nonnegative integer')
    if not unreal_map['edges']:
        raise ValueError('simulation map must contain at least one edge')

    nodes = [{'id': node['id'], 'pos': list(positions[node['id']])}
             for node in unreal_map['nodes']]
    edges = []
    for edge in unreal_map['edges']:
        if 'bidirectional' in edge and edge['bidirectional'] is not True:
            raise ValueError('explicit one-way or invalid edge is unsupported')
        source, target = edge['from'], edge['to']
        edges.append({'from': source, 'to': target,
                      'shape': [list(point) for point in shapes[(source, target)]],
                      'bidirectional': True, 'lanes': 1})
    zones = [{'id': 'sim-wait-{:06d}'.format(index), 'type': 'waiting', 'node': node['id']}
             for index, node in enumerate(nodes)]
    result = {'map_id': map_id, 'version': version, 'unit': 'mm',
              'nodes': nodes, 'edges': edges, 'zones': zones}
    MapGraph(result)
    # plan_segment validates every edge and all waiting zones before producing
    # a segment. Every node is a demo waiting point, so any edge suffices.
    plan_segment(result, [edges[0]['from'], edges[0]['to']])
    return result
