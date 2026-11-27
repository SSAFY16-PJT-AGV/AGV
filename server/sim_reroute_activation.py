"""Stage an accepted virtual reroute without publishing or permitting motion.

The recorded robot receipt is revalidated with the live, per-session key.
Staging is not route activation. Both old and proposed resources remain held.
"""
import hashlib
import hmac
import json

from server.allocation_store import _verify_field_global
from server.sim_reroute_receipt import SimRerouteReceiptInbox
from server.sim_reroute_wire import _message, _robot
from sim.reroute_mqtt import SimRerouteMqttReceiver, verify_receipt


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('ascii')


def _insert_activation(db, task, transition, payload, digest):
    db.execute('INSERT INTO sim_reroute_activation_attempts VALUES (?,?,?,?,?)',
               (task, transition, payload, digest, 'staged'))


def stage_activation(runtime, task_id, receipt_inbox, robot_receiver):
    """Persist the exact activation candidate, without sending or moving."""
    if (type(receipt_inbox) is not SimRerouteReceiptInbox or receipt_inbox.runtime is not runtime
            or type(robot_receiver) is not SimRerouteMqttReceiver or robot_receiver.guard.runtime is not runtime
            or not runtime._reroute_transitions):
        raise ValueError('live isolated reroute session required')
    with runtime._runtime_read_operation():
        robot = _robot(runtime, task_id)
        if (robot_receiver.robot is not robot or robot_receiver.guard.robot_id != robot.robot_id
                or robot.robot_id not in receipt_inbox._keys
                or not hmac.compare_digest(robot_receiver._key, receipt_inbox._keys[robot.robot_id])):
            raise ValueError('reroute receipt key or robot instance changed')
        with robot_receiver._lifecycle_lock, robot_receiver._connection_fence, robot._lock, runtime._lock:
            runtime._ready()
            receiver = runtime.receiver
            with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                if (not robot.ready or robot._event_uncertain or not robot._queue.empty()
                        or robot._client is not robot_receiver._robot_client
                        or not robot_receiver._ready.is_set() or robot_receiver._failed.is_set()
                        or robot_receiver._stopping.is_set() or not robot_receiver._client.is_connected()):
                    raise ValueError('live simulated robot and reroute receiver required')
                with runtime.progress._transaction() as db:
                    _verify_field_global(db, field_required=True)
                    proposal = _message(db, runtime, task_id, required_state='accepted')
                    old = json.loads(proposal)['data']
                    row = db.execute('SELECT robot_id,payload,digest,state FROM sim_reroute_server_receipts '
                                     'WHERE task_id=? AND transition_id=?',
                                     (task_id, old['transition_id'])).fetchone()
                    if (row is None or row[0] != robot.robot_id or not isinstance(row[1], bytes)
                            or hashlib.sha256(row[1]).hexdigest() != row[2]
                            or row[3] != 'recorded_not_activated'):
                        raise ValueError('durable simulated robot receipt is missing')
                    receipt = verify_receipt(row[1], receipt_inbox._keys[robot.robot_id])
                    if (receipt['robot_id'] != robot.robot_id or receipt['robot_session'] != old['robot_session']
                            or receipt['task_id'] != task_id or receipt['transition_id'] != old['transition_id']
                            or receipt['generation'] != old['generation'] or receipt['parent_seq'] != old['parent_seq']
                            or receipt['digest'] != hashlib.sha256(proposal).hexdigest()
                            or robot_receiver.acceptance.robot_session != old['robot_session']):
                        raise ValueError('live signed receipt differs from the prepared reroute')
                    stopped, incoming = robot.executor.field_stop_direction()
                    snapshot = robot.executor.snapshot()
                    saved = snapshot.get('receipt') or {}
                    if (stopped != old['stopped_at'] or incoming != old['incoming_edge']
                            or snapshot.get('status') != 'stopped' or saved.get('order_id') != task_id
                            or saved.get('seq') != old['parent_seq'] or saved.get('digest') != old['parent_digest']
                            or saved.get('state') != 'completed'):
                        raise ValueError('simulated robot no longer matches its parent stop')
                    activation = _canonical({'type': 'sim.reroute.activate.v1', 'data':
                        dict(old, proposal_digest=hashlib.sha256(proposal).hexdigest())})
                    digest = hashlib.sha256(activation).hexdigest()
                    db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_activation_attempts ('
                        'task_id TEXT PRIMARY KEY NOT NULL REFERENCES sim_reroute_generations(task_id), '
                        'transition_id TEXT UNIQUE NOT NULL, payload BLOB NOT NULL, digest TEXT NOT NULL, '
                        "state TEXT NOT NULL CHECK(state IN ('staged','uncertain')))" )
                    current = db.execute('SELECT transition_id,payload,digest,state '
                                         'FROM sim_reroute_activation_attempts WHERE task_id=?', (task_id,)).fetchone()
                    expected = (old['transition_id'], activation, digest, 'staged')
                    if current is None:
                        _insert_activation(db, task_id, old['transition_id'], activation, digest)
                    elif current != expected:
                        raise ValueError('activation is changed or already attempted')
                    if (not robot.ready or robot._client is not robot_receiver._robot_client
                            or robot_receiver._failed.is_set() or not robot_receiver._client.is_connected()
                            or receiver is not runtime.receiver or not receiver.ready):
                        raise ValueError('simulation connection changed during staging')
                    return {'state': 'staged_not_published', 'task_id': task_id,
                            'transition_id': old['transition_id'], 'motion_authorized': False}
