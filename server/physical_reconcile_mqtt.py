"""Loopback-only MQTT adapter for the isolated signed hold exchange."""
import json
import threading
from contextlib import contextmanager

import paho.mqtt.client as mqtt

from server.physical_reconcile import MAX_BYTES, PhysicalReconciler


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
        probe = self.service.begin(robot, task, session, event_id=event_id, event_digest=event_digest)
        self._queue(probe['nonce'], 'probe')
        return probe['nonce']

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
            if data.get('kind') == 'snapshot':
                self.service.accept_snapshot(data)
                self._queue(data['nonce'], 'commit')
            elif data.get('kind') == 'commit_ack':
                self.last_result = self.service.accept_ack(data)
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
def isolated_runtime_reconciliation(runtime, robot_keys, *, isolated=False, timeout=5):
    """Attach an explicitly opted-in test exchange to an existing runtime.

    The caller owns the context lifetime. The runtime still blocks physical
    startup, and this adapter never unblocks its gate or enables actuators.
    """
    from server.sim_runtime import SimRuntime
    if isolated is not True or not isinstance(runtime, SimRuntime):
        raise ValueError('explicit isolated runtime required')
    with runtime._lock:
        runtime._ready()
        if (runtime.host != '127.0.0.1' or runtime.physical_watcher is None
                or runtime.gate._physical_watcher is not runtime.physical_watcher
                or runtime.physical_watcher._runtime is not runtime):
            raise ValueError('runtime must own the live physical watcher and gate')
        service = PhysicalReconciler(runtime.gate, runtime.inbox, robot_keys, isolated=True)
        adapter = PhysicalReconcileMqtt(service, host=runtime.host, port=runtime.port,
                                        isolated=True, timeout=timeout)
    adapter.start()
    try:
        yield adapter
    finally:
        adapter.stop()
