"""Optional MQTT receiver for virtual reroute proposals; never moves hardware.

Only a server-verified, already-reserved simulated proposal can be recorded.
The signed receipt is an isolated in-process simulation claim, not a physical
robot identity, action completion, or movement authorization.
"""
import hashlib
import hmac
import json
import queue
import re
import secrets
import threading

import paho.mqtt.client as mqtt

from server.mqtt_security import configure_mqtt_client, read_mqtt_security
from server.sim_mqtt_cleanup import reset_stopped_client
from server.sim_reroute_candidate import _owned_simulator
from server.sim_reroute_wire import SimRerouteProposalGuard
from sim.mqtt_order_robot import SimMqttOrderRobot, _verify_no_proxy
from sim.order_inbox import _object_pairs, _constant
from sim.reroute_acceptance import SimRerouteAcceptance


_RECEIPT_FIELDS = frozenset(('robot_id', 'robot_session', 'task_id', 'transition_id',
                             'generation', 'parent_seq', 'digest', 'state'))


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def verify_receipt(payload, key):
    if not isinstance(payload, bytes) or len(payload) > 65536 or not isinstance(key, bytes) or len(key) < 32:
        raise ValueError('bounded receipt and a simulation key are required')
    try:
        message = json.loads(payload.decode('utf-8'), object_pairs_hook=_object_pairs, parse_constant=_constant)
        if (not isinstance(message, dict) or set(message) != {'type', 'data', 'mac'}
                or message['type'] != 'sim.reroute.receipt.v1' or not isinstance(message['data'], dict)
                or set(message['data']) != _RECEIPT_FIELDS or not isinstance(message['mac'], str)
                or re.fullmatch('[0-9a-f]{64}', message['mac']) is None):
            raise ValueError('invalid simulated receipt')
        signed = {'type': message['type'], 'data': message['data']}
        if not hmac.compare_digest(hmac.new(key, _canonical(signed), hashlib.sha256).hexdigest(), message['mac']):
            raise ValueError('invalid simulated receipt signature')
        data = message['data']
        if (data['robot_id'] not in ('sim-r1', 'sim-r2') or type(data['generation']) is not int
                or data['generation'] != 1 or type(data['parent_seq']) is not int or data['parent_seq'] < 1
                or any(not isinstance(data[k], str) or not data[k] or len(data[k].encode('utf-8')) > 256
                       for k in ('robot_session', 'task_id'))
                or not isinstance(data['transition_id'], str)
                or re.fullmatch('[0-9a-f]{32}', data['transition_id']) is None
                or not isinstance(data['digest'], str) or re.fullmatch('[0-9a-f]{64}', data['digest']) is None
                or data['state'] != 'accepted_not_executed'):
            raise ValueError('invalid simulated receipt fields')
        return dict(data)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid simulated receipt') from exc


class SimRerouteMqttReceiver:
    """Receive one isolated proposal, record it, and send a signed receipt."""
    def __init__(self, robot, guard, acceptance, *, receipt_key):
        if (type(robot) is not SimMqttOrderRobot or type(guard) is not SimRerouteProposalGuard
                or guard.robot_id != robot.robot_id or type(acceptance) is not SimRerouteAcceptance
                or acceptance.executor is not robot.executor or not isinstance(receipt_key, bytes)
                or len(receipt_key) < 32 or guard.runtime._reroute_simulators is None
                or guard.runtime._reroute_simulators.get(robot.robot_id) is not robot
                or not _owned_simulator(guard.runtime, robot.robot_id, robot)
                or acceptance.robot_session not in guard.runtime._sessions.get(robot.robot_id, {}).values()):
            raise ValueError('bound simulated robot, guard, inbox and receipt key required')
        self.robot, self.guard, self.acceptance, self._key = robot, guard, acceptance, receipt_key
        self._robot_client = robot._client
        self._queue = queue.Queue(maxsize=32)
        self._ready, self._failed, self._stopping = threading.Event(), threading.Event(), threading.Event()
        self._worker = None
        self._started = False
        self._lifecycle_lock = threading.RLock()
        self._network_cleanup_failed = False
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
            client_id=robot.robot_id + '-reroute-' + secrets.token_hex(8), protocol=mqtt.MQTTv311,
            reconnect_on_failure=False)
        configure_mqtt_client(self._client, *read_mqtt_security())
        self._client.manual_ack_set(True)
        self._client.on_connect = self._connect
        self._client.on_subscribe = self._subscribe
        self._client.on_message = self._message
        self._client.on_disconnect = self._disconnect
        self._mid = None

    def _connect(self, client, _userdata, _flags, reason, _properties):
        if reason.is_failure or self._stopping.is_set():
            self._failed.set()
            return
        result, mid = client.subscribe('agv/{}/reroute/prepare'.format(self.robot.robot_id), qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self._failed.set()
            return
        self._mid = mid

    def _subscribe(self, _client, _userdata, mid, reasons, _properties):
        if (self._stopping.is_set() or mid != self._mid or len(reasons) != 1
                or reasons[0].is_failure or reasons[0].value != 1):
            self._failed.set()
        else:
            self._ready.set()

    def _message(self, _client, _userdata, message):
        if (self._stopping.is_set() or self._failed.is_set()
                or message.topic != 'agv/{}/reroute/prepare'.format(self.robot.robot_id)
                or message.qos != 1 or message.retain or not isinstance(message.payload, bytes)
                or len(message.payload) > 256 * 1024):
            self._failed.set()
            return
        try:
            self._queue.put_nowait((message.payload, message.mid, message.qos))
        except queue.Full:
            self._failed.set()

    def _disconnect(self, _client, _userdata, _flags, _reason, _properties):
        self._ready.clear()
        if not self._stopping.is_set():
            self._failed.set()

    def _run(self):
        while not self._stopping.is_set() and not self._failed.is_set():
            try:
                payload, mid, qos = self._queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                with self.guard.guard(payload):
                    if (self.guard.runtime._reroute_simulators is None
                            or self.guard.runtime._reroute_simulators.get(self.robot.robot_id) is not self.robot
                            or self.robot._client is not self._robot_client or not self.robot.ready):
                        raise ValueError('simulated robot instance or connection changed')
                    result = self.acceptance.accept(payload)
                    if self.robot._client is not self._robot_client or not self.robot.ready:
                        raise ValueError('simulated robot disconnected during acceptance')
                if (self._stopping.is_set() or not self._client.is_connected() or self._failed.is_set()
                        or self._client.ack(mid, qos) != mqtt.MQTT_ERR_SUCCESS):
                    raise RuntimeError('simulated proposal receipt acknowledgement failed')
                data = {k: result[k] for k in ('task_id', 'transition_id', 'generation', 'parent_seq', 'digest', 'state')}
                data.update(robot_id=self.robot.robot_id, robot_session=self.acceptance.robot_session)
                signed = {'type': 'sim.reroute.receipt.v1', 'data': data}
                message = dict(signed, mac=hmac.new(self._key, _canonical(signed), hashlib.sha256).hexdigest())
                if self._stopping.is_set() or not self._client.is_connected():
                    raise RuntimeError('simulated receipt connection changed')
                info = self._client.publish('agv/{}/reroute/receipt'.format(self.robot.robot_id),
                                            _canonical(message), qos=1, retain=False)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('simulated receipt enqueue failed')
                info.wait_for_publish(timeout=5)
                if not info.is_published() or not self._client.is_connected():
                    raise RuntimeError('simulated receipt delivery is uncertain')
            except Exception:
                self._failed.set()
                self._ready.clear()
                return

    def start(self):
        with self._lifecycle_lock:
            return self._start_locked()

    def _start_locked(self):
        if self._started or self._stopping.is_set():
            raise RuntimeError('simulated reroute receiver cannot be restarted')
        self._started = True
        self.acceptance.initialize()
        _verify_no_proxy()
        if getattr(self._client, '_proxy', None):
            raise RuntimeError('a client-specific MQTT proxy is not allowed')
        if read_mqtt_security() != self.guard.runtime._mqtt_security:
            raise RuntimeError('simulated MQTT security configuration changed')
        try:
            if self._client.connect('127.0.0.1', self.robot.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('isolated simulated broker connect failed')
            if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('simulated MQTT loop failed')
            if not self._ready.wait(5) or self._failed.is_set() or not self.robot.ready:
                raise RuntimeError('simulated reroute receiver not ready')
            self._worker = threading.Thread(target=self._run, name='sim-reroute-receiver', daemon=True)
            self._worker.start()
            return self
        except BaseException:
            self.stop()
            raise

    def stop(self):
        with self._lifecycle_lock:
            return self._stop_locked()

    def _stop_locked(self):
        if self._stopping.is_set():
            if self._worker is not None:
                self._worker.join(timeout=10)
                if self._worker.is_alive():
                    raise RuntimeError('simulated reroute worker did not stop')
            if self._network_cleanup_failed:
                raise RuntimeError('simulated reroute network cleanup failed')
            reset_stopped_client(self._client)
            return
        self._stopping.set()
        self._ready.clear()
        errors = []
        loop_stopped = False
        for index, action in enumerate((self._client.disconnect, self._client.loop_stop)):
            try:
                action()
                if index == 1:
                    loop_stopped = True
            except BaseException as exc:
                errors.append(exc)
        if self._worker is not None:
            self._worker.join(timeout=10)
            if self._worker.is_alive():
                raise RuntimeError('simulated reroute worker did not stop')
        if loop_stopped:
            try:
                reset_stopped_client(self._client)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            self._network_cleanup_failed = True
            raise RuntimeError('simulated reroute network cleanup failed') from errors[0]
