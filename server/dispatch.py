"""Choose an available robot by directed distance to the pickup node.

This independent calculation does not reserve or allocate anything. The caller
must validate localization, connection, availability and freshness beforehand.
"""

from collections import namedtuple

from server.routing import MapGraph, NoRouteError


RobotCandidate = namedtuple("RobotCandidate", ["robot_id", "node", "available"])
DispatchChoice = namedtuple("DispatchChoice", ["robot_id", "route"])


def choose_robot(graph, pickup_node, candidates):
    """Return nearest eligible candidate, or None; exact ties use robot ID order."""
    if not isinstance(graph, MapGraph):
        raise TypeError("graph must be a MapGraph")
    # Validate pickup even when there are no eligible robots.
    graph.shortest_path(pickup_node, pickup_node)
    checked = []
    seen = set()
    for item in candidates:
        if not isinstance(item, RobotCandidate):
            raise TypeError("candidates must be RobotCandidate records")
        if not isinstance(item.robot_id, str) or not item.robot_id.strip():
            raise ValueError("robot id must be a nonempty string")
        if item.robot_id in seen:
            raise ValueError("duplicate robot id")
        if not isinstance(item.node, str) or not item.node.strip():
            raise ValueError("robot node must be a nonempty string")
        if type(item.available) is not bool:
            raise ValueError("available must be a boolean")
        seen.add(item.robot_id)
        checked.append(item)

    routes = []
    for item in checked:
        if not item.available:
            continue
        try:
            route = graph.shortest_path(item.node, pickup_node)
        except NoRouteError:
            continue
        routes.append((route.distance_mm, item.robot_id, route))

    if not routes:
        return None
    _, robot_id, route = min(routes, key=lambda item: (item[0], item[1]))
    return DispatchChoice(robot_id, route)
