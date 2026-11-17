"""Geometry-only simulated order executor; never connects to robot hardware.

The network, recovery confirmation and rear-clear event are managed by the
caller. Reaching a node here is a simulated result, not physical evidence.
"""
import copy
import math
import threading

from server.routing import MapGraph
from sim.order_inbox import OrderRejected, SimOrderInbox, Receipt, _decode


SPEED_MM_PER_SECOND = 1000.0


class SimOrderExecutor:
    """Move one virtual pose only within a durably accepted order release."""

    def __init__(self, inbox, map_data, start_node):
        if not isinstance(inbox, SimOrderInbox):
            raise ValueError('a simulated order inbox is required')
        data = copy.deepcopy(map_data)
        graph = MapGraph(data)
        if inbox.map_id != '{}@v{}'.format(graph.map_id, graph.version):
            raise ValueError('inbox map and executor map differ')
        positions = {node['id']: tuple(float(v) for v in node['pos']) for node in data['nodes']}
        if not isinstance(start_node, str) or start_node not in positions:
            raise ValueError('start node must be in the map')
        shapes = {}
        for edge in data['edges']:
            source, destination = edge['from'], edge['to']
            points = tuple(tuple(float(v) for v in point) for point in edge['shape'])
            shapes[(source, destination)] = points
            if edge['bidirectional']:
                shapes[(destination, source)] = tuple(reversed(points))
        self._inbox, self._shapes, self._positions = inbox, shapes, positions
        self._current_node = start_node
        self._x, self._y = positions[start_node]
        self._yaw = 0.0
        self._status = 'idle'
        self._order = self._receipt = None
        self._recovery_evidence = None
        self._waypoints = ()
        self._next_waypoint = 0
        self._lock = threading.RLock()

    def accept(self, payload):
        """Validate before persisting; duplicates never re-execute a movement."""
        order, _canonical, digest = _decode(payload, self._inbox.robot_id, self._inbox.map_id)
        with self._lock:
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
            else:
                if (self._receipt is None or self._order is None or self._status != 'stopped'
                        or order['order_id'] != self._receipt.order_id
                        or order['seq'] != self._receipt.seq + 1
                        or self._current_node != self._order['released_until']
                        or self._current_node not in nodes):
                    raise OrderRejected('next segment does not follow the stopped order')
                start = nodes.index(self._current_node)
            end = nodes.index(stop)
            if end <= start:
                raise OrderRejected('release must advance beyond the current node')
            waypoints = []
            for source, target in zip(nodes[start:end], nodes[start + 1:end + 1]):
                points = self._shapes[(source, target)]
                for index, (x, y) in enumerate(points[1:], 1):
                    waypoints.append((x, y, target if index == len(points) - 1 else None))
            if not waypoints:
                raise OrderRejected('released segment contains no geometry')

            # SimOrderInbox checks duplicate content, order continuity, map and
            # persistence before this instance ever starts moving the pose.
            receipt = self._inbox.accept(payload)
            if not receipt.new:
                return receipt
            self._order = copy.deepcopy(order)
            self._receipt = receipt
            self._recovery_evidence = None
            self._waypoints = tuple(waypoints)
            self._next_waypoint = 0
            self._status = 'moving'
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
                if self._next_waypoint == len(self._waypoints):
                    try:
                        self._inbox.mark_completed(self._receipt.order_id, self._receipt.seq,
                                                   self._receipt.digest, self._current_node,
                                                   rear_clear_confirmed=True)
                    except Exception:
                        self._status = 'error'
                        raise
                    self._receipt = self._receipt._replace(state='completed')
                    self._status = 'stopped'
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            result = {'status': self._status, 'current_node': self._current_node,
                    'pose': {'x': self._x, 'y': self._y, 'th': self._yaw},
                    'order': copy.deepcopy(self._order),
                    'receipt': self._receipt._asdict() if self._receipt else None}
            if self._recovery_evidence is not None:
                result['recovered'] = True
            return result

    def _restore_completed(self, order, digest, evidence):
        """Install already validated historical stop; never execute an order."""
        with self._lock:
            if self._status != 'idle' or self._order is not None or self._receipt is not None:
                raise OrderRejected('executor is not fresh and idle')
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
            self._recovery_evidence = copy.deepcopy(evidence)
            self._status = 'stopped'

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
