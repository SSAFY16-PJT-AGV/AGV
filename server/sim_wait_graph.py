"""Bounded read-only wait-for observations for the existing simulator.

No simulated evidence here authenticates hardware or grants motion. Only
currently connected, receiver-classified SegmentOccupied events are examined;
initial allocation waits and unclassified inbox records remain unknown.
"""
from server.allocation_store import _verify_field_global
from server.route_occurrence import plan_occurrence_segment
from server.segment_progress import _resources
from server.segments import plan_segment
from server.sim_handoff_store import SimHandoffStore
from sim.event_inbox import _event

_LIMIT = 32
_RESOURCE_LIMIT = 512
_BLOCKER_LIMIT = 256
_IDENTIFIER_BYTES = 256


def _unknown(reason):
    return {'status': 'unknown', 'reason': reason, 'scope': 'isolated_simulation',
            'control_effect': 'none', 'initial_allocation_waits': 'not_observed',
            'waiting': [], 'cycle_candidates': []}


def _cycles(waiting):
    """Strongly connected task groups; not evidence of permanent deadlock."""
    tasks = {item['task_id'] for item in waiting}
    graph = {task: set() for task in tasks}
    for item in waiting:
        graph[item['task_id']].update(b['owner_task_id'] for b in item['blockers']
                                      if b['owner_task_id'] in tasks)
    index, stack, positions, low, on_stack, result = 0, [], {}, {}, set(), []

    def visit(node):
        nonlocal index
        positions[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for other in sorted(graph[node]):
            if other not in positions:
                visit(other)
                low[node] = min(low[node], low[other])
            elif other in on_stack:
                low[node] = min(low[node], positions[other])
        if low[node] == positions[node]:
            group = []
            while True:
                current = stack.pop()
                on_stack.remove(current)
                group.append(current)
                if current == node:
                    break
            if len(group) > 1:
                result.append(sorted(group))

    for node in sorted(graph):
        if node not in positions:
            visit(node)
    return sorted(result)


def _validated_wait(runtime, db, robot, event_id, payload, digest, task_id, session, seq):
    if any(not isinstance(value, str) or len(value.encode('utf-8')) > _IDENTIFIER_BYTES
           for value in (robot, event_id, task_id, session)):
        raise ValueError('observation identifier exceeds limit')
    event, canonical, actual_digest = _event(payload.encode('ascii'))
    if (canonical != payload or actual_digest != digest or event['event_id'] != event_id
            or event['order_id'] != task_id or event['robot_session'] != session
            or event['seq'] != seq or runtime._tasks.get(task_id) != (robot, session)):
        raise ValueError('inconsistent pending event')
    progress = runtime.progress
    progress._authorize(db, task_id, session, seq, event['stopped_at'])
    owner, path, stopped = progress._job(db, task_id, robot)
    current_seq, _payload, state, _order = progress._latest(db, task_id, owner, path, stopped)
    if (state not in ('acked', 'uncertain') or current_seq != seq or event['map_id'] != progress._map_id
            or event['stopped_at'] != stopped):
        raise ValueError('pending event does not match current command')
    current = progress._segment_for_task(db, task_id, path, stopped)
    current_resources = set(current.resources)
    if progress._occupancy(db, task_id) != current_resources:
        raise ValueError('current occupancy does not match segment')
    repeated = len(set(path)) != len(path)
    end = current.end_index if repeated else path.index(stopped)
    if end >= len(path) - 1:
        raise ValueError('finished segment cannot wait for a next segment')
    next_segment = (plan_occurrence_segment(progress._map, path, end) if repeated
                    else plan_segment(progress._map, path[end:]))
    needed = set(next_segment.resources)
    if len(needed) > _RESOURCE_LIMIT:
        raise ValueError('observation resource count exceeds limit')
    if _resources(event['cleared_resources']) != current_resources - needed:
        raise ValueError('clearance differs from segment transition')
    blockers = []
    idle_enabled = type(runtime.store) is SimHandoffStore and runtime.store._idle_enabled
    for kind, resource in sorted(needed - current_resources):
        if not isinstance(resource, str) or len(resource.encode('utf-8')) > _IDENTIFIER_BYTES:
            raise ValueError('observation resource identifier exceeds limit')
        row = db.execute('SELECT o.task_id,j.robot_id,t.status FROM allocation_occupancy o '
                         'JOIN allocation_jobs j ON j.task_id=o.task_id '
                         'JOIN tasks t ON t.task_id=j.task_id '
                         'WHERE o.kind=? AND o.resource_id=?', (kind, resource)).fetchone()
        if row is not None and row[0] != task_id:
            if (runtime._tasks.get(row[0], (None,))[0] != row[1]
                    or row[2] not in ('ASSIGNED', 'DONE')
                    or any(not isinstance(x, str) or len(x.encode('utf-8')) > _IDENTIFIER_BYTES
                           for x in row[:2])):
                raise ValueError('resource owner is not in the simulated mapping')
            blockers.append({'resource': [kind, resource], 'owner_task_id': row[0], 'owner_robot_id': row[1]})
        if idle_enabled and kind == 'node':
            idle = db.execute('SELECT robot_id FROM sim_idle_robots WHERE node=?', (resource,)).fetchone()
            if idle is not None:
                if (row is not None or idle[0] not in runtime._sessions
                        or not isinstance(idle[0], str)
                        or len(idle[0].encode('utf-8')) > _IDENTIFIER_BYTES):
                    raise ValueError('idle resource owner is not mapped')
                blockers.append({'resource': [kind, resource], 'owner_task_id': None, 'owner_robot_id': idle[0]})
        if len(blockers) > _BLOCKER_LIMIT:
            raise ValueError('observation blocker count exceeds limit')
    if not blockers:
        raise ValueError('the previously observed conflict is no longer present')
    return {'task_id': task_id, 'robot_id': robot, 'event_id': event_id,
            'stopped_at': stopped, 'blockers': blockers}


def observe_wait_graph(runtime):
    """Sample classified waits without advancing events or touching occupancy."""
    with runtime._runtime_read_operation():
        with runtime._lock:
            receiver = runtime.receiver
            if (receiver is None or runtime._reconnecting or runtime._reconnect_cleanup_failed
                    or not receiver.ready):
                return _unknown('receiver_unavailable')
            with receiver._dispatch_lock:
                if receiver is not runtime.receiver or not receiver.ready:
                    return _unknown('receiver_unavailable')
                with runtime.inbox._lock:
                    try:
                        with runtime.progress._transaction() as db:
                            _verify_field_global(db, field_required=runtime.progress._field_required)
                            rows = db.execute('SELECT robot_id,event_id,payload,digest,task_id,robot_session,seq '
                                              "FROM sim_event_inbox WHERE state='pending' "
                                              'ORDER BY robot_id,event_id LIMIT ?', (_LIMIT + 1,)).fetchall()
                            if len(rows) > _LIMIT or len(receiver._occupied_events) > _LIMIT:
                                return _unknown('observation_limit')
                            pending = {(row[0], row[1]) for row in rows}
                            if pending != set(receiver._occupied_events):
                                return _unknown('unclassified_pending')
                            waiting = [_validated_wait(runtime, db, *row) for row in rows]
                    except Exception:
                        return _unknown('evidence_unavailable')
            cycles = _cycles(waiting)
            return {'status': 'cycle_candidate' if cycles else 'observed', 'reason': None,
                    'scope': 'isolated_simulation', 'control_effect': 'none',
                    'initial_allocation_waits': 'not_observed', 'waiting': waiting,
                    'cycle_candidates': cycles}
