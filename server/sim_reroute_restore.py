"""Restore a finished virtual detour only from consistent durable histories."""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from server.segments import plan_segment
from server.sim_reroute_finish import finished_resources
from server.sim_reroute_multi_progress import validate_steps
from sim.order_inbox import _decode, _validated_history


def verify_saved_reroute_robot(db, map_data, inbox_path, task, robot, session):
    """Cross-check one finished server generation with that robot's inbox."""
    generation = db.execute('SELECT robot_id,robot_session,parent_seq,old_payload,transition_id '
                            'FROM sim_reroute_generations WHERE task_id=?', (task,)).fetchone()
    execution = db.execute('SELECT payload FROM sim_reroute_execution_attempts WHERE task_id=?', (task,)).fetchone()
    if generation is None or generation[:2] != (robot, session) or execution is None:
        raise ValueError('saved reroute robot identity differs')
    parent_seq, old_payload, transition = generation[2:]
    data = json.loads(execution[0].decode('ascii'))['data']
    if (data['task_id'] != task or data['robot_id'] != robot or data['robot_session'] != session
            or data['parent_seq'] != parent_seq or data['transition_id'] != transition):
        raise ValueError('saved reroute execution identity differs')
    parent, parent_encoded, parent_digest = _decode(old_payload.encode('utf-8'), robot, data['map_id'])
    steps = validate_steps(db, map_data, data)
    first = {'order_id': task, 'seq': parent_seq + 1, 'map_id': data['map_id'], 'nodes': data['nodes'],
             'released_until': plan_segment(map_data, data['nodes']).released_until,
             'action_at_end': data['action_at_end']}
    orders = [first] + [step['next_order'] for step in steps]
    uri = Path(inbox_path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as inbox:
        inbox.execute('BEGIN')
        identity = inbox.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone()
        history, _ = _validated_history(inbox, robot, data['map_id'], task)
        rows = [row for row in history if row[0] > parent_seq]
        robot_execution = inbox.execute('SELECT transition_id,order_id,parent_seq,activation_digest,digest,payload,state '
                                        'FROM sim_reroute_executions WHERE transition_id=?', (transition,)).fetchone()
        robot_parent = inbox.execute('SELECT digest,payload,state FROM sim_inbox_orders WHERE order_id=? AND seq=?',
                                     (task, parent_seq)).fetchone()
    if (identity != (robot, data['map_id']) or len(rows) != len(orders)
            or robot_execution != (transition, task, parent_seq, data['activation_digest'],
                                   hashlib.sha256(execution[0]).hexdigest(), execution[0].decode('ascii'),
                                   'recorded_not_executed')
            or robot_parent != (parent_digest, parent_encoded, 'completed')
            or parent_digest != data['parent_digest'] or parent['action_at_end'] != data['action_at_end']):
        raise ValueError('server and robot reroute history differs')
    for row, order in zip(rows, orders):
        encoded = json.dumps(order, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        _, canonical, digest = _decode(encoded.encode('ascii'), robot, data['map_id'])
        if row != (order['seq'], canonical, digest, 'completed'):
            raise ValueError('rerouted robot history command differs')
    return True


def restore_rerouted_terminal(runtime, executor, db, task, session, event_id, robot, route, stopped, parent):
    """Rebuild a stopped simulator, without authorizing movement or world state.

    Receipt MACs are not reauthenticated after reboot. This checks consistency
    between saved virtual records, not malicious replacement of all databases.
    """
    if runtime._team_shelf_recovery is None:
        raise ValueError('bound team shelf recovery required for a terminal reroute')
    runtime._team_shelf_recovery.validate(runtime.store, db)
    goal = route[-1]
    old = set(runtime.progress._segment_for_task(db, task, route, stopped).resources)
    if finished_resources(db, runtime.progress, task, robot, route, old) != {('node', goal)}:
        raise ValueError('completed reroute is unavailable')
    finish = db.execute('SELECT transition_id,robot_id,event_id FROM sim_reroute_finishes WHERE task_id=?',
                        (task,)).fetchone()
    execution = db.execute('SELECT payload FROM sim_reroute_execution_attempts WHERE task_id=?', (task,)).fetchone()
    if finish is None or finish[1:] != (robot, event_id) or execution is None:
        raise ValueError('completed reroute identity differs')
    data = json.loads(execution[0].decode('ascii'))['data']
    if (data['transition_id'] != finish[0] or data['robot_session'] != session
            or data['task_id'] != task or data['robot_id'] != robot
            or data['map_id'] != runtime.progress._map_id or data['nodes'][-1] != goal
            or data['action_at_end'] != parent['action_at_end']):
        raise ValueError('completed reroute command differs')
    steps = validate_steps(db, runtime.progress._map, data)
    initial = {'order_id': task, 'seq': data['parent_seq'] + 1, 'map_id': data['map_id'],
               'nodes': data['nodes'], 'released_until': plan_segment(runtime.progress._map, data['nodes']).released_until,
               'action_at_end': data['action_at_end']}
    orders = [initial] + [step['next_order'] for step in steps]
    uri = Path(executor._inbox.path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as inbox:
        identity = inbox.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone()
        rows = inbox.execute('SELECT seq,digest,payload,state FROM sim_inbox_orders '
                             'WHERE order_id=? AND seq>? ORDER BY seq', (task, data['parent_seq'])).fetchall()
        latest = inbox.execute('SELECT order_id,seq FROM sim_inbox_orders ORDER BY rowid DESC LIMIT 1').fetchone()
        robot_execution = inbox.execute('SELECT transition_id,order_id,parent_seq,activation_digest,digest,payload,state '
                                        'FROM sim_reroute_executions WHERE transition_id=?', (finish[0],)).fetchone()
        robot_parent = inbox.execute('SELECT digest,payload,state FROM sim_inbox_orders WHERE order_id=? AND seq=?',
                                     (task, data['parent_seq'])).fetchone()
    encoded_execution = execution[0].decode('ascii')
    parent_payload = json.dumps(parent, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    _, parent_encoded, parent_digest = _decode(parent_payload.encode('ascii'), robot, data['map_id'])
    if (robot_execution != (finish[0], task, data['parent_seq'], data['activation_digest'],
                            hashlib.sha256(execution[0]).hexdigest(), encoded_execution, 'recorded_not_executed')
            or robot_parent != (parent_digest, parent_encoded, 'completed')
            or parent_digest != data['parent_digest']):
        raise ValueError('server and robot reroute execution or parent differ')
    if identity != (robot, data['map_id']) or len(rows) != len(orders):
        raise ValueError('rerouted robot inbox identity or length differs')
    for row, order in zip(rows, orders):
        canonical = json.dumps(order, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        parsed, encoded, digest = _decode(canonical.encode('ascii'), robot, data['map_id'])
        if parsed != order or row != (order['seq'], digest, encoded, 'completed'):
            raise ValueError('rerouted robot inbox command differs')
    final = orders[-1]
    if latest != (task, final['seq']) or final['released_until'] != goal:
        raise ValueError('rerouted final inbox command is not current')
    digest = rows[-1][1]
    evidence = {'task_id': task, 'session': session, 'robot_id': robot, 'seq': final['seq'],
                'stopped_at': goal, 'event_id': event_id, 'map_id': data['map_id'], 'digest': digest}
    executor._restore_completed(final, digest, evidence, field_entry=data['incoming_edge'],
                                field_history=orders, reroute_transition=data['transition_id'],
                                reroute_edges=data['edge_ids'])
    return evidence
