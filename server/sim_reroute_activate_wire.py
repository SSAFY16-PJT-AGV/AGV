"""One-shot activation message staging for an isolated virtual reroute.

This handshake asks the virtual robot to durably record the activation. It
does not permit motion, transfer resources, or retry uncertain publication.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import threading

import paho.mqtt.client as mqtt

from server.allocation_store import _verify_field_global
from server.sim_reroute_activation import _canonical
from server.sim_reroute_receipt import SimRerouteReceiptInbox
from server.sim_reroute_wire import _message, _robot
from sim.mqtt_order_robot import _verify_no_proxy
from sim.reroute_mqtt import SimRerouteMqttReceiver, verify_receipt


def _check_binding(runtime, task_id, receipt_inbox, robot_receiver):
    if (type(receipt_inbox) is not SimRerouteReceiptInbox or receipt_inbox.runtime is not runtime
            or type(robot_receiver) is not SimRerouteMqttReceiver or robot_receiver.guard.runtime is not runtime
            or not runtime._reroute_transitions):
        raise ValueError('live isolated reroute session required')
    robot = _robot(runtime, task_id)
    if (robot_receiver.robot is not robot or robot_receiver.guard.robot_id != robot.robot_id
            or robot.robot_id not in receipt_inbox._keys
            or not hmac.compare_digest(robot_receiver._key, receipt_inbox._keys[robot.robot_id])):
        raise ValueError('reroute receipt key or robot instance changed')
    return robot


def _activation(db, runtime, task_id, receipt_inbox, robot_receiver, robot, *, state):
    _verify_field_global(db, field_required=True)
    if (not robot.ready or robot._event_uncertain or not robot._queue.empty()
            or robot._client is not robot_receiver._robot_client or not robot_receiver._ready.is_set()
            or robot_receiver._failed.is_set() or robot_receiver._stopping.is_set()
            or not robot_receiver._client.is_connected()):
        raise ValueError('live simulated robot and reroute receiver required')
    proposal = _message(db, runtime, task_id, required_state='accepted')
    old = json.loads(proposal)['data']
    row = db.execute('SELECT robot_id,payload,digest,state FROM sim_reroute_server_receipts '
                     'WHERE task_id=? AND transition_id=?', (task_id, old['transition_id'])).fetchone()
    if (row is None or row[0] != robot.robot_id or not isinstance(row[1], bytes)
            or hashlib.sha256(row[1]).hexdigest() != row[2] or row[3] != 'recorded_not_activated'):
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
    if (stopped != old['stopped_at'] or incoming != old['incoming_edge'] or snapshot.get('status') != 'stopped'
            or saved.get('order_id') != task_id or saved.get('seq') != old['parent_seq']
            or saved.get('digest') != old['parent_digest'] or saved.get('state') != 'completed'):
        raise ValueError('simulated robot no longer matches its parent stop')
    expected = _canonical({'type': 'sim.reroute.activate.v1', 'data':
                           dict(old, proposal_digest=hashlib.sha256(proposal).hexdigest())})
    saved = db.execute('SELECT transition_id,payload,digest,state FROM sim_reroute_activation_attempts '
                       'WHERE task_id=?', (task_id,)).fetchone()
    if saved != (old['transition_id'], expected, hashlib.sha256(expected).hexdigest(), state):
        raise ValueError('simulated activation record changed or was already attempted')
    return expected


def publish_activation(runtime, task_id, receipt_inbox, robot_receiver):
    """Publish one recorded activation request after marking it uncertain."""
    _verify_no_proxy()
    with runtime._runtime_read_operation():
        robot = _check_binding(runtime, task_id, receipt_inbox, robot_receiver)
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
            with robot_receiver._lifecycle_lock, robot_receiver._connection_fence, robot._lock, runtime._lock:
                runtime._ready()
                receiver = runtime.receiver
                with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                    with runtime.progress._transaction() as db:
                        payload = _activation(db, runtime, task_id, receipt_inbox, robot_receiver, robot, state='staged')
                        if db.execute("UPDATE sim_reroute_activation_attempts SET state='uncertain' "
                                      "WHERE task_id=? AND state='staged'", (task_id,)).rowcount != 1:
                            raise ValueError('activation was already attempted')
                    if (not robot.ready or robot._client is not robot_receiver._robot_client
                            or receiver is not runtime.receiver or not receiver.ready
                            or robot_receiver._failed.is_set() or not robot_receiver._client.is_connected()):
                        raise RuntimeError('simulated connection changed before activation send')
                    info = client.publish('agv/{}/reroute/activate'.format(robot.robot_id), payload, qos=1, retain=False)
                    if info.rc != mqtt.MQTT_ERR_SUCCESS:
                        raise RuntimeError('simulated activation enqueue failed')
            info.wait_for_publish(timeout=runtime.ack_timeout)
            if not info.is_published():
                raise RuntimeError('simulated activation broker acknowledgement is unknown')
            return {'state': 'uncertain', 'broker_ack': True, 'motion_authorized': False}
        finally:
            runtime._close_client(client)


class SimRerouteActivationGuard:
    """Pin the exact attempted activation and live evidence while recording."""
    def __init__(self, runtime, *, receipt_inbox, robot_receiver):
        self.runtime, self.receipt_inbox, self.robot_receiver = runtime, receipt_inbox, robot_receiver

    @contextmanager
    def guard(self, payload):
        if not isinstance(payload, bytes) or len(payload) > 256 * 1024:
            raise ValueError('bounded simulated activation payload required')
        try:
            task_id = json.loads(payload.decode('utf-8'))['data']['task_id']
            if not isinstance(task_id, str) or not task_id or len(task_id.encode('utf-8')) > 256:
                raise ValueError('invalid task')
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError('invalid simulated activation') from exc
        runtime = self.runtime
        with runtime._runtime_read_operation():
            robot = _check_binding(runtime, task_id, self.receipt_inbox, self.robot_receiver)
            with self.robot_receiver._lifecycle_lock, self.robot_receiver._connection_fence, robot._lock, runtime._lock:
                runtime._ready()
                receiver = runtime.receiver
                with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                    with runtime.progress._transaction() as db:
                        expected = _activation(db, runtime, task_id, self.receipt_inbox, self.robot_receiver,
                                               robot, state='uncertain')
                        if payload != expected or receiver is not runtime.receiver or not receiver.ready:
                            raise ValueError('activation differs from the staged generation')
                        yield
