"""Refresh a still-valid, independently observed waiting virtual stop.

This is restricted to the already-bound in-process simulator and does not
prove physical location, safety, or clearance. Failure keeps the event and
reservations but revokes any uncertain replacement challenge.
"""
from contextlib import closing, contextmanager
import os
import math
from pathlib import Path
import sqlite3

from server.allocation_store import _verify_field_global
from server.recovery import _snapshot
from server.sim_reroute_candidate import _owned_simulator
from server.sim_reroute_transition import is_prepared
from server.sim_wait_graph import _validated_wait
from server.field_map_planner import plan_field_route
from sim.order_inbox import _decode, _validated_history


def _observe_time(runtime, gate, task_id, current):
    """Permanently block this boot's wait proof if its clock reverses."""
    clocks = getattr(runtime, '_sim_wait_refresh_clocks', None)
    if clocks is None:
        clocks = runtime._sim_wait_refresh_clocks = {}
    previous = clocks.get(task_id)
    if type(current) not in (int, float) or not math.isfinite(current):
        clocks[task_id] = (gate, gate._epoch, None, True)
        gate._block(task_id)
        raise ValueError('virtual waiting proof clock is invalid')
    current = float(current)
    if previous is not None and (previous[0] is not gate or previous[1] != gate._epoch
                                  or previous[3] or current < previous[2]):
        clocks[task_id] = (gate, gate._epoch, None, True)
        gate._block(task_id)
        raise ValueError('virtual waiting proof clock moved backwards')
    clocks[task_id] = (gate, gate._epoch, current, False)
    return current


def _now(runtime, gate, task_id):
    try:
        return _observe_time(runtime, gate, task_id, gate._clock())
    except Exception:
        clocks = getattr(runtime, '_sim_wait_refresh_clocks', None)
        if clocks is None:
            clocks = runtime._sim_wait_refresh_clocks = {}
        clocks[task_id] = (gate, gate._epoch, None, True)
        gate._block(task_id)
        raise


@contextmanager
def _tracked_gate_clock(runtime, gate, task_id):
    """Track even private begin/complete observations under the gate lock."""
    original = gate._clock
    def tracked():
        try:
            return _observe_time(runtime, gate, task_id, original())
        except Exception:
            clocks = getattr(runtime, '_sim_wait_refresh_clocks', None)
            if clocks is None:
                clocks = runtime._sim_wait_refresh_clocks = {}
            clocks[task_id] = (gate, gate._epoch, None, True)
            gate._block(task_id)
            raise
    gate._clock = tracked
    try:
        yield
    finally:
        gate._clock = original


def _check_wait_clock(runtime, gate, task_id, session):
    current = _now(runtime, gate, task_id)
    with closing(gate._connect()) as db:
        if is_prepared(db, task_id):
            raise ValueError('a reroute generation cannot refresh its parent challenge')
        row = db.execute('SELECT epoch,robot_session,expires,status FROM recovery_challenges '
                         'WHERE task_id=?', (task_id,)).fetchone()
    if (row is None or row[0] != gate._epoch or row[1] != session or row[3] != 'verified'
            or current < row[2] - gate._ttl or current >= row[2]):
        gate._block(task_id)
        raise ValueError('current virtual waiting proof is unavailable')
    with gate._state_lock:
        if task_id in gate._blocked:
            raise ValueError('virtual waiting proof is blocked')
    return {'state': 'valid', 'expires': row[2]}


def _check_live_stop(runtime, db, robot, robot_id, task_id, data):
    progress = runtime.progress
    state = robot.executor.snapshot(); order, receipt = state.get('order') or {}, state.get('receipt') or {}
    if (state.get('status') != 'stopped' or state.get('current_node') != data['stopped_at']
            or receipt.get('state') != 'completed' or receipt.get('order_id') != task_id
            or receipt.get('seq') != data['seq'] or receipt.get('digest') != data['order_digest']
            or order.get('order_id') != task_id or order.get('seq') != data['seq']
            or order.get('released_until') != data['stopped_at']
            or state.get('visit_index') != state.get('released_index')):
        raise ValueError('live virtual robot does not match the waiting proof')
    stopped_direction = robot.executor.field_stop_direction()
    inbox = robot.executor._inbox
    uri = Path(inbox.path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as robot_db:
        identity = robot_db.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone()
        history, indices = _validated_history(robot_db, robot_id, data['map_id'], task_id)
    parsed, canonical, digest = _decode(_canonical_order(order), robot_id, data['map_id'])
    if (identity != (robot_id, data['map_id']) or not history
            or history[-1] != (data['seq'], canonical, digest, 'completed')
            or not indices or state.get('visit_index') != indices[-1]
            or state.get('released_index') != indices[-1]
            or digest != data['order_digest'] or parsed != order):
        raise ValueError('durable virtual robot order differs')
    provenance = db.execute('SELECT incoming_edge FROM field_route_provenance WHERE task_id=?',
                            (task_id,)).fetchone()
    if provenance is None:
        raise ValueError('virtual field route provenance is unavailable')
    route = plan_field_route(progress._map, map_ref=data['map_id'],
                             incoming_edge=provenance[0], goal=order['nodes'][-1])
    index = indices[-1]
    if (tuple(order['nodes']) != route.nodes or not 1 <= index < len(route.nodes)
            or stopped_direction != (route.nodes[index], route.edge_ids[index - 1])):
        raise ValueError('virtual stopped direction differs from the route history')
    return state


def retry_wait(runtime, task_id, robot, event_id, *, simulation_enabled=False):
    """Track the live proof clock through the actual pending-event retry."""
    if simulation_enabled is not True:
        raise ValueError('explicit isolated simulation is required')
    if not isinstance(event_id, str) or not event_id:
        raise ValueError('explicit event identity required')
    with runtime._runtime_read_operation():
        pair = runtime._tasks.get(task_id)
        if (not isinstance(pair, tuple) or len(pair) != 2 or runtime._reroute_simulators is None
                or runtime._reroute_simulators.get(pair[0]) is not robot
                or not _owned_simulator(runtime, pair[0], robot)):
            raise ValueError('the exact bound virtual robot is required')
        with robot._lock, runtime._lock:
            runtime._ready(); gate, receiver = runtime.gate, runtime.receiver
            if (not robot.ready or robot._event_uncertain or robot._reroute_next_inflight
                    or not robot._queue.empty() or receiver is None or not receiver.ready
                    or not receiver.client.is_connected()):
                raise ValueError('live virtual waiting connections required')
            client, receiver_client = robot._client, receiver.client
            with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence, \
                    robot.executor._lock, robot.executor._inbox._lock, gate._operation_lock, \
                    _tracked_gate_clock(runtime, gate, task_id):
                if (runtime.receiver is not receiver or receiver.client is not receiver_client
                        or not receiver.ready or not receiver_client.is_connected()
                        or robot._client is not client or not robot.ready
                        or robot._event_uncertain or robot._reroute_next_inflight
                        or not robot._queue.empty()):
                    raise ValueError('live virtual waiting connections required')
                _check_wait_clock(runtime, gate, task_id, pair[1])
                if (pair[0], event_id) in receiver._occupied_events:
                    with runtime.progress._transaction() as db:
                        _verify_field_global(db, field_required=runtime.progress._field_required)
                        data = _snapshot(db, task_id, field_required=runtime.progress._field_required)
                        record = db.execute('SELECT task_id,robot_session,state FROM sim_event_inbox '
                                            'WHERE robot_id=? AND event_id=?', (pair[0], event_id)).fetchone()
                        if record != (task_id, pair[1], 'pending'):
                            raise ValueError('pending virtual event is unavailable')
                        _check_live_stop(runtime, db, robot, pair[0], task_id, data)
                return receiver.retry_occupied_event(pair[0], event_id)



def refresh_wait(runtime, task_id, robot, event_id, *, simulation_enabled=False):
    """Issue a new challenge only while the current classified proof is valid."""
    if simulation_enabled is not True:
        raise ValueError('explicit isolated simulation is required')
    if not isinstance(task_id, str) or not task_id or not isinstance(event_id, str) or not event_id:
        raise ValueError('explicit task and event identity required')
    with runtime._runtime_read_operation():
        pair = runtime._tasks.get(task_id)
        if (not isinstance(pair, tuple) or len(pair) != 2 or runtime._reroute_simulators is None
                or runtime._reroute_simulators.get(pair[0]) is not robot
                or not _owned_simulator(runtime, pair[0], robot)):
            raise ValueError('the exact bound virtual robot is required')
        robot_id, session = pair
        with robot._lock, runtime._lock:
            runtime._ready(); receiver, gate, progress = runtime.receiver, runtime.gate, runtime.progress
            if (receiver is None or not receiver.ready or not receiver.client.is_connected()
                    or not robot.ready or robot._event_uncertain or robot._reroute_next_inflight
                    or not robot._queue.empty()):
                raise ValueError('live virtual waiting connections required')
            client, receiver_client = robot._client, receiver.client
            with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence, \
                    robot.executor._lock, robot.executor._inbox._lock:
                if (receiver is not runtime.receiver or receiver.client is not receiver_client or not receiver.ready
                        or not receiver_client.is_connected()
                        or not robot.ready or robot._client is not client or robot._event_uncertain
                        or robot._reroute_next_inflight or not robot._queue.empty()
                        or (robot_id, event_id) not in receiver._occupied_events):
                    raise ValueError('classified live virtual wait required')
                with gate._operation_lock, _tracked_gate_clock(runtime, gate, task_id):
                    # No other local gate begin/invalidation may fit between
                    # the original proof check and the final verification.
                    with progress._transaction() as db:
                        _verify_field_global(db, field_required=progress._field_required)
                        if is_prepared(db, task_id):
                            raise ValueError('a reroute generation cannot refresh its parent challenge')
                        row = db.execute('SELECT payload,digest,task_id,robot_session,seq,state FROM sim_event_inbox '
                                         'WHERE robot_id=? AND event_id=?', (robot_id, event_id)).fetchone()
                        if row is None or row[2:] != (task_id, session, row[4], 'pending'):
                            raise ValueError('pending virtual event is unavailable')
                        now = _now(runtime, gate, task_id)
                        wait = _validated_wait(runtime, db, robot_id, event_id, row[0], row[1], task_id, session, row[4])
                        data = _snapshot(db, task_id, field_required=progress._field_required)
                        challenge = db.execute('SELECT epoch,challenge,robot_session,seq,stopped_at,expires,status,snapshot_digest '
                                               'FROM recovery_challenges WHERE task_id=?', (task_id,)).fetchone()
                        if challenge is not None and now < challenge[5] - gate._ttl:
                            gate._block(task_id)
                            raise ValueError('virtual waiting proof clock moved backwards')
                        if (challenge is None or challenge[0] != gate._epoch or challenge[2] != session
                                or challenge[3] != data['seq'] or challenge[4] != data['stopped_at']
                                or challenge[6] != 'verified' or challenge[7] != data['snapshot_digest']
                                or now >= challenge[5] or wait['stopped_at'] != data['stopped_at']):
                            raise ValueError('current virtual waiting proof is not valid')
                        remaining = challenge[5] - now
                        if remaining > min(10.0, gate._ttl / 3):
                            return {'state': 'not_due', 'expires': challenge[5]}
                        state = _check_live_stop(runtime, db, robot, robot_id, task_id, data)
                        baseline, old_expires, last_now = data['snapshot_digest'], challenge[5], now
                    try:
                        current = _now(runtime, gate, task_id)
                        if current < last_now or current >= old_expires:
                            raise ValueError('original virtual waiting proof expired or clock changed')
                        last_now = current
                        token = gate.begin(task_id, session)
                        with closing(gate._connect()) as db:
                            db.execute('BEGIN')
                            after_begin = _snapshot(db, task_id, field_required=progress._field_required)
                            pending = db.execute('SELECT epoch,challenge,robot_session,seq,stopped_at,status,snapshot_digest '
                                                 'FROM recovery_challenges WHERE task_id=?', (task_id,)).fetchone()
                        if (after_begin['snapshot_digest'] != baseline or pending !=
                                (gate._epoch, token, session, data['seq'], data['stopped_at'], 'pending', baseline)):
                            raise ValueError('waiting state changed while challenge was started')
                        current = _now(runtime, gate, task_id)
                        if current < last_now or current >= old_expires:
                            raise ValueError('original virtual waiting proof expired or clock changed')
                        last_now = current
                        gate.complete(task_id, session, token, robot_id=robot_id, order_id=task_id,
                                      seq=data['seq'], map_id=data['map_id'], stopped_at=data['stopped_at'],
                                      order_digest=data['order_digest'], stopped=True)
                        current = _now(runtime, gate, task_id)
                        if current < last_now or current >= old_expires:
                            raise ValueError('original virtual waiting proof expired or clock changed')
                        with progress._transaction() as db:
                            if is_prepared(db, task_id):
                                raise ValueError('virtual reroute appeared during waiting proof')
                            _verify_field_global(db, field_required=progress._field_required)
                            after = _snapshot(db, task_id, field_required=progress._field_required)
                            check = db.execute('SELECT payload,digest,task_id,robot_session,seq,state FROM sim_event_inbox '
                                               'WHERE robot_id=? AND event_id=?', (robot_id, event_id)).fetchone()
                            if (after['snapshot_digest'] != baseline or check != row
                                    or receiver is not runtime.receiver or receiver.client is not receiver_client
                                    or not receiver.ready or not receiver_client.is_connected()
                                    or not robot.ready or robot._client is not client
                                    or robot._event_uncertain or robot._reroute_next_inflight or not robot._queue.empty()
                                    or (robot_id, event_id) not in receiver._occupied_events
                                    or robot.executor.snapshot() != state):
                                raise ValueError('virtual waiting state changed during refresh')
                            _validated_wait(runtime, db, robot_id, event_id, row[0], row[1], task_id, session, row[4])
                            fresh = db.execute('SELECT expires FROM recovery_challenges WHERE task_id=?', (task_id,)).fetchone()
                            current = _now(runtime, gate, task_id)
                            if current < last_now or current >= old_expires:
                                raise ValueError('original virtual waiting proof expired during verification')
                        return {'state': 'refreshed', 'expires': fresh[0]}
                    except BaseException as original:
                        try:
                            gate.invalidate(task_id)
                        except BaseException as cleanup_error:
                            original.add_note('virtual waiting proof invalidation failed: ' + repr(cleanup_error))
                        raise


def _canonical_order(value):
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('ascii')
