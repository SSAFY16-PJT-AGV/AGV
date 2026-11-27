"""One-segment virtual reroute clearance; never authorizes physical movement."""
import hashlib
import json
import re
import sqlite3

from server.allocation_store import AllocationConflict, _verify_field_global
from server.segments import plan_segment
from server.segment_progress import _resources
from server.sim_reroute_activation import _canonical
from server.sim_reroute_activate_wire import _check_binding
from server.sim_reroute_execute_wire import SimRerouteExecutionGuard, _execution
from sim.event_inbox import _event
from sim.order_inbox import _decode


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def finished_resources(db, progress, task, robot, path, old_resources):
    """Validate durable completion lineage without granting a live permit."""
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sim_reroute_finishes'").fetchone()
    row = db.execute('SELECT transition_id,robot_id,event_id,event_digest FROM sim_reroute_finishes WHERE task_id=?',
                     (task,)).fetchone() if exists else None
    if row is None:
        return None
    try:
        transition, saved_robot, event_id, event_digest = row
        gen = db.execute('SELECT generation,transition_id,robot_id,robot_session,parent_seq,old_payload,stopped_at,'
            'incoming_edge,nodes_json,edges_json,blocked_json,new_resources_json,state FROM sim_reroute_generations '
            'WHERE task_id=?', (task,)).fetchone()
        generation, trans, rid, session, seq, old_payload, stopped, incoming, nodes_raw, edges_raw, blocked_raw, resources_raw, status = gen
        nodes = json.loads(nodes_raw)
        new_resources = _resources(json.loads(resources_raw))
        if transition != trans or saved_robot != robot or rid != robot or status != 'accepted' or nodes[-1] != path[-1]:
            raise ValueError('finish generation differs')
        parent, _, parent_digest = _decode(old_payload.encode('utf-8'), robot, progress._map_id)
        proposal = _canonical({'type': 'sim.reroute.prepare.v1', 'data': {
            'transition_id': trans, 'generation': generation, 'robot_id': robot, 'robot_session': session,
            'task_id': task, 'parent_seq': seq, 'parent_digest': parent_digest, 'map_id': progress._map_id,
            'stopped_at': stopped, 'incoming_edge': incoming, 'nodes': nodes, 'edge_ids': json.loads(edges_raw),
            'blocked_resources': json.loads(blocked_raw), 'released_until': plan_segment(progress._map, nodes).released_until,
            'action_at_end': parent['action_at_end']}})
        data = json.loads(proposal)['data']
        activation = _canonical({'type': 'sim.reroute.activate.v1', 'data': dict(data, proposal_digest=_sha(proposal))})
        expected_activation = (trans, activation, _sha(activation), 'uncertain')
        if db.execute('SELECT transition_id,payload,digest,state FROM sim_reroute_activation_attempts WHERE task_id=?',
                      (task,)).fetchone() != expected_activation:
            raise ValueError('finish activation attempt differs')
        receipt = db.execute('SELECT robot_id,payload,digest,state FROM sim_reroute_activation_receipts WHERE task_id=?',
                             (task,)).fetchone()
        if not receipt or receipt[0] != robot or not isinstance(receipt[1], bytes) or _sha(receipt[1]) != receipt[2] or receipt[3] != 'recorded_not_executed':
            raise ValueError('finish activation receipt missing')
        signed = json.loads(receipt[1].decode('ascii'))
        expected_receipt = {'robot_id': robot, 'robot_session': session, 'task_id': task, 'transition_id': trans,
                            'generation': generation, 'parent_seq': seq, 'digest': _sha(activation),
                            'proposal_digest': _sha(proposal), 'state': 'recorded_not_executed'}
        if (not isinstance(signed, dict) or set(signed) != {'type', 'data', 'mac'}
                or signed['type'] != 'sim.reroute.activation-receipt.v1' or _canonical(signed['data']) != _canonical(expected_receipt)
                or not isinstance(signed['mac'], str) or not re.fullmatch('[0-9a-f]{64}', signed['mac'])):
            raise ValueError('finish activation receipt differs')
        execution = _canonical({'type': 'sim.reroute.execute.v1', 'data':
                                dict(json.loads(activation)['data'], activation_digest=_sha(activation))})
        if db.execute('SELECT transition_id,payload,digest,state FROM sim_reroute_execution_attempts WHERE task_id=?',
                      (task,)).fetchone() != (trans, execution, _sha(execution), 'finished'):
            raise ValueError('finish execution attempt differs')
        event_row = db.execute('SELECT payload,digest,task_id,robot_session,seq,state,result FROM sim_event_inbox '
                               'WHERE robot_id=? AND event_id=?', (robot, event_id)).fetchone()
        expected_result = _canonical({'status': 'reroute_reached_goal', 'task_id': task}).decode('ascii')
        if not event_row or event_row[1:] != (event_digest, task, session, seq + 1, 'applied', expected_result):
            raise ValueError('finish event metadata differs')
        event, encoded, digest = _event(event_row[0].encode('ascii'))
        keep = {('node', path[-1])}
        if (encoded != event_row[0] or digest != event_digest or event['event_id'] != event_id
                or event['order_id'] != task or event['robot_session'] != session or event['seq'] != seq + 1
                or event['map_id'] != progress._map_id or event['stopped_at'] != path[-1]
                or _resources(event['cleared_resources']) != (old_resources | new_resources) - keep):
            raise ValueError('finish clearance differs')
        return keep
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError, sqlite3.Error) as exc:
        raise AllocationConflict('simulated reroute finish is inconsistent') from exc


def finish_reroute(runtime, task_id, event_id, guard):
    """Release jointly held simulated segments only on a new goal-stop event."""
    if type(guard) is not SimRerouteExecutionGuard or guard.runtime is not runtime:
        raise ValueError('live simulated execution guard required')
    with runtime._runtime_read_operation():
        robot = _check_binding(runtime, task_id, guard.receipt_inbox, guard.robot_receiver)
        execution_receiver = guard._execution_receiver
        with guard.robot_receiver._lifecycle_lock, guard.robot_receiver._connection_fence, \
                guard.activation_receiver.endpoint._connection_fence, execution_receiver.endpoint._connection_fence, \
                robot._lock, runtime._lock:
            runtime._ready()
            receiver = runtime.receiver
            with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                with runtime.progress._transaction() as db:
                    payload, data = _execution(db, guard, task_id, robot, state='uncertain')
                    state = robot.executor.snapshot()
                    order, receipt = state.get('order') or {}, state.get('receipt') or {}
                    if (state.get('status') != 'stopped' or state.get('current_node') != data['nodes'][-1]
                            or state.get('visit_index') != state.get('released_index')
                            or receipt.get('state') != 'completed' or receipt.get('seq') != data['parent_seq'] + 1
                            or order.get('nodes') != data['nodes'] or (robot.robot_id, event_id) not in receiver._occupied_events):
                        raise ValueError('new virtual goal stop is not confirmed')
                    row = db.execute('SELECT payload,digest,state FROM sim_event_inbox WHERE robot_id=? AND event_id=?',
                                     (robot.robot_id, event_id)).fetchone()
                    if not row or row[2] != 'pending':
                        raise ValueError('new virtual clearance event is not pending')
                    event, encoded, digest = _event(row[0].encode('ascii'))
                    old_id = db.execute('SELECT event_id FROM sim_reroute_generations WHERE task_id=?', (task_id,)).fetchone()[0]
                    held = runtime.progress._occupancy(db, task_id)
                    keep = {('node', data['nodes'][-1])}
                    if (event_id == old_id or digest != row[1] or encoded != row[0]
                            or event['event_id'] != event_id or event['order_id'] != task_id
                            or event['robot_session'] != data['robot_session'] or event['seq'] != data['parent_seq'] + 1
                            or event['map_id'] != data['map_id'] or event['stopped_at'] != data['nodes'][-1]
                            or _resources(event['cleared_resources']) != held - keep or not keep <= held):
                        raise ValueError('new virtual clearance does not match the held generation')
                    db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_finishes ('
                               'task_id TEXT PRIMARY KEY NOT NULL REFERENCES sim_reroute_generations(task_id), '
                               'transition_id TEXT UNIQUE NOT NULL,robot_id TEXT NOT NULL,event_id TEXT NOT NULL,'
                               'event_digest TEXT NOT NULL)')
                    db.execute('INSERT INTO sim_reroute_finishes VALUES(?,?,?,?,?)',
                               (task_id, data['transition_id'], robot.robot_id, event_id, digest))
                    for kind, resource in held - keep:
                        if db.execute('DELETE FROM allocation_occupancy WHERE task_id=? AND kind=? AND resource_id=?',
                                      (task_id, kind, resource)).rowcount != 1:
                            raise ValueError('reserved resource changed')
                    if db.execute("UPDATE sim_reroute_execution_attempts SET state='finished' WHERE task_id=? AND state='uncertain'", (task_id,)).rowcount != 1:
                        raise ValueError('execution state changed')
                    result = _canonical({'status': 'reroute_reached_goal', 'task_id': task_id}).decode('ascii')
                    if db.execute("UPDATE sim_event_inbox SET state='applied',result=? WHERE robot_id=? AND event_id=? AND state='pending'",
                                  (result, robot.robot_id, event_id)).rowcount != 1:
                        raise ValueError('clearance event state changed')
                    superseded = _canonical({'status': 'superseded_by_reroute',
                                             'transition_id': data['transition_id']}).decode('ascii')
                    if db.execute("UPDATE sim_event_inbox SET state='rejected',result=? "
                                  "WHERE robot_id=? AND event_id=? AND state='pending'",
                                  (superseded, robot.robot_id, old_id)).rowcount != 1:
                        raise ValueError('parent event state changed')
                    _verify_field_global(db, field_required=True)
                receiver._occupied_events.discard((robot.robot_id, event_id))
                receiver._occupied_events.discard((robot.robot_id, old_id))
                return {'state': 'reached_goal_held', 'task_id': task_id, 'scope': 'isolated_simulation'}
