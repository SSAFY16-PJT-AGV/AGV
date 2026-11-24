"""Robot ID classes shared by the order, reservation and recovery path.

``sim-`` robots are virtual robots in an isolated simulation. ``physical-``
robots are the team's JETANK robots running the field-run ``serve`` mode
(see feature/field-run agv/robotcfg.py; e.g. ``physical-1``).

Both classes may use the same order -> reservation -> rear_clear -> next order
path. Modules that rely on synthetic acknowledgements or synthetic pickup and
placement results (the web product demo, Unreal action replies, synthetic
peers) keep accepting ``sim-`` robots only; such virtual results must never be
used as evidence for a physical robot.

Accepting a physical ID is not by itself proof of identity, position or a
physical stop. Broker authentication and the stop evidence rules (see
``server/physical_state_proof.py``) still apply.
"""
SIM_PREFIX = 'sim-'
PHYSICAL_PREFIX = 'physical-'
FLEET_PREFIXES = (SIM_PREFIX, PHYSICAL_PREFIX)


def _has_prefix(robot_id, prefix):
    return (isinstance(robot_id, str) and robot_id.startswith(prefix)
            and bool(robot_id[len(prefix):].strip()))


def is_sim_robot(robot_id):
    """True for an isolated virtual robot ID such as ``sim-r1``."""
    return _has_prefix(robot_id, SIM_PREFIX)


def is_physical_robot(robot_id):
    """True for a team JETANK robot ID such as ``physical-1``."""
    return _has_prefix(robot_id, PHYSICAL_PREFIX)


def is_fleet_robot(robot_id):
    """True for a robot that may use the order/reservation/rear_clear path."""
    return is_sim_robot(robot_id) or is_physical_robot(robot_id)
