"""Pure compatibility checks for the field-run robot's documented order parser.

This module does not publish, authorize motion, check a robot's live location,
or prove map/session identity. The robot additionally applies stateful OrderBook
checks against its active task and current release index. The normal simulation
publisher is unchanged; callers must explicitly opt into this preflight check.
"""
from server.order_publisher import _payload


MAX_FIELD_ORDER_BYTES = 64 * 1024
MAX_FIELD_ID_CHARS = 128
MAX_FIELD_NODES = 256


def validate_field_robot_order(order):
    """Raise ValueError if the JSON order violates stateless field-run limits.

    The emitted UTF-8 representation is the server's actual wire encoding.
    Validation is conservative and is not evidence of receipt or execution.
    """
    payload = _payload(order)
    if len(payload) > MAX_FIELD_ORDER_BYTES:
        raise ValueError('field robot order exceeds 64 KiB')
    if order['seq'] < 1:
        raise ValueError('field robot sequence must start at 1')
    nodes = order['nodes']
    if not 2 <= len(nodes) <= MAX_FIELD_NODES:
        raise ValueError('field robot route must contain 2 to 256 nodes')
    if any(a == b for a, b in zip(nodes, nodes[1:])):
        raise ValueError('field robot route cannot have adjacent duplicate nodes')
    ids = [order['order_id'], order['map_id'], *nodes]
    if any(len(item) > MAX_FIELD_ID_CHARS for item in ids):
        raise ValueError('field robot order identifiers cannot exceed 128 characters')
    if order['seq'] == 1 and order['released_until'] not in nodes[1:]:
        raise ValueError('field robot first release must follow the initial node')
