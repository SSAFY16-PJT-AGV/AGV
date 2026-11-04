"""Directed shortest paths over a map's 2-D edge polylines.

This module computes geometry only. It does not grant reservations, command
robots, validate localization or guarantee that a route is safe to drive.
Node positions and corresponding polyline endpoints must match exactly;
the geometry tolerance is zero until the team agrees an error allowance.
"""

from collections import namedtuple
import heapq
import itertools
import math


class InvalidMapError(ValueError):
    """The map is missing or contains ambiguous or unusable geometry."""


class UnknownNodeError(ValueError):
    """The requested start or end node does not exist in this map."""


class NoRouteError(ValueError):
    """The directed graph has no path between the requested nodes."""


Route = namedtuple("Route", ["nodes", "distance_mm"])


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidMapError("shape coordinates must be finite numbers")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise InvalidMapError("shape coordinates must be finite numbers")
    if not math.isfinite(result):
        raise InvalidMapError("shape coordinates must be finite numbers")
    return result


def _length(shape):
    if not isinstance(shape, list) or len(shape) < 2:
        raise InvalidMapError("each edge needs a polyline with at least two points")
    points = []
    for point in shape:
        if not isinstance(point, list) or len(point) != 2:
            raise InvalidMapError("edge shape points must be 2-D coordinates")
        points.append((_number(point[0]), _number(point[1])))
    segments = [math.hypot(b[0] - a[0], b[1] - a[1])
                for a, b in zip(points, points[1:])]
    try:
        distance = math.fsum(segments)
    except OverflowError:
        raise InvalidMapError("edge length must be finite and positive")
    if not math.isfinite(distance) or distance <= 0:
        raise InvalidMapError("edge length must be finite and positive")
    return distance


class MapGraph:
    """A snapshot of valid map edges; shape distances are in millimeters."""

    def __init__(self, data):
        if not isinstance(data, dict):
            raise InvalidMapError("map must be an object")
        map_id = data.get("map_id")
        version = data.get("version")
        if not isinstance(map_id, str) or not map_id.strip():
            raise InvalidMapError("map_id must be a nonempty string")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise InvalidMapError("version must be a nonnegative integer")
        if data.get("unit") != "mm":
            raise InvalidMapError("map unit must be mm")
        nodes = data.get("nodes")
        edges = data.get("edges")
        if not isinstance(nodes, list) or not nodes or not isinstance(edges, list):
            raise InvalidMapError("nodes and edges must be lists and nodes nonempty")
        adjacency = {}
        positions = {}
        for node in nodes:
            if not isinstance(node, dict):
                raise InvalidMapError("node must be an object")
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id.strip() or node_id in adjacency:
                raise InvalidMapError("node ids must be unique nonempty strings")
            position = node.get("pos")
            if not isinstance(position, list) or len(position) != 2:
                raise InvalidMapError("node positions must be 2-D coordinates")
            for coordinate in position:
                _number(coordinate)
            adjacency[node_id] = {}
            positions[node_id] = tuple(position)

        for edge in edges:
            if not isinstance(edge, dict):
                raise InvalidMapError("edge must be an object")
            source, target = edge.get("from"), edge.get("to")
            if not isinstance(source, str) or not isinstance(target, str):
                raise InvalidMapError("edge endpoints must be node ids")
            if source not in adjacency or target not in adjacency or source == target:
                raise InvalidMapError("edge endpoints must be distinct known nodes")
            bidirectional = edge.get("bidirectional")
            if not isinstance(bidirectional, bool):
                raise InvalidMapError("bidirectional must be a boolean")
            shape = edge.get("shape")
            distance = _length(shape)
            if tuple(shape[0]) != positions[source] or tuple(shape[-1]) != positions[target]:
                raise InvalidMapError("edge endpoints must exactly match node positions")
            arcs = [(source, target)]
            if bidirectional:
                arcs.append((target, source))
            for start, end in arcs:
                if end in adjacency[start]:
                    raise InvalidMapError("multiple edges in one direction are ambiguous")
                adjacency[start][end] = distance

        self.map_id = map_id
        self.version = version
        self._adjacency = adjacency

    def shortest_path(self, start, target):
        if not isinstance(start, str) or not isinstance(target, str):
            raise UnknownNodeError("start and target must be node ids")
        if start not in self._adjacency or target not in self._adjacency:
            raise UnknownNodeError("start or target is not in the map")
        distances = {start: 0.0}
        previous = {}
        counter = itertools.count()
        queue = [(0.0, next(counter), start)]
        while queue:
            distance, _, node = heapq.heappop(queue)
            if distance != distances.get(node):
                continue
            if node == target:
                result = [target]
                while result[-1] != start:
                    result.append(previous[result[-1]])
                return Route(tuple(reversed(result)), distance)
            for neighbour, edge_distance in self._adjacency[node].items():
                candidate = distance + edge_distance
                if not math.isfinite(candidate):
                    raise InvalidMapError("route distance exceeds numeric range")
                if candidate < distances.get(neighbour, math.inf):
                    distances[neighbour] = candidate
                    previous[neighbour] = node
                    heapq.heappush(queue, (candidate, next(counter), neighbour))
        raise NoRouteError("no directed route exists")
