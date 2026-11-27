"""Durable intermediate stops for a wholly reserved virtual reroute.

A recorded stop is not a release: all parent and future reservations stay held.
This journal is only structural evidence, never physical authorization.
"""
import hashlib
import json
import sqlite3

from server.segments import plan_segment
from server.sim_reroute_activation import _canonical
from sim.event_inbox import _event
from sim.order_inbox import _decode


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_multi_steps ('
        'task_id TEXT NOT NULL REFERENCES sim_reroute_generations(task_id), '
        'transition_id TEXT NOT NULL,seq INTEGER NOT NULL,stop_index INTEGER NOT NULL, '
        'event_id TEXT NOT NULL,event_digest TEXT NOT NULL,next_payload BLOB NOT NULL,next_digest TEXT NOT NULL, '
        "state TEXT NOT NULL CHECK(state IN ('staged','uncertain')), "
        'PRIMARY KEY(task_id,seq), UNIQUE(task_id,event_id))')


def validate_steps(db, map_data, data):
    """Reconstruct exact ordered stops and next commands from immutable evidence."""
    try:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sim_reroute_multi_steps'").fetchone()
        if not table:
            return []
        rows = db.execute('SELECT transition_id,seq,stop_index,event_id,event_digest,next_payload,next_digest,state '
                          'FROM sim_reroute_multi_steps WHERE task_id=? ORDER BY seq', (data['task_id'],)).fetchall()
        if len(rows) > 255:
            raise ValueError('too many virtual reroute steps')
        if rows:
            marker = db.execute('SELECT 1 FROM sim_reroute_multiseg_routes WHERE task_id=?', (data['task_id'],)).fetchone()
            if marker is None:
                raise ValueError('whole-route reservation is required')
        nodes, offset, result = data['nodes'], 0, []
        for ordinal, row in enumerate(rows):
            transition, seq, index, event_id, event_digest, payload, payload_digest, state = row
            current = plan_segment(map_data, nodes[offset:])
            stop_index = nodes.index(current.released_until, offset + 1)
            if (stop_index >= len(nodes) - 1 or transition != data['transition_id']
                    or type(seq) is not int or seq != data['parent_seq'] + ordinal + 1
                    or type(index) is not int or index != stop_index or state not in ('staged', 'uncertain')
                    or (ordinal < len(rows) - 1 and state != 'uncertain')):
                raise ValueError('simulated segment sequence differs')
            next_segment = plan_segment(map_data, nodes[stop_index:])
            expected = {'order_id': data['task_id'], 'seq': seq + 1, 'map_id': data['map_id'],
                        'nodes': nodes, 'released_until': next_segment.released_until,
                        'action_at_end': data['action_at_end']}
            encoded = _canonical(expected)
            if (not isinstance(payload, bytes) or payload != encoded
                    or payload_digest != hashlib.sha256(encoded).hexdigest()):
                raise ValueError('simulated next segment order differs')
            order, _canonical_text, digest = _decode(payload, data['robot_id'], data['map_id'])
            if digest != payload_digest:
                raise ValueError('simulated next command digest differs')
            inbox = db.execute('SELECT payload,digest,task_id,robot_session,seq,state,result FROM sim_event_inbox '
                               'WHERE robot_id=? AND event_id=?', (data['robot_id'], event_id)).fetchone()
            outcome = _canonical({'status': 'reroute_segment_completed', 'task_id': data['task_id'],
                                  'next_seq': seq + 1}).decode('ascii')
            if not inbox or inbox[1:] != (event_digest, data['task_id'], data['robot_session'], seq, 'applied', outcome):
                raise ValueError('simulated segment stop event differs')
            event, canonical, actual = _event(inbox[0].encode('ascii'))
            if (canonical != inbox[0] or actual != event_digest or event['event_id'] != event_id
                    or event['order_id'] != data['task_id'] or event['robot_session'] != data['robot_session']
                    or event['seq'] != seq or event['map_id'] != data['map_id']
                    or event['stopped_at'] != nodes[stop_index] or event['cleared_resources'] != []):
                raise ValueError('simulated segment event content differs')
            result.append({'seq': seq, 'stop_index': stop_index, 'event_id': event_id,
                           'next_payload': payload, 'next_order': order, 'state': state})
            offset = stop_index
        return result
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, sqlite3.Error, RecursionError) as exc:
        raise ValueError('simulated reroute segment history is inconsistent') from exc


def stage_next(runtime, task_id, event_id, guard):
    """Record a fresh intermediate stop and exact next order atomically.

    No occupancy is released here; the full proposed route remains held.
    """
    from server.allocation_store import _verify_field_global
    from server.sim_reroute_activate_wire import _check_binding
    from server.sim_reroute_execute_wire import SimRerouteExecutionGuard, _execution
    if type(guard) is not SimRerouteExecutionGuard or guard.runtime is not runtime:
        raise ValueError('live virtual execution guard required')
    with runtime._runtime_read_operation():
        robot = _check_binding(runtime, task_id, guard.receipt_inbox, guard.robot_receiver)
        endpoint = guard._execution_receiver
        if endpoint is None:
            raise ValueError('virtual execution endpoint required')
        with guard.robot_receiver._lifecycle_lock, guard.robot_receiver._connection_fence, \
                guard.activation_receiver.endpoint._connection_fence, endpoint.endpoint._connection_fence, \
                robot._lock, runtime._lock:
            runtime._ready(); receiver = runtime.receiver
            with receiver._dispatch_lock, runtime.inbox._lock, guard._fresh_stop_proof.motion_fence(robot):
                with runtime.progress._transaction() as db:
                    _payload, data = _execution(db, guard, task_id, robot, state='uncertain')
                    if data['released_until'] == data['nodes'][-1]:
                        raise ValueError('multiple segment reservation required')
                    steps = validate_steps(db, runtime.progress._map, data)
                    state = robot.executor.snapshot(); order, receipt = state.get('order') or {}, state.get('receipt') or {}
                    seq = data['parent_seq'] + 1 + len(steps)
                    index = data['nodes'].index(order.get('released_until'))
                    if (index == len(data['nodes']) - 1 or state.get('status') != 'stopped'
                            or state.get('current_node') != data['nodes'][index]
                            or state.get('visit_index') != index or state.get('released_index') != index
                            or order.get('seq') != seq or receipt.get('seq') != seq
                            or receipt.get('state') != 'completed'
                            or (robot.robot_id, event_id) not in receiver._occupied_events):
                        raise ValueError('fresh intermediate virtual stop required')
                    row = db.execute('SELECT payload,digest,state FROM sim_event_inbox WHERE robot_id=? AND event_id=?',
                                     (robot.robot_id, event_id)).fetchone()
                    if not row or row[2] != 'pending':
                        raise ValueError('fresh intermediate event is not pending')
                    event, canonical, digest = _event(row[0].encode('ascii'))
                    if (canonical != row[0] or digest != row[1] or event['event_id'] != event_id
                            or event['order_id'] != task_id or event['robot_session'] != data['robot_session']
                            or event['seq'] != seq or event['map_id'] != data['map_id']
                            or event['stopped_at'] != data['nodes'][index] or event['cleared_resources'] != []):
                        raise ValueError('intermediate event does not match the reserved generation')
                    guard._fresh_stop_proof.issue(db, task_id, robot, event_id, final=False)
                    following = plan_segment(runtime.progress._map, data['nodes'][index:])
                    next_order = {'order_id': task_id, 'seq': seq + 1, 'map_id': data['map_id'],
                                  'nodes': data['nodes'], 'released_until': following.released_until,
                                  'action_at_end': data['action_at_end']}
                    next_payload = _canonical(next_order); next_digest = hashlib.sha256(next_payload).hexdigest()
                    initialize(db)
                    db.execute('INSERT INTO sim_reroute_multi_steps VALUES (?,?,?,?,?,?,?,?,?)',
                               (task_id, data['transition_id'], seq, index, event_id, digest, next_payload,
                                next_digest, 'staged'))
                    result = _canonical({'status': 'reroute_segment_completed', 'task_id': task_id,
                                         'next_seq': seq + 1}).decode('ascii')
                    if db.execute("UPDATE sim_event_inbox SET state='applied',result=? WHERE robot_id=? AND event_id=? AND state='pending'",
                                  (result, robot.robot_id, event_id)).rowcount != 1:
                        raise ValueError('intermediate event changed')
                    _verify_field_global(db, field_required=True)
                receiver._occupied_events.discard((robot.robot_id, event_id))
                return {'state': 'staged', 'task_id': task_id, 'next_seq': seq + 1}


def publish_next(runtime, task_id, guard):
    """Publish the exact staged next order once after committing uncertainty."""
    import threading
    import paho.mqtt.client as mqtt
    from server.allocation_store import _verify_field_global
    from server.sim_reroute_activate_wire import _check_binding
    from server.sim_reroute_execute_wire import SimRerouteExecutionGuard, _execution
    from sim.mqtt_order_robot import _verify_no_proxy
    if type(guard) is not SimRerouteExecutionGuard or guard.runtime is not runtime:
        raise ValueError('live virtual execution guard required')
    _verify_no_proxy()
    with runtime._runtime_read_operation():
        robot = _check_binding(runtime, task_id, guard.receipt_inbox, guard.robot_receiver)
        client = runtime._new_client()
        try:
            if getattr(client, '_proxy', None):
                raise RuntimeError('a client-specific MQTT proxy is not allowed')
            connected, failure = threading.Event(), []
            def on_connect(_client, _userdata, _flags, reason, _properties):
                if reason.is_failure: failure.append(True)
                connected.set()
            client.on_connect = on_connect
            if client.connect('127.0.0.1', runtime.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('isolated simulated publisher connect failed')
            if client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('isolated simulated publisher loop failed')
            if not connected.wait(runtime.connect_timeout) or failure or not client.is_connected():
                raise RuntimeError('simulated publisher connection timed out')
            endpoint = guard._execution_receiver
            if endpoint is None:
                raise ValueError('virtual execution endpoint required')
            with guard.robot_receiver._lifecycle_lock, guard.robot_receiver._connection_fence, \
                    guard.activation_receiver.endpoint._connection_fence, endpoint.endpoint._connection_fence, \
                    robot._lock, runtime._lock:
                runtime._ready(); receiver = runtime.receiver
                with receiver._dispatch_lock, runtime.inbox._lock, guard._fresh_stop_proof.motion_fence(robot):
                    with runtime.progress._transaction() as db:
                        _execution_payload, data = _execution(db, guard, task_id, robot, state='uncertain')
                        steps = validate_steps(db, runtime.progress._map, data)
                        if not steps or steps[-1]['state'] != 'staged':
                            raise ValueError('no staged virtual segment to publish')
                        item = steps[-1]
                        if db.execute("UPDATE sim_reroute_multi_steps SET state='uncertain' WHERE task_id=? AND seq=? AND state='staged'",
                                      (task_id, item['seq'])).rowcount != 1:
                            raise ValueError('virtual segment was already attempted')
                        _verify_field_global(db, field_required=True)
                    if not robot.ready or receiver is not runtime.receiver or not receiver.ready:
                        raise RuntimeError('virtual connection changed before next command')
                    info = client.publish('agv/{}/order'.format(robot.robot_id), item['next_payload'], qos=1, retain=False)
                    if info.rc != mqtt.MQTT_ERR_SUCCESS:
                        raise RuntimeError('virtual next command enqueue failed')
            info.wait_for_publish(timeout=runtime.ack_timeout)
            if not info.is_published():
                raise RuntimeError('virtual next command broker acknowledgement is unknown')
            return {'state': 'uncertain', 'task_id': task_id, 'next_seq': item['seq'] + 1}
        finally:
            runtime._close_client(client)
