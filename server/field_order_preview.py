"""Offline inspection of an existing team-format order, never a drive permit.

The returned value is a description of input, not authenticated evidence.
This module never connects to a broker, publishes or changes reservations.
"""
from dataclasses import dataclass
import copy
import hashlib
import math

from server.field_robot_contract import validate_field_robot_order
from server.order_publisher import _payload


@dataclass(frozen=True)
class FieldOrderPreview:
    """Immutable summary of the exact inspected order encoding."""
    robot_id: str
    order_id: str
    sequence: int
    map_id: str
    route_start_node: str
    payload_sha256: str


def inspect_field_order(robot_id, order):
    """Validate robot ID and the team-format six-field order offline.

    This does not authenticate robot_id, verify a map, or prove position,
    receipt, stop, clearance or safe movement. It grants no permissions.
    """
    if (not isinstance(robot_id, str) or not robot_id or robot_id != robot_id.strip()
            or any(char in '/+#' or ord(char) < 32 or ord(char) == 127 for char in robot_id)):
        raise ValueError('invalid field robot ID')
    try:
        encoded = robot_id.encode('utf-8', errors='strict')
    except UnicodeEncodeError as exc:
        raise ValueError('invalid field robot ID') from exc
    if len(encoded) > 128:
        raise ValueError('field robot ID exceeds temporary size limit')
    snapshot = copy.deepcopy(order)
    validate_field_robot_order(snapshot)
    payload = _payload(snapshot)
    return FieldOrderPreview(robot_id=robot_id, order_id=snapshot['order_id'],
                             sequence=snapshot['seq'], map_id=snapshot['map_id'],
                             route_start_node=snapshot['nodes'][0],
                             payload_sha256=hashlib.sha256(payload).hexdigest())


@dataclass(frozen=True)
class ProposedObservationReview:
    """Detected contradictions in caller-supplied claims, not permission."""
    issues: tuple


def _finite_nonnegative(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def review_proposed_observation(preview, observation, *, expected_session,
                                expected_incoming_edge, now, maximum_age):
    """Compare synthetic claims; timestamps must use one local monotonic clock.

    Values and timestamps are supplied by the caller, not obtained from MQTT.
    An empty issues tuple is not evidence, authentication, or authorization.
    """
    if not isinstance(preview, FieldOrderPreview) or not isinstance(observation, dict):
        raise ValueError('preview and observation are required')
    if preview.sequence != 1:
        raise ValueError('observation review only supports a first order')
    for value in (expected_session, expected_incoming_edge):
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError('expected session and incoming edge are required')
    if not _finite_nonnegative(now) or not _finite_nonnegative(maximum_age) or maximum_age == 0:
        raise ValueError('invalid local monotonic clock or maximum age')

    required = ('robot_id', 'robot_session', 'map_id', 'node', 'incoming_edge',
                'status', 'online', 'retained', 'received_at')
    issues = ['missing:' + key for key in required if key not in observation]
    for key, expected in (('robot_id', preview.robot_id), ('robot_session', expected_session),
                          ('map_id', preview.map_id), ('node', preview.route_start_node),
                          ('incoming_edge', expected_incoming_edge)):
        if key in observation and observation[key] != expected:
            issues.append('mismatch:' + key)
    if 'status' in observation and observation['status'] != 'IDLE':
        issues.append('not_idle')
    if 'online' in observation and observation['online'] is not True:
        issues.append('offline')
    if 'retained' in observation and observation['retained'] is not False:
        issues.append('retained')
    if 'received_at' in observation:
        received = observation['received_at']
        if not _finite_nonnegative(received):
            issues.append('invalid:received_at')
        elif received > now:
            issues.append('future')
        elif now - received > maximum_age:
            issues.append('stale')
    return ProposedObservationReview(tuple(issues))
