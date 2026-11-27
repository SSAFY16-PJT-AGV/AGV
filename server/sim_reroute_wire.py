"""One-shot, isolated MQTT transport for a prepared virtual reroute.

Broker acknowledgement does not prove robot acceptance or grant movement.
An attempted proposal remains uncertain; neither reservation is released.
"""
from contextlib import contextmanager
import json
import threading

import paho.mqtt.client as mqtt

from server.allocation_store import _verify_field_global
from server.segments import plan_segment
from server.sim_reroute_candidate import _owned_simulator
from sim.mqtt_order_robot import _verify_no_proxy
from sim.order_inbox import _decode


def _message(db, runtime, task_id, *, required_state):
    progress = runtime.progress
    row = db.execute('SELECT generation,transition_id,robot_id,robot_session,parent_seq,old_payload,'
        'stopped_at,incoming_edge,nodes_json,edges_json,blocked_json,recovery_epoch,state '
        'FROM sim_reroute_generations WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError('prepared simulated reroute is missing')
    generation, transition, robot, session, seq, payload, stopped, incoming, nodes, edges, blocked, epoch, state = row
    if (state not in ((required_state,) if isinstance(required_state, str) else required_state)
            or runtime._tasks.get(task_id) != (robot, session)):
        raise ValueError('simulated reroute state or task binding changed')
    with runtime.gate._state_lock:
        if task_id in runtime.gate._blocked or runtime.gate._generations.get(task_id, 0) != epoch:
            raise ValueError('simulated recovery generation changed')
    progress._authorize(db, task_id, session, seq, stopped)
    old, _canonical, digest = _decode(payload.encode('utf-8'), robot, progress._map_id)
    nodes = json.loads(nodes)
    message = {'type': 'sim.reroute.prepare.v1', 'data': {
        'transition_id': transition, 'generation': generation, 'robot_id': robot, 'robot_session': session,
        'task_id': task_id, 'parent_seq': seq, 'parent_digest': digest, 'map_id': progress._map_id,
        'stopped_at': stopped, 'incoming_edge': incoming, 'nodes': nodes, 'edge_ids': json.loads(edges),
        'blocked_resources': json.loads(blocked), 'released_until': plan_segment(progress._map, nodes).released_until,
        'action_at_end': old['action_at_end']}}
    return json.dumps(message, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('ascii')


def _robot(runtime, task_id):
    robot_id = runtime._tasks.get(task_id, (None,))[0]
    bindings = runtime._reroute_simulators
    robot = bindings.get(robot_id) if bindings is not None else None
    if robot is None or not _owned_simulator(runtime, robot_id, robot):
        raise ValueError('bound isolated simulated robot required')
    return robot


def publish_proposal(runtime, task_id):
    """Attempt one QoS1 proposal after durably recording uncertain delivery."""
    if not runtime._reroute_transitions:
        raise RuntimeError('simulated reroute transitions are disabled')
    _verify_no_proxy()
    with runtime._runtime_read_operation():
        robot = _robot(runtime, task_id)
        client = runtime._new_client()
        try:
            if getattr(client, '_proxy', None):
                raise RuntimeError('a client-specific MQTT proxy is not allowed')
            connected, failure = threading.Event(), []
            def on_connect(_client, _userdata, _flags, reason, _properties):
                if reason.is_failure:
                    failure.append(True)
                connected.set()
            client.on_connect = on_connect
            if client.connect('127.0.0.1', runtime.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('isolated simulated publisher connect failed')
            if client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('isolated simulated publisher loop failed')
            if not connected.wait(runtime.connect_timeout) or failure or not client.is_connected():
                raise RuntimeError('isolated simulated publisher connection timed out')
            with robot._lock, runtime._lock:
                runtime._ready()
                receiver = runtime.receiver
                with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                    if not robot.ready or robot._event_uncertain or not robot._queue.empty():
                        raise ValueError('simulated robot is not ready for a proposal')
                    with runtime.progress._transaction() as db:
                        _verify_field_global(db, field_required=True)
                        payload = _message(db, runtime, task_id, required_state='prepared')
                        if db.execute("UPDATE sim_reroute_generations SET state='uncertain' "
                                      "WHERE task_id=? AND state='prepared'", (task_id,)).rowcount != 1:
                            raise ValueError('simulated reroute was already attempted')
                        _verify_field_global(db, field_required=True)
                    # Commit before enqueue; an exception after this point is
                    # uncertain and must never trigger an automatic retry.
                    if not robot.ready or receiver is not runtime.receiver or not receiver.ready:
                        raise RuntimeError('simulated connection changed before proposal')
                    info = client.publish('agv/{}/reroute/prepare'.format(robot.robot_id), payload,
                                          qos=1, retain=False)
                    if info.rc != mqtt.MQTT_ERR_SUCCESS:
                        raise RuntimeError('simulated proposal enqueue failed')
            info.wait_for_publish(timeout=runtime.ack_timeout)
            if not info.is_published():
                raise RuntimeError('simulated proposal broker acknowledgement is unknown')
            return {'state': 'uncertain', 'broker_ack': True, 'motion_authorized': False}
        finally:
            runtime._close_client(client)


class SimRerouteProposalGuard:
    """Pin the prepared reservation and session while a robot records receipt."""
    def __init__(self, runtime, *, robot_id):
        if robot_id not in ('sim-r1', 'sim-r2'):
            raise ValueError('isolated simulated robot required')
        self.runtime, self.robot_id = runtime, robot_id

    @contextmanager
    def guard(self, payload):
        if not isinstance(payload, bytes):
            raise ValueError('proposal payload must be bytes')
        runtime = self.runtime
        try:
            task_id = json.loads(payload.decode('utf-8'))['data']['task_id']
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError('invalid simulated proposal') from exc
        if not isinstance(task_id, str) or not task_id or len(task_id.encode('utf-8')) > 256:
            raise ValueError('invalid simulated proposal task')
        with runtime._runtime_read_operation():
            robot = _robot(runtime, task_id)
            if robot.robot_id != self.robot_id:
                raise ValueError('proposal belongs to a different robot')
            with robot._lock, runtime._lock:
                runtime._ready()
                receiver = runtime.receiver
                with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                    if not robot.ready or robot._event_uncertain or not robot._queue.empty():
                        raise ValueError('simulated robot is unavailable')
                    with runtime.progress._transaction() as db:
                        _verify_field_global(db, field_required=True)
                        expected = _message(db, runtime, task_id, required_state=('uncertain', 'accepted'))
                        if payload != expected or receiver is not runtime.receiver or not receiver.ready:
                            raise ValueError('proposal differs from the reserved generation')
                        yield
