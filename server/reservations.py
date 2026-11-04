"""Atomic in-memory reservation primitive; not a complete safety interlock.

The caller must derive the complete, canonical resource set from a validated
map and confirm that the robot's rear has cleared before requesting release.
This table is volatile and cannot recover occupancy after a server restart.
It must not be used to grant real motion without durable recovery and a
verified physical stop/clearance protocol. Locally generated lease IDs prevent
an old clearance from releasing a later reservation; relating them to robot
events still requires a separately agreed protocol.
"""
import threading
import uuid


def _resource_set(robot_id, resources):
    if not isinstance(robot_id, str) or not robot_id.strip():
        raise ValueError("robot_id must be a non-empty string")
    if not isinstance(resources, (list, tuple, set, frozenset)) or not resources:
        raise ValueError("resources must be a non-empty collection")
    result = set()
    for resource in resources:
        if (not isinstance(resource, tuple) or len(resource) != 2
                or resource[0] not in ("node", "edge", "zone")
                or not isinstance(resource[1], str) or not resource[1].strip()):
            raise ValueError("resource must be a (node, edge or zone, id) tuple")
        result.add(resource)
    return result


class Reservations:
    def __init__(self):
        self._owners = {}
        self._leases = {}
        self._lock = threading.Lock()

    def reserve(self, robot_id, resources):
        requested = _resource_set(robot_id, resources)
        with self._lock:
            if any(resource in self._owners and self._owners[resource] != robot_id
                   for resource in requested):
                return None
            occupied = {resource for resource in requested if resource in self._owners}
            if occupied:
                if occupied != requested:
                    return None
                leases = {self._leases[resource] for resource in requested}
                if len(leases) != 1:
                    return None
                lease = next(iter(leases))
                held = {resource for resource, value in self._leases.items() if value == lease}
                return lease if held == requested else None
            lease = uuid.uuid4().hex
            for resource in requested:
                self._owners[resource] = robot_id
                self._leases[resource] = lease
            return lease

    def release_after_rear_clear(self, robot_id, resources, lease):
        cleared = _resource_set(robot_id, resources)
        with self._lock:
            if any(self._owners.get(resource) != robot_id or self._leases.get(resource) != lease
                   for resource in cleared):
                raise ValueError("all cleared resources must match the robot and lease")
            for resource in cleared:
                del self._owners[resource]
                del self._leases[resource]

    def snapshot(self):
        with self._lock:
            return dict(self._owners)
