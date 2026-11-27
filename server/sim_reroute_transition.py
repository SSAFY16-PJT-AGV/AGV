"""Durable reservation boundary for an explicitly enabled simulated reroute.

Preparing a transition does not change a route, publish a command, move a
robot, or interpret a previous rear-clear event as clearance for a new route.
The old and proposed first segments are held together in the existing
allocation_occupancy table until a future, separately verified transition.
"""
import json
import re
import secrets
import sqlite3

from server.allocation_store import AllocationConflict
from server.field_map_planner import plan_field_route
from server.segments import plan_segment
from server.segment_progress import _resources
from sim.event_inbox import _event


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_generations ('
               'task_id TEXT PRIMARY KEY NOT NULL REFERENCES allocation_jobs(task_id), '
               'generation INTEGER NOT NULL, transition_id TEXT UNIQUE NOT NULL, '
               'robot_id TEXT NOT NULL, robot_session TEXT NOT NULL, parent_seq INTEGER NOT NULL, '
               'event_id TEXT NOT NULL, event_digest TEXT NOT NULL, old_payload TEXT NOT NULL, '
               'stopped_at TEXT NOT NULL, incoming_edge TEXT NOT NULL, '
               'nodes_json TEXT NOT NULL, edges_json TEXT NOT NULL, blocked_json TEXT NOT NULL, '
               'new_resources_json TEXT NOT NULL, recovery_epoch INTEGER NOT NULL, state TEXT NOT NULL)')


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def _pairs(value):
    return [list(item) for item in sorted(value)]


def is_prepared(db, task_id):
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sim_reroute_generations'").fetchone()
    return bool(table and db.execute('SELECT 1 FROM sim_reroute_generations WHERE task_id=?', (task_id,)).fetchone())


def reserve(db, runtime, task_id, wait, path, incoming, candidate, blocked, old_resources):
    """Claim the proposal inside the existing gate's active write transaction."""
    progress = runtime.progress
    if is_prepared(db, task_id):
        raise AllocationConflict('a reroute is already prepared')
    segment = plan_segment(progress._map, list(candidate.nodes))
    new_resources = set(segment.resources)
    if not new_resources or new_resources & blocked:
        raise AllocationConflict('reroute conflicts with existing resources')
    if progress._occupancy(db, task_id) != old_resources:
        raise AllocationConflict('original segment occupancy changed')
    event = db.execute("SELECT payload,digest,robot_session,seq,state FROM sim_event_inbox "
                       'WHERE robot_id=? AND event_id=?', (wait['robot_id'], wait['event_id'])).fetchone()
    if event is None or event[4] != 'pending':
        raise AllocationConflict('reroute requires the classified pending event')
    seq, old_payload, state, _order = progress._latest(db, task_id, wait['robot_id'], path, wait['stopped_at'])
    if state not in ('uncertain', 'acked') or event[3] != seq:
        raise AllocationConflict('reroute parent command changed')
    with runtime.gate._state_lock:
        if task_id in runtime.gate._blocked:
            raise AllocationConflict('simulated recovery is blocked')
        epoch = runtime.gate._generations.get(task_id, 0)
    from server.sim_handoff_store import SimHandoffStore
    if type(runtime.store) is SimHandoffStore:
        runtime.store._check_resource_availability(db, new_resources - old_resources, ignore_task=task_id)
    transition_id = secrets.token_hex(16)
    try:
        db.executemany('INSERT INTO allocation_occupancy(kind,resource_id,task_id) VALUES(?,?,?)',
                       [(kind, resource, task_id) for kind, resource in sorted(new_resources - old_resources)])
        db.execute('INSERT INTO sim_reroute_generations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (task_id, 1, transition_id, wait['robot_id'], event[2], seq, wait['event_id'], event[1], old_payload,
                    wait['stopped_at'], incoming, _json(list(candidate.nodes)), _json(list(candidate.edge_ids)),
                    _json(_pairs(blocked)), _json(_pairs(new_resources)), epoch, 'prepared'))
    except sqlite3.IntegrityError as exc:
        raise AllocationConflict('reroute reservation conflicts with another task') from exc
    return {'state': 'prepared', 'task_id': task_id, 'generation': 1, 'transition_id': transition_id,
            'scope': 'isolated_simulation',
            'nodes': list(candidate.nodes), 'previous_resources_retained': True,
            'published': False, 'motion_authorized': False}


def verify_prepared(db, progress, task_id, robot, path, stopped, seq, payload, old_resources):
    """Validate a prepared generation and return its exact extra resources."""
    if not is_prepared(db, task_id):
        return set()
    row = db.execute('SELECT generation,transition_id,robot_id,robot_session,parent_seq,event_id,event_digest,old_payload,stopped_at,'
                     'incoming_edge,nodes_json,edges_json,blocked_json,new_resources_json,recovery_epoch,state '
                     'FROM sim_reroute_generations WHERE task_id=?', (task_id,)).fetchone()
    try:
        (generation, transition_id, saved_robot, session, parent_seq, event_id, digest, saved_payload, saved_stop, incoming,
         nodes_json, edges_json, blocked_json, resources_json, epoch, state) = row
        if (generation != 1 or type(transition_id) is not str or not re.fullmatch('[0-9a-f]{32}', transition_id)
                or saved_robot != robot or type(session) is not str or not session or type(parent_seq) is not int
                or parent_seq != seq or saved_payload != payload or saved_stop != stopped
                or type(epoch) is not int or epoch < 0 or state != 'prepared'):
            raise ValueError('reroute parent or state changed')
        nodes, edges = json.loads(nodes_json), json.loads(edges_json)
        blocked_raw, resources_raw = json.loads(blocked_json), json.loads(resources_json)
        if any(_json(value) != raw for value, raw in ((nodes, nodes_json), (edges, edges_json),
                (blocked_raw, blocked_json), (resources_raw, resources_json))):
            raise ValueError('reroute journal is not canonical')
        blocked, resources = _resources(blocked_raw), _resources(resources_raw)
        if len(set(path)) != len(path) or path.index(stopped) == 0:
            raise ValueError('unsupported parent route')
        from server.sim_reroute_candidate import _incoming
        if _incoming(progress._map, path[path.index(stopped) - 1], stopped) != incoming:
            raise ValueError('reroute parent direction changed')
        candidate = plan_field_route(progress._map, map_ref=progress._map_id, incoming_edge=incoming,
                                     goal=path[-1], blocked_resources=blocked)
        if (nodes != list(candidate.nodes) or edges != list(candidate.edge_ids) or len(nodes) < 2
                or len(nodes) > 256 or len(nodes) != len(set(nodes)) or nodes[0] != stopped
                or resources != set(plan_segment(progress._map, nodes).resources) or resources & blocked):
            raise ValueError('reroute plan or reservation changed')
        event = db.execute('SELECT payload,digest,task_id,robot_session,seq,state FROM sim_event_inbox '
                           'WHERE robot_id=? AND event_id=?', (robot, event_id)).fetchone()
        if event is None or event[1:] != (digest, task_id, session, seq, 'pending'):
            raise ValueError('reroute event changed')
        parsed, canonical, actual = _event(event[0].encode('ascii'))
        if (canonical != event[0] or actual != digest or parsed['event_id'] != event_id
                or parsed['order_id'] != task_id or parsed['robot_session'] != session
                or parsed['seq'] != seq or parsed['stopped_at'] != stopped or parsed['map_id'] != progress._map_id):
            raise ValueError('reroute event evidence is inconsistent')
        return resources - old_resources
    except (ValueError, TypeError, KeyError, IndexError, RecursionError, sqlite3.Error, UnicodeError) as exc:
        raise AllocationConflict('prepared reroute is inconsistent') from exc
