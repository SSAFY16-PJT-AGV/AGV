"""Loopback-only MQTT adapter for the isolated signed hold exchange."""
import json
import threading
from contextlib import contextmanager

import paho.mqtt.client as mqtt

from server.physical_reconcile import MAX_BYTES, PhysicalReconciler, _hex
from server.physical_evidence_observer import require_direct_mqtt_client


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate reconciliation field')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('non-finite reconciliation value')


class PhysicalReconcileMqtt:
    """Single-owner local MQTT subscriber; no automatic reconnect or movement."""
    def __init__(self, service, *, host='127.0.0.1', port, client=None, isolated=False, timeout=5):
        if isolated is not True or not isinstance(service, PhysicalReconciler) or host != '127.0.0.1':
            raise ValueError('explicit isolated loopback reconciliation is required')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid isolated MQTT port')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 10:
            raise ValueError('invalid MQTT timeout')
        self.service, self.host, self.port, self.timeout = service, host, port, float(timeout)
        self.client = client or mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        if getattr(self.client, '_reconnect_on_failure', None) is not False:
            raise ValueError('automatic reconnect must be disabled')
        self._ready = threading.Event()
        self._done = threading.Event()
        self._mid = None
        self.last_error = None
        self.last_result = None
        self._results_lock = threading.RLock()
        self._requested = set()
        self._results = {}
        self._started = False

    def _connect(self, client, _userdata, _flags, reason, _properties):
        if reason.is_failure:
            self.last_error = 'connection rejected'
            self._done.set()
            return
        rc, mid = client.subscribe('agv/+/reconcile/response', qos=1)
        if rc != mqtt.MQTT_ERR_SUCCESS or type(mid) is not int:
            self.last_error = 'subscription failed'
            self._done.set()
            return
        self._mid = mid

    def _subscribe(self, _client, _userdata, mid, reasons, _properties):
        if mid == self._mid and len(reasons) == 1 and not reasons[0].is_failure and reasons[0].value == 1:
            self._ready.set()
        else:
            self.last_error = 'subscription rejected'
        self._done.set()

    def _disconnect(self, _client, _userdata, _flags, _reason, _properties):
        self._ready.clear()
        self._done.set()

    def start(self):
        if self._started:
            raise RuntimeError('adapter is one-shot')
        self._started = True
        self.client.on_connect, self.client.on_subscribe = self._connect, self._subscribe
        self.client.on_message, self.client.on_disconnect = self._message, self._disconnect
        try:
            require_direct_mqtt_client(self.client)
            if self.client.connect(self.host, self.port, keepalive=10) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('MQTT connect failed')
            if self.client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('MQTT loop failed')
            if not self._done.wait(self.timeout) or not self._ready.is_set():
                raise RuntimeError('MQTT response subscription unavailable')
        except BaseException:
            self.stop()
            raise
        return self

    def _queue(self, nonce, kind):
        if not self._ready.is_set():
            raise RuntimeError('reconciliation subscriber unavailable')
        with self.service.publication(nonce, kind) as (robot, payload):
            info = self.client.publish('agv/%s/reconcile/%s' %
                (robot, 'request' if kind == 'probe' else 'commit'), payload, qos=1, retain=False)
            if getattr(info, 'rc', None) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('reconciliation publish is uncertain')
        return info

    def request(self, robot, task, session, *, event_id=None, event_digest=None):
        if not self._ready.is_set():
            raise RuntimeError('reconciliation subscriber unavailable')
        with self._results_lock:
            if len(self._requested) >= 256:
                raise RuntimeError('reconciliation observer request capacity reached')
            probe = self.service.begin(robot, task, session, event_id=event_id, event_digest=event_digest)
            self._requested.add(probe['nonce'])
            self._queue(probe['nonce'], 'probe')
            return probe['nonce']

    def result_for(self, nonce):
        """Read a specific completed exchange; None means no known result."""
        if not _hex(nonce):
            raise ValueError('invalid reconciliation nonce')
        with self._results_lock:
            result = self._results.get(nonce)
            return None if result is None else dict(result)

    def _message(self, _client, _userdata, message):
        try:
            parts = message.topic.split('/')
            if (len(parts) != 4 or parts[0] != 'agv' or parts[2:] != ['reconcile', 'response']
                    or parts[1] not in self.service._keys or message.retain is not False
                    or type(message.qos) is not int or message.qos != 1
                    or not isinstance(message.payload, bytes) or len(message.payload) > MAX_BYTES):
                raise ValueError('invalid response envelope')
            data = json.loads(message.payload.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
            if not isinstance(data, dict) or data.get('robot_id') != parts[1]:
                raise ValueError('response topic mismatch')
            with self._results_lock:
                if data.get('nonce') not in self._requested:
                    raise ValueError('response does not belong to this observer')
            if data.get('kind') == 'snapshot':
                self.service.accept_snapshot(data)
                self._queue(data['nonce'], 'commit')
            elif data.get('kind') == 'commit_ack':
                result = self.service.accept_ack(data)
                with self._results_lock:
                    self._results[data['nonce']] = dict(result)
                    self.last_result = dict(result)
            else:
                raise ValueError('unknown response kind')
        except Exception as exc:
            # Never log wire payloads, MAC keys, or user-provided text.
            self.last_error = type(exc).__name__

    def stop(self):
        self._ready.clear()
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()


@contextmanager
def isolated_gate_reconciliation(gate, inbox, watcher, robot_keys, *, port, isolated=False, timeout=5,
                                 client=None):
    """Use an already active gate and inbox only for loopback hold evidence.

    The caller owns the gate, database and watcher. This does not enable the
    physical runtime, event progression, proof generation, or automatic motion.
    An explicitly configured client may supply local broker TLS credentials;
    direct loopback checks and the no-reconnect requirement still apply.
    """
    from server.recovery import RecoveryGate
    from server.physical_evidence_observer import PhysicalEvidenceInbox, PhysicalEvidenceWatcher
    if (isolated is not True or not isinstance(gate, RecoveryGate)
            or not isinstance(inbox, PhysicalEvidenceInbox)
            or not isinstance(watcher, PhysicalEvidenceWatcher)):
        raise ValueError('explicit isolated gate and evidence observer required')
    with gate._operation_lock:
        if (gate._physical_watcher is not watcher or watcher._runtime.gate is not gate
                or watcher._host != '127.0.0.1' or watcher._port != port
                or not watcher._mqtt_started or not watcher._connected.is_set()
                or not watcher._subscription_done.is_set()):
            raise ValueError('the gate must own the live loopback observer')
        service = PhysicalReconciler(gate, inbox, robot_keys, isolated=True)
        adapter = PhysicalReconcileMqtt(service, host='127.0.0.1', port=port,
                                        isolated=True, timeout=timeout, client=client)
    adapter.start()
    try:
        yield adapter
    finally:
        adapter.stop()
