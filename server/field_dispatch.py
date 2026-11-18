"""Direction-aware selection for explicitly simulated robot candidates.

The caller must separately establish freshness, location and identity; this
pure calculation does not authorize movement or publish any commands.
"""
from collections import namedtuple

from server.field_map_planner import NoFieldRouteError, plan_field_route
from server.order_publisher import _topic
from server.routing import MapGraph


FieldCandidate = namedtuple('FieldCandidate', ['robot_id', 'node', 'incoming_edge', 'available'])
FieldDispatchChoice = namedtuple('FieldDispatchChoice', ['robot_id', 'route'])


def choose_field_robot(map_data, map_ref, destination, candidates):
    """Pick the nearest eligible simulation candidate using directed entry edges."""
    graph = MapGraph(map_data)
    expected = '{}@v{}'.format(graph.map_id, graph.version)
    if map_ref != expected:
        raise ValueError('map reference does not match')
    nodes = {node['id']: node for node in map_data['nodes']}
    if (not isinstance(destination, str) or destination not in nodes
            or nodes[destination].get('kind') == 'spur_end'
            or nodes[destination].get('type') == 'destination'):
        raise ValueError('unknown or unsupported destination')
    if not isinstance(candidates, (list, tuple)):
        raise ValueError('candidates must be a list or tuple')
    seen, choices = set(), []
    for candidate in candidates:
        if not isinstance(candidate, FieldCandidate) or type(candidate.available) is not bool:
            raise ValueError('invalid candidate')
        _topic(candidate.robot_id)  # Enforce the existing simulation-only target rule.
        if candidate.robot_id in seen:
            raise ValueError('duplicate robot id')
        seen.add(candidate.robot_id)
        if not isinstance(candidate.node, str) or candidate.node not in nodes:
            raise ValueError('unknown candidate node')
        # Compute a self-route first to validate the entry edge and node even
        # if the robot is unavailable or cannot reach the requested goal.
        origin = plan_field_route(map_data, map_ref=map_ref,
                                  incoming_edge=candidate.incoming_edge, goal=candidate.node)
        if origin.nodes != (candidate.node,):
            raise ValueError('incoming edge does not end at candidate node')
        if not candidate.available:
            continue
        try:
            route = plan_field_route(map_data, map_ref=map_ref,
                                     incoming_edge=candidate.incoming_edge, goal=destination)
        except NoFieldRouteError:
            continue
        choices.append((route.distance_mm, candidate.robot_id, route))
    if not choices:
        return None
    _, robot_id, route = min(choices, key=lambda value: (value[0], value[1]))
    return FieldDispatchChoice(robot_id, route)
