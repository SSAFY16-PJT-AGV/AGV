"""Opt-in loopback receiver for untrusted proposed simulated responses only.

Never connects to an address other than IPv4 loopback; loopback and `sim-`
IDs alone do not establish broker isolation or authenticate a publisher.
No SimRuntime, allocation, motion, recovery or physical-robot integration.
"""
import json
import secrets
import threading

import paho.mqtt.client as mqtt

from server.mqtt_security import configure_mqtt_client, read_mqtt_security
from server.order_publisher import _topic
from server.sim_command_observations import SimCommandObservations
from sim.event_receiver import _pairs, _constant, _finite
from sim.mqtt_order_robot import _verify_no_proxy


MAX_RESPONSE_BYTES = 64 * 1024


def _configure_client(client):
    configure_mqtt_client(client, *read_mqtt_security())


class SimCommandObservationReceiver:
    """Dedicated non-reconnecting clean-session listener for simulated claims."""

    def __init__(self, observations, robots, port, *, simulation=False, timeout=10):
        if simulation is not True or type(observations) is not SimCommandObservations:
            raise ValueError('explicit isolated simulation observations required')
        if (not isinstance(robots, (tuple, list)) or not robots or len(robots) > 128
                or len(set(robots)) != len(robots)):
            raise ValueError('explicit unique simulated robots required')
        for robot in robots:
            _topic(robot)
        if set(robots) != set(observations._sessions):
            raise ValueError('receiver robots must match observation sessions')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid loopback broker port')
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError('invalid connection timeout')
        self.observations, self.port, self.timeout = observations, port, timeout
        self._topics = {'sim-observation/{}/order-response'.format(robot): robot for robot in robots}
        self._lock = threading.RLock()
        self._started_event = threading.Event()
        self._started = self._ready = self._closed = self._connected = False
        self._mid = None
        self.last_result = None
        self.last_error = None
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id='sim-observation-' + secrets.token_hex(16),
                                   clean_session=True, protocol=mqtt.MQTTv311,
                                   reconnect_on_failure=False)
        _configure_client(self._client)
        self._client.manual_ack_set(True)
        if getattr(self._client, '_manual_ack', None) is not True:
            raise ValueError('manual acknowledgment is required')
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    @property
    def ready(self):
        with self._lock:
            return self._ready and not self._closed

    def _fail(self, category):
        with self._lock:
            self._ready, self._closed, self._connected = False, True, False
            self.last_error = category
            self._started_event.set()
            try:
                self._client.disconnect()
            except Exception:
                pass

    def _on_connect(self, client, _userdata, _flags, reason, _properties):
        with self._lock:
            if (self._closed or self._connected or client is not self._client
                    or getattr(reason, 'is_failure', None) is not False):
                self._fail('connection rejected')
                return
            self._connected = True
            try:
                rc, self._mid = client.subscribe([(topic, 1) for topic in self._topics])
                if rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('subscription not queued')
            except Exception:
                self._fail('subscription failed')

    def _on_subscribe(self, client, _userdata, mid, reasons, _properties):
        with self._lock:
            try:
                valid = (not self._closed and client is self._client and mid == self._mid
                         and len(reasons) == len(self._topics)
                         and all(not item.is_failure and item.value == 1 for item in reasons))
            except (TypeError, AttributeError):
                valid = False
            if not valid:
                self._fail('subscription rejected')
                return
            self._mid = None
            self._ready = True
            self._started_event.set()

    def _on_message(self, client, _userdata, message):
        with self._lock:
            try:
                if self._closed or not self._ready or client is not self._client:
                    raise ValueError('receiver is not ready')
                robot = self._topics.get(message.topic)
                if (robot is None or type(message.qos) is not int or message.qos != 1
                        or message.retain is not False or type(message.payload) is not bytes
                        or len(message.payload) > MAX_RESPONSE_BYTES
                        or type(message.mid) is not int or not 1 <= message.mid <= 65535):
                    raise ValueError('invalid response envelope')
                response = json.loads(message.payload.decode('utf-8'), object_pairs_hook=_pairs,
                                      parse_constant=_constant, parse_float=_finite)
                result = self.observations.record(robot, response, retained=False)
                if (not isinstance(result, dict) or result.get('status') not in ('recorded', 'duplicate')
                        or result.get('outcome') not in ('accepted', 'rejected')):
                    raise ValueError('observation was not committed')
                if client.ack(message.mid, 1) != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('response acknowledgement failed')
                self.last_result = dict(result)
            except Exception:
                self._fail('response rejected or unavailable')

    def _on_disconnect(self, client, _userdata, _flags, _reason, _properties):
        with self._lock:
            if client is self._client and not self._closed:
                self._fail('connection lost')

    def start(self):
        with self._lock:
            if self._started or self._closed:
                raise RuntimeError('receiver cannot be restarted')
            self._started = True
        try:
            _verify_no_proxy()
            if self._client.connect('127.0.0.1', self.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('loopback connection failed')
            if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('network loop failed')
            if not self._started_event.wait(self.timeout) or not self.ready:
                raise RuntimeError('response subscription not ready')
        except BaseException:
            self.stop()
            raise
        return self

    def stop(self):
        with self._lock:
            if self._closed and not self._started:
                return
            self._closed, self._ready = True, False
            started, self._started = self._started, False
            self._started_event.set()
        if started:
            try:
                self._client.disconnect()
            finally:
                self._client.loop_stop()
