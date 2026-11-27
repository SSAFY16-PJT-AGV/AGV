"""One-shot execution authority for a single-segment virtual reroute.

Original and proposed resources remain jointly reserved throughout motion.
The message and receipt keys authorize only a loopback simulated executor.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import threading

import paho.mqtt.client as mqtt

from server.allocation_store import _verify_field_global
from server.sim_reroute_activate_wire import _check_binding
from server.sim_reroute_activation import _canonical
from server.sim_reroute_activate_receipt import ActivationReceiptInbox
from server.sim_reroute_wire import _message
from sim.mqtt_order_robot import _verify_no_proxy
from sim.reroute_activation_mqtt import SimRerouteActivationMqttReceiver, verify_activation_receipt
from sim.order_inbox import _decode


class SimRerouteExecutionGuard:
    """Hold exact server generation, live session and both holds for each tick."""
    def __init__(self, runtime, *, receipt_inbox, robot_receiver, activation_inbox, activation_receiver):
        if (type(activation_inbox) is not ActivationReceiptInbox or activation_inbox.runtime is not runtime
                or activation_inbox.proposals is not receipt_inbox or activation_inbox.robot_receiver is not robot_receiver
                or type(activation_receiver) is not SimRerouteActivationMqttReceiver
                or activation_receiver.proposal_receiver is not robot_receiver
                or activation_receiver.guard.runtime is not runtime
                or activation_receiver.guard.receipt_inbox is not receipt_inbox
                or activation_receiver.activation.acceptance is not robot_receiver.acceptance
                or not hmac.compare_digest(activation_receiver._key, robot_receiver._key)):
            raise ValueError('matching live virtual activation handshake required')
        self.runtime, self.receipt_inbox, self.robot_receiver = runtime, receipt_inbox, robot_receiver
        self.activation_inbox, self.activation_receiver = activation_inbox, activation_receiver
        self._execution_receiver = None

    def bind_receiver(self, receiver):
        from sim.reroute_execute_mqtt import SimRerouteExecutionMqttReceiver
        if (self._execution_receiver is not None or type(receiver) is not SimRerouteExecutionMqttReceiver
                or receiver.guard is not self or receiver.execution.executor is not self.robot_receiver.robot.executor
                or not hmac.compare_digest(receiver._key, self.robot_receiver._key)):
            raise ValueError('one matching virtual execution receiver required')
        self._execution_receiver = receiver

    @contextmanager
    def guard(self, payload):
        if not isinstance(payload, bytes) or len(payload) > 256 * 1024:
            raise ValueError('bounded simulated execution payload required')
        try:
            task_id = json.loads(payload.decode('utf-8'))['data']['task_id']
            if not isinstance(task_id, str) or not task_id or len(task_id.encode('utf-8')) > 256:
                raise ValueError('invalid task')
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError('invalid simulated execution') from exc
        runtime = self.runtime
        with runtime._runtime_read_operation():
            robot = _check_binding(runtime, task_id, self.receipt_inbox, self.robot_receiver)
            execution_receiver = self._execution_receiver
            if execution_receiver is None:
                raise ValueError('virtual execution receiver is not bound')
            with self.robot_receiver._lifecycle_lock, self.robot_receiver._connection_fence, \
                    self.activation_receiver.endpoint._connection_fence, execution_receiver.endpoint._connection_fence, \
                    robot._lock, runtime._lock:
                runtime._ready()
                receiver = runtime.receiver
                with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                    with runtime.progress._transaction() as db:
                        expected, _original = _execution(db, self, task_id, robot, state='uncertain')
                        if payload != expected or receiver is not runtime.receiver or not receiver.ready:
                            raise ValueError('execution differs from the reserved virtual generation')
                        yield

    @contextmanager
    def guard_next(self, _payload, _next_order):
        # This first supported transition is one segment all the way to its
        # final destination. Future multi-segment generations need their own
        # progression proof; do not infer or skip a waiting point.
        raise ValueError('multi-segment reroute execution is not supported')
        yield


def _live_endpoint(endpoint):
    return (endpoint._ready.is_set() and not endpoint._failed.is_set() and not endpoint._stopping.is_set()
            and endpoint._client.is_connected())


def _finished_message(db, runtime, task_id, robot):
    """Recheck parent proof and completed generation without requiring the old hold."""
    from server.segments import plan_segment
    from server.sim_reroute_finish import finished_resources

    progress, gate = runtime.progress, runtime.gate
    if not db.in_transaction or getattr(gate._local, 'db', None) is not db:
        raise ValueError('current simulated recovery transaction is required')
    row = db.execute('SELECT generation,transition_id,robot_id,robot_session,parent_seq,old_payload,'
                     'stopped_at,incoming_edge,nodes_json,edges_json,blocked_json,recovery_epoch,state '
                     'FROM sim_reroute_generations WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError('completed simulated reroute is missing')
    generation, transition, rid, session, seq, payload, stopped, incoming, nodes, edges, blocked, epoch, state = row
    if (state != 'accepted' or rid != robot.robot_id or runtime._tasks.get(task_id) != (rid, session)
            or not isinstance(payload, str) or type(seq) is not int or seq < 1):
        raise ValueError('completed simulated reroute identity changed')
    with gate._state_lock:
        if task_id in gate._blocked or gate._generations.get(task_id, 0) != epoch:
            raise ValueError('simulated recovery generation changed')
    gate._active(db)
    old, _encoded, digest = _decode(payload.encode('utf-8'), rid, progress._map_id)
    challenge = db.execute('SELECT epoch,robot_session,seq,stopped_at,expires,status,robot_id,map_id,'
                           'order_id,order_digest,map_fingerprint FROM recovery_challenges WHERE task_id=?',
                           (task_id,)).fetchone()
    if (challenge is None or challenge[:4] != (gate._epoch, session, seq, stopped)
            or gate._now() >= challenge[4] or challenge[5:] !=
            ('verified', rid, progress._map_id, task_id, digest, progress._fingerprint)):
        raise ValueError('current simulated parent recovery proof is unavailable')
    prior_robot, path, prior_stop = progress._job(db, task_id, rid)
    prior_seq, prior_payload, prior_state, prior_order = progress._latest(db, task_id, rid, path, prior_stop)
    if (prior_robot != rid or prior_seq != seq or prior_stop != stopped or prior_payload != payload
            or prior_state != 'acked' or prior_order != old):
        raise ValueError('completed simulated parent order changed')
    old_resources = set(progress._segment_for_task(db, task_id, path, stopped).resources)
    if (finished_resources(db, progress, task_id, rid, path, old_resources) != {('node', path[-1])}
            or progress._occupancy(db, task_id) != {('node', path[-1])}):
        raise ValueError('completed simulated destination is not reserved')
    nodes = json.loads(nodes)
    message = {'type': 'sim.reroute.prepare.v1', 'data': {
        'transition_id': transition, 'generation': generation, 'robot_id': rid, 'robot_session': session,
        'task_id': task_id, 'parent_seq': seq, 'parent_digest': digest, 'map_id': progress._map_id,
        'stopped_at': stopped, 'incoming_edge': incoming, 'nodes': nodes, 'edge_ids': json.loads(edges),
        'blocked_resources': json.loads(blocked), 'released_until': plan_segment(progress._map, nodes).released_until,
        'action_at_end': old['action_at_end']}}
    return _canonical(message)


def _execution(db, guard, task_id, robot, *, state):
    runtime, proposal_receiver = guard.runtime, guard.robot_receiver
    _verify_field_global(db, field_required=True)
    if (not robot.ready or robot._event_uncertain or not robot._queue.empty()
            or robot._client is not proposal_receiver._robot_client
            or not proposal_receiver._ready.is_set() or proposal_receiver._failed.is_set()
            or proposal_receiver._stopping.is_set() or not proposal_receiver._client.is_connected()
            or not _live_endpoint(guard.activation_receiver.endpoint)
            or guard._execution_receiver is None or not _live_endpoint(guard._execution_receiver.endpoint)):
        raise ValueError('live virtual robot and reroute connections required')
    proposal = (_finished_message(db, runtime, task_id, robot) if state == 'finished'
                else _message(db, runtime, task_id, required_state='accepted'))
    original = json.loads(proposal)['data']
    # The proposed route's single reserved segment must reach its goal.
    if original['released_until'] != original['nodes'][-1]:
        raise ValueError('multi-segment reroutes require a later generation protocol')
    signed = db.execute('SELECT robot_id,payload,digest,state FROM sim_reroute_server_receipts '
                        'WHERE task_id=? AND transition_id=?', (task_id, original['transition_id'])).fetchone()
    key = guard.receipt_inbox._keys.get(robot.robot_id)
    if (signed is None or signed[0] != robot.robot_id or not isinstance(signed[1], bytes)
            or hashlib.sha256(signed[1]).hexdigest() != signed[2] or signed[3] != 'recorded_not_activated'
            or key is None or not hmac.compare_digest(key, proposal_receiver._key)):
        raise ValueError('live virtual proposal receipt is unavailable')
    from sim.reroute_mqtt import verify_receipt
    receipt = verify_receipt(signed[1], key)
    if (receipt['transition_id'] != original['transition_id'] or receipt['task_id'] != task_id
            or receipt['robot_session'] != original['robot_session'] or receipt['robot_id'] != robot.robot_id
            or receipt['generation'] != original['generation'] or receipt['parent_seq'] != original['parent_seq']
            or receipt['digest'] != hashlib.sha256(proposal).hexdigest()):
        raise ValueError('virtual proposal receipt differs')
    activation = _canonical({'type': 'sim.reroute.activate.v1', 'data':
                             dict(original, proposal_digest=hashlib.sha256(proposal).hexdigest())})
    attempt = db.execute('SELECT transition_id,payload,digest,state FROM sim_reroute_activation_attempts '
                         'WHERE task_id=?', (task_id,)).fetchone()
    if attempt != (original['transition_id'], activation, hashlib.sha256(activation).hexdigest(), 'uncertain'):
        raise ValueError('virtual activation attempt differs')
    received = db.execute('SELECT robot_id,payload,digest,state FROM sim_reroute_activation_receipts '
                          'WHERE task_id=? AND transition_id=?', (task_id, original['transition_id'])).fetchone()
    if (received is None or received[0] != robot.robot_id or not isinstance(received[1], bytes)
            or hashlib.sha256(received[1]).hexdigest() != received[2] or received[3] != 'recorded_not_executed'):
        raise ValueError('durable virtual activation receipt unavailable')
    accepted = verify_activation_receipt(received[1], key)
    if (accepted['transition_id'] != original['transition_id'] or accepted['task_id'] != task_id
            or accepted['robot_session'] != original['robot_session'] or accepted['robot_id'] != robot.robot_id
            or accepted['generation'] != original['generation'] or accepted['parent_seq'] != original['parent_seq']
            or accepted['digest'] != hashlib.sha256(activation).hexdigest()
            or accepted['proposal_digest'] != hashlib.sha256(proposal).hexdigest()):
        raise ValueError('virtual activation receipt differs')
    snapshot = robot.executor.snapshot()
    prior = snapshot.get('receipt') or {}
    transition = snapshot.get('reroute_transition_id')
    if transition is None:
        if state == 'finished':
            raise ValueError('completed generation is not installed on the live virtual robot')
        stopped, incoming = robot.executor.field_stop_direction()
        if (snapshot.get('status') != 'stopped' or stopped != original['stopped_at']
                or incoming != original['incoming_edge'] or prior.get('order_id') != task_id
                or prior.get('seq') != original['parent_seq'] or prior.get('digest') != original['parent_digest']
                or prior.get('state') != 'completed'):
            raise ValueError('virtual robot parent stop changed')
    else:
        robot.executor._verify_field_execution()
        order = snapshot.get('order') or {}
        if (transition != original['transition_id'] or snapshot.get('status') not in ('moving', 'stopped')
                or prior.get('order_id') != task_id or prior.get('seq') != original['parent_seq'] + 1
                or order.get('nodes') != original['nodes'] or order.get('released_until') != original['released_until']
                or _canonical(order.get('action_at_end')) != _canonical(original['action_at_end'])):
            raise ValueError('virtual robot execution differs from the reserved generation')
        if state == 'finished' and (snapshot.get('status') != 'stopped'
                or snapshot.get('current_node') != original['nodes'][-1]
                or snapshot.get('visit_index') != snapshot.get('released_index')
                or prior.get('state') != 'completed'):
            raise ValueError('virtual robot is not stopped at the completed destination')
    execution = _canonical({'type': 'sim.reroute.execute.v1', 'data':
        dict(json.loads(activation)['data'], activation_digest=hashlib.sha256(activation).hexdigest())})
    if state is not None:
        saved = db.execute('SELECT transition_id,payload,digest,state FROM sim_reroute_execution_attempts '
                           'WHERE task_id=?', (task_id,)).fetchone()
        if saved != (original['transition_id'], execution, hashlib.sha256(execution).hexdigest(), state):
            raise ValueError('virtual execution attempt is missing or changed')
    return execution, original


def prepare_execution(runtime, task_id, guard, execution_receiver):
    """Persist the exact single-segment execute request without publishing."""
    if (type(guard) is not SimRerouteExecutionGuard or guard.runtime is not runtime
            or guard._execution_receiver is not execution_receiver):
        raise ValueError('bound virtual execution guard required')
    with runtime._runtime_read_operation():
        robot = _check_binding(runtime, task_id, guard.receipt_inbox, guard.robot_receiver)
        with guard.robot_receiver._lifecycle_lock, guard.robot_receiver._connection_fence, \
                guard.activation_receiver.endpoint._connection_fence, execution_receiver.endpoint._connection_fence, \
                robot._lock, runtime._lock:
            runtime._ready()
            with runtime.receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                with runtime.progress._transaction() as db:
                    payload, old = _execution(db, guard, task_id, robot, state=None)
                    db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_execution_attempts ('
                               'task_id TEXT PRIMARY KEY NOT NULL REFERENCES sim_reroute_generations(task_id), '
                               'transition_id TEXT UNIQUE NOT NULL, payload BLOB NOT NULL, digest TEXT NOT NULL, '
                               "state TEXT NOT NULL CHECK(state IN ('staged','uncertain','finished')))" )
                    expected = (old['transition_id'], payload, hashlib.sha256(payload).hexdigest(), 'staged')
                    current = db.execute('SELECT transition_id,payload,digest,state FROM sim_reroute_execution_attempts '
                                         'WHERE task_id=?', (task_id,)).fetchone()
                    if current is None:
                        db.execute('INSERT INTO sim_reroute_execution_attempts VALUES (?,?,?,?,?)', (task_id, *expected))
                    elif current != expected:
                        raise ValueError('execution was changed or already attempted')
                    return {'state': 'ready_not_published', 'task_id': task_id,
                            'transition_id': old['transition_id'], 'motion_authorized': False}


def publish_execution(runtime, task_id, guard, execution_receiver):
    """Record uncertain before one QoS1 execution attempt; never retry."""
    if (type(guard) is not SimRerouteExecutionGuard or guard.runtime is not runtime
            or guard._execution_receiver is not execution_receiver):
        raise ValueError('bound virtual execution guard required')
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
                raise RuntimeError('simulated publisher loop failed')
            if not connected.wait(runtime.connect_timeout) or failure or not client.is_connected():
                raise RuntimeError('simulated publisher connection timed out')
            with guard.robot_receiver._lifecycle_lock, guard.robot_receiver._connection_fence, \
                    guard.activation_receiver.endpoint._connection_fence, execution_receiver.endpoint._connection_fence, \
                    robot._lock, runtime._lock:
                runtime._ready()
                receiver = runtime.receiver
                with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                    with runtime.progress._transaction() as db:
                        payload, _old = _execution(db, guard, task_id, robot, state='staged')
                        if db.execute("UPDATE sim_reroute_execution_attempts SET state='uncertain' "
                                      "WHERE task_id=? AND state='staged'", (task_id,)).rowcount != 1:
                            raise ValueError('execution was already attempted')
                    if not robot.ready or receiver is not runtime.receiver or not receiver.ready:
                        raise RuntimeError('virtual connection changed before execute send')
                    info = client.publish('agv/{}/reroute/execute'.format(robot.robot_id), payload, qos=1, retain=False)
                    if info.rc != mqtt.MQTT_ERR_SUCCESS:
                        raise RuntimeError('virtual execute enqueue failed')
            info.wait_for_publish(timeout=runtime.ack_timeout)
            if not info.is_published():
                raise RuntimeError('virtual execute broker acknowledgement is unknown')
            return {'state': 'uncertain', 'broker_ack': True}
        finally:
            runtime._close_client(client)
