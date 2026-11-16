"""MQTT adapter for one explicitly isolated virtual robot.

This is not a robot controller. It does not connect to non-loopback hosts or
operate hardware. Loopback and a sim ID do not by themselves prove broker
isolation: the caller must provide its own independently started broker.
"""
import json
import queue
import threading
import urllib.request
from contextlib import nullcontext

import paho.mqtt.client as mqtt

from server.mqtt_security import configure_mqtt_client, read_mqtt_security
from sim.event_inbox import _event
from sim.order_executor import SimOrderExecutor


MAX_QUEUED_ORDERS = 32
MAX_ORDER_BYTES = 256 * 1024
ORDER_TOPIC = 'agv/sim-r1/order'
EVENT_TOPIC = 'agv/sim-r1/event'


def _verify_no_proxy():
    # PySocks can redirect even a 127.0.0.1 connection outside this PC.
    if mqtt.socks is None:
        return
    default = mqtt.socks.get_default_proxy()
    if default and default[0] is not None:
        raise RuntimeError('a process-wide SOCKS proxy is not allowed')
    if urllib.request.getproxies().get('mqtt') and not urllib.request.proxy_bypass('127.0.0.1'):
        raise RuntimeError('an MQTT proxy is not allowed for the isolated broker')


class SimMqttOrderRobot:
    """Receive only sim-r1 orders; callers own ticks and recovery decisions."""

    def __init__(self, executor, port, *, order_authorizer=None, authorization_guard=None):
        if not isinstance(executor, SimOrderExecutor) or executor._inbox.robot_id != 'sim-r1':
            raise ValueError('the sim-r1 executor is required')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid isolated broker port')
        if ((order_authorizer is not None and not callable(order_authorizer))
                or (authorization_guard is not None and not callable(authorization_guard))):
            raise ValueError('order authorization hooks must be callable')
        self.executor, self.port = executor, port
        self._order_authorizer, self._authorization_guard = order_authorizer, authorization_guard
        self._queue = queue.Queue(maxsize=MAX_QUEUED_ORDERS)
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._lock = threading.RLock()
        self._started = self._stopping = self._stopped = False
        self._pending_mid = None
        self._event_segments = set()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id='sim-r1-order-executor',
                                   protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        configure_mqtt_client(self._client, *read_mqtt_security())
        self._client.manual_ack_set(True)
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    def _fail(self):
        self._failed.set()
        self._ready.clear()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if self._failed.is_set() or self._stopping or reason_code.is_failure:
            self._fail()
            return
        try:
            rc, mid = client.subscribe(ORDER_TOPIC, qos=1)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                self._fail()
                return
            self._pending_mid = mid
        except Exception:
            self._fail()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if (self._failed.is_set() or self._stopping or mid != self._pending_mid
                or len(reason_codes) != 1 or reason_codes[0].is_failure
                or reason_codes[0].value != 1):
            self._fail()
            return
        self._ready.set()

    def _on_message(self, client, userdata, message):
        # The network thread never validates JSON, touches SQLite or moves pose.
        if self._failed.is_set() or self._stopping:
            return
        if (not self._ready.is_set() or message.topic != ORDER_TOPIC or message.qos != 1
                or message.retain or not isinstance(message.payload, bytes)
                or len(message.payload) > MAX_ORDER_BYTES):
            self._fail()
            return
        try:
            self._queue.put_nowait((message.payload, message.mid, message.qos))
        except queue.Full:
            self._fail()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self._ready.clear()
        if not self._stopping:
            self._fail()

    @property
    def ready(self):
        return (self._started and not self._stopped and not self._failed.is_set()
                and self._ready.is_set() and self._client.is_connected())

    def start(self):
        with self._lock:
            if self._started or self._stopped:
                raise RuntimeError('the simulated MQTT robot cannot be restarted')
            self._started = True
            try:
                _verify_no_proxy()
                if self._client.connect('127.0.0.1', self.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('isolated broker connection failed')
                if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('isolated MQTT loop failed')
                if not self._ready.wait(10) or not self.ready:
                    raise RuntimeError('simulated order subscription is not ready')
            except Exception:
                self._fail()
                self._stopping = True
                try:
                    self._client.disconnect()
                finally:
                    self._client.loop_stop()
                    self._stopped = True
                raise
        return self

    def stop(self):
        with self._lock:
            if self._stopped:
                return
            self._stopping = True
            self._ready.clear()
            try:
                if self._started:
                    self._client.disconnect()
            finally:
                if self._started:
                    self._client.loop_stop()
                self._stopped = True

    def tick(self, dt_seconds):
        with self._lock:
            if not self.ready:
                raise RuntimeError('simulated MQTT robot is unavailable')
            try:
                while True:
                    try:
                        payload, mid, qos = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if not self.ready:
                        raise RuntimeError('simulated MQTT connection lost')
                    guard = self._authorization_guard(payload) if self._authorization_guard else nullcontext()
                    with guard:
                        if not self.ready:
                            raise RuntimeError('simulated MQTT connection lost')
                        if self._order_authorizer is not None and self._order_authorizer(payload) is not True:
                            raise ValueError('order does not match a reserved simulated segment')
                        self.executor.accept(payload)
                    if not self.ready or self._client.ack(mid, qos) != mqtt.MQTT_ERR_SUCCESS:
                        raise RuntimeError('simulated order acknowledgement failed')
                if not self.ready:
                    raise RuntimeError('simulated MQTT connection lost')
                state = self.executor.snapshot()
                if (state.get('status') == 'moving' and type(dt_seconds) in (int, float)
                        and dt_seconds > 0 and (self._authorization_guard or self._order_authorizer)):
                    payload = json.dumps(state['order'], ensure_ascii=False, allow_nan=False,
                                         separators=(',', ':')).encode('utf-8')
                    guard = self._authorization_guard(payload) if self._authorization_guard else nullcontext()
                    with guard:
                        if not self.ready:
                            raise RuntimeError('simulated MQTT connection lost')
                        if self._order_authorizer is not None and self._order_authorizer(payload) is not True:
                            raise ValueError('reserved simulated segment is no longer current')
                        return self.executor.tick(dt_seconds)
                return self.executor.tick(dt_seconds)
            except Exception as exc:
                self._fail()
                raise RuntimeError('simulated order processing stopped') from exc

    def publish_event(self, event):
        """Publish one matching virtual stop event; PUBACK is not execution proof."""
        with self._lock:
            if not self.ready:
                raise RuntimeError('simulated MQTT robot is unavailable')
            try:
                checked, encoded, _digest = _event(event)
                state = self.executor.snapshot()
                receipt, order = state['receipt'], state['order']
                if (state['status'] != 'stopped' or not receipt or not order
                        or checked['order_id'] != receipt['order_id']
                        or checked['seq'] != receipt['seq']
                        or checked['map_id'] != order['map_id']
                        or checked['stopped_at'] != state['current_node']
                        or checked['stopped_at'] != order['released_until']):
                    raise ValueError('event does not match the simulated stopped segment')
                segment = (receipt['order_id'], receipt['seq'])
                if segment in self._event_segments:
                    raise RuntimeError('a virtual event was already attempted for this segment')
                # Claim before network I/O: an uncertain publish is never repeated.
                self._event_segments.add(segment)
                info = self._client.publish(EVENT_TOPIC, encoded, qos=1, retain=False)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('virtual event queueing failed')
                info.wait_for_publish(timeout=5)
                if not self.ready or info.is_published() is not True:
                    raise RuntimeError('virtual event publication was not confirmed')
                return {'event_id': checked['event_id'], 'published': True}
            except Exception as exc:
                self._fail()
                raise RuntimeError('simulated event publication stopped') from exc
