"""Validate a robot's own report before preparing its first field task.

This does not authenticate the report, establish its freshness, or prove the
robot's physical position, orientation, or stop. The caller must separately
bind it to the correct MQTT topic, connection, and live reception generation.
"""
from server.allocation_store import _id


def validate_initial_state(state, *, expected_map, expected_node):
    """Return the session only if the team state agrees with initial config."""
    _id(expected_map, 'expected_map')
    _id(expected_node, 'expected_node')
    if not isinstance(state, dict):
        raise ValueError('initial robot state must be an object')
    required = ('robot_session', 'map_id', 'last_node', 'status', 'error',
                'order_id', 'seq', 'released_until')
    if not all(key in state for key in required):
        raise ValueError('initial robot state is missing required fields')
    session = _id(state['robot_session'], 'robot_session')
    if session == 'from_state':
        raise ValueError('reserved robot session is not valid state')
    if (state['map_id'] != expected_map or state['last_node'] != expected_node
            or state['status'] != 'IDLE'):
        raise ValueError('initial robot state does not match configured map and location')
    if any(state[key] is not None for key in ('error', 'order_id', 'seq', 'released_until')):
        raise ValueError('initial robot state reports an error or an existing order')
    return session
