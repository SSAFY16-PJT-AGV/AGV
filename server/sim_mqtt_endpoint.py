"""Bounded loopback-only worker for explicit isolated simulation handshakes."""
import queue
import secrets
import threading
from contextlib import nullcontext

import paho.mqtt.client as mqtt

from server.mqtt_security import configure_mqtt_client, read_mqtt_security
from server.sim_mqtt_cleanup import reset_stopped_client
from sim.mqtt_order_robot import _verify_no_proxy


class IsolatedMqttEndpoint:
    """Run a checked callback off the Paho thread; never reconnect or retry."""
    def __init__(self, port, topics, response_topics, processor, *, expected_security, effect_guard=None,
                 stop_forbidden=None):
        if (type(port) is not int or not 1 <= port <= 65535 or not isinstance(topics, (set, frozenset))
                or not topics or not isinstance(response_topics, (set, frozenset)) or not callable(processor)
                or any(not isinstance(x, str) or not x.startswith('agv/sim-') or '+' in x or '#' in x
                       for x in topics | response_topics)
                or (effect_guard is not None and not callable(effect_guard))
                or (stop_forbidden is not None and not callable(stop_forbidden))):
            raise ValueError('explicit isolated simulation topics and processor required')
        self.port, self.topics, self.response_topics, self.processor = port, frozenset(topics), frozenset(response_topics), processor
        self._security = expected_security
        self._effect_guard = effect_guard or nullcontext
        self._stop_forbidden = stop_forbidden
        self._queue = queue.Queue(maxsize=32)
        self._ready, self._failed, self._stopping = threading.Event(), threading.Event(), threading.Event()
        self._started, self._worker, self._mid = False, None, None
        self._lifecycle_lock = threading.RLock()
        self._connection_fence = threading.RLock()
        self._network_cleanup_failed = False
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
            client_id='sim-handshake-' + secrets.token_hex(12), protocol=mqtt.MQTTv311,
            reconnect_on_failure=False)
        configure_mqtt_client(self._client, *read_mqtt_security())
        self._client.manual_ack_set(True)
        self._client.on_connect, self._client.on_subscribe = self._connect, self._subscribe
        self._client.on_message, self._client.on_disconnect = self._message, self._disconnect

    def _connect(self, client, _userdata, _flags, reason, _properties):
        if reason.is_failure or self._stopping.is_set():
            self._failed.set()
            return
        result, self._mid = client.subscribe([(topic, 1) for topic in sorted(self.topics)])
        if result != mqtt.MQTT_ERR_SUCCESS:
            self._failed.set()

    def _subscribe(self, _client, _userdata, mid, reasons, _properties):
        if (self._stopping.is_set() or mid != self._mid or len(reasons) != len(self.topics)
                or any(reason.is_failure or reason.value != 1 for reason in reasons)):
            self._failed.set()
        else:
            self._ready.set()

    def _message(self, _client, _userdata, message):
        if (self._stopping.is_set() or self._failed.is_set() or message.topic not in self.topics
                or message.qos != 1 or message.retain or not isinstance(message.payload, bytes)
                or len(message.payload) > 256 * 1024):
            self._failed.set()
            return
        try:
            self._queue.put_nowait((message.topic, message.payload, message.mid, message.qos))
        except queue.Full:
            self._failed.set()

    def _disconnect(self, _client, _userdata, _flags, _reason, _properties):
        with self._connection_fence:
            self._ready.clear()
            if not self._stopping.is_set():
                self._failed.set()

    def _run(self):
        while not self._stopping.is_set() and not self._failed.is_set():
            try:
                topic, payload, mid, qos = self._queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                with self._effect_guard(), self._connection_fence:
                    if self._stopping.is_set() or self._failed.is_set() or not self._ready.is_set():
                        raise RuntimeError('simulated handshake stopped before processing')
                    response = self.processor(topic, payload)
                    if (self._stopping.is_set() or self._failed.is_set() or not self._client.is_connected()
                            or self._client.ack(mid, qos) != mqtt.MQTT_ERR_SUCCESS):
                        raise RuntimeError('simulated handshake acknowledgement failed')
                    if response is not None:
                        if (not isinstance(response, tuple) or len(response) != 2 or response[0] not in self.response_topics
                                or not isinstance(response[1], bytes) or len(response[1]) > 256 * 1024):
                            raise ValueError('invalid simulated handshake response')
                        if self._stopping.is_set() or not self._client.is_connected():
                            raise RuntimeError('simulated handshake connection changed')
                        info = self._client.publish(response[0], response[1], qos=1, retain=False)
                        if info.rc != mqtt.MQTT_ERR_SUCCESS:
                            raise RuntimeError('simulated handshake response enqueue failed')
                        info.wait_for_publish(timeout=5)
                        if not info.is_published() or not self._client.is_connected():
                            raise RuntimeError('simulated handshake response delivery is uncertain')
            except Exception:
                self._failed.set()
                self._ready.clear()
                return

    def start(self):
        with self._lifecycle_lock:
            if self._started or self._stopping.is_set():
                raise RuntimeError('simulated endpoint cannot be restarted')
            self._started = True
            _verify_no_proxy()
            if getattr(self._client, '_proxy', None) or read_mqtt_security() != self._security:
                raise RuntimeError('simulation MQTT proxy or security configuration changed')
            try:
                if self._client.connect('127.0.0.1', self.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('isolated simulated broker connect failed')
                if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('simulated MQTT loop failed')
                if not self._ready.wait(5) or self._failed.is_set():
                    raise RuntimeError('simulated endpoint not ready')
                self._worker = threading.Thread(target=self._run, name='sim-handshake-worker', daemon=True)
                self._worker.start()
                return self
            except BaseException:
                self.stop()
                raise

    def stop(self):
        current = threading.current_thread()
        if (current is self._worker or current is getattr(self._client, '_thread', None)
                or (self._stop_forbidden is not None and self._stop_forbidden())):
            raise RuntimeError('cannot stop a simulated endpoint while holding its execution locks')
        with self._lifecycle_lock:
            if self._stopping.is_set():
                if self._worker is not None:
                    self._worker.join(timeout=10)
                    if self._worker.is_alive():
                        raise RuntimeError('simulated endpoint worker did not stop')
                if self._network_cleanup_failed:
                    raise RuntimeError('simulated endpoint network cleanup failed')
                reset_stopped_client(self._client)
                return
            self._stopping.set()
            self._ready.clear()
            errors, loop_stopped = [], False
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
                    raise RuntimeError('simulated endpoint worker did not stop')
            if loop_stopped:
                try:
                    reset_stopped_client(self._client)
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                self._network_cleanup_failed = True
                raise RuntimeError('simulated endpoint network cleanup failed') from errors[0]
