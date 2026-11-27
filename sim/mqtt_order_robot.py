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


class _ConnectionLost(RuntimeError):
    """A readiness check failed before a new simulated side effect began."""


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
    """Receive one isolated robot's orders; callers own ticks and recovery."""

    def __init__(self, executor, port, *, order_authorizer=None, authorization_guard=None):
        if (not isinstance(executor, SimOrderExecutor)
                or executor._inbox.robot_id not in ('sim-r1', 'sim-r2')):
            raise ValueError('a sim-r1 or sim-r2 executor is required')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid isolated broker port')
        if ((order_authorizer is not None and not callable(order_authorizer))
                or (authorization_guard is not None and not callable(authorization_guard))):
            raise ValueError('order authorization hooks must be callable')
        self.executor, self.port = executor, port
        self.robot_id = executor._inbox.robot_id
        self.order_topic = 'agv/{}/order'.format(self.robot_id)
        self.event_topic = 'agv/{}/event'.format(self.robot_id)
        self._order_authorizer, self._authorization_guard = order_authorizer, authorization_guard
        self._queue = queue.Queue(maxsize=MAX_QUEUED_ORDERS)
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._lock = threading.RLock()
        # Disconnect completion and an already-entered geometry update are
        # serialized separately from _lock: Paho's network callback must not
        # need _lock while reconnect/stop waits for its network thread.
        self._motion_fence = threading.Lock()
        self._started = self._stopping = self._stopped = False
        self._recoverable_disconnect = False
        self._event_uncertain = False
        self._pending_mid = None
        self._event_segments = set()
        self._reroute_driver = None
        self._client = self._new_client()

    def _new_client(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id='{}-order-executor'.format(self.robot_id),
                                   protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        configure_mqtt_client(client, *read_mqtt_security())
        client.manual_ack_set(True)
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        return client

    def _fail(self):
        self._recoverable_disconnect = False
        self._failed.set()
        self._ready.clear()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if client is not self._client:
            return
        if self._failed.is_set() or self._stopping or reason_code.is_failure:
            self._fail()
            return
        try:
            rc, mid = client.subscribe(self.order_topic, qos=1)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                self._fail()
                return
            self._pending_mid = mid
        except Exception:
            self._fail()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if client is not self._client:
            return
        if (self._failed.is_set() or self._stopping or mid != self._pending_mid
                or len(reason_codes) != 1 or reason_codes[0].is_failure
                or reason_codes[0].value != 1):
            self._fail()
            return
        self._ready.set()

    def _on_message(self, client, userdata, message):
        # The network thread never validates JSON, touches SQLite or moves pose.
        if client is not self._client:
            return
        if self._failed.is_set() or self._stopping:
            return
        if (not self._ready.is_set() or message.topic != self.order_topic or message.qos != 1
                or message.retain or not isinstance(message.payload, bytes)
                or len(message.payload) > MAX_ORDER_BYTES):
            self._fail()
            return
        try:
            self._queue.put_nowait((message.payload, message.mid, message.qos))
        except queue.Full:
            self._fail()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        with self._motion_fence:
            if client is not self._client:
                return
            self._ready.clear()
            if not self._stopping:
                recoverable = self._recoverable_disconnect or not self._failed.is_set()
                self._fail()
                self._recoverable_disconnect = recoverable

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

    def _checkpoint(self):
        # The caller holds the robot lock; the executor lock also prevents an
        # accidental direct tick from changing its cursor while copying it.
        with self.executor._lock:
            self.executor._verify_field_execution()
            return (id(self), id(self.executor), id(self.executor._inbox),
                    json.dumps(self.executor.snapshot(), sort_keys=True, allow_nan=False,
                               separators=(',', ':')),
                    self.executor._waypoints, self.executor._next_waypoint,
                    self.executor._waypoint_visits, self.executor._visit_index,
                    self.executor._release_index, self.executor._segment_start_index,
                    self.executor._field_incoming_edge, self.executor._field_guard_incoming,
                    self.executor._field_order_entry,
                    self.executor._field_map_canonical,
                    self.executor._field_route_edges,
                    json.dumps(self.executor._field_map, sort_keys=True, allow_nan=False,
                               separators=(',', ':')),
                    frozenset(self._event_segments))

    def recovery_checkpoint(self):
        """Capture in-process simulated state after an observed disconnect."""
        with self._lock:
            if (not self._started or self._stopped or not self._failed.is_set()
                    or not self._recoverable_disconnect or self._event_uncertain
                    or not self._queue.empty()):
                raise RuntimeError('simulated state cannot be checkpointed')
            return self._checkpoint()

    def connected_recovery_checkpoint(self):
        """Capture a connected simulator already frozen by its caller.

        The caller must stop all fleet ticks and check both readiness and this
        checkpoint again before allowing any movement. This does not establish
        physical position or authorize a robot to move.
        """
        with self._lock:
            if not self.ready or self._event_uncertain or not self._queue.empty():
                raise RuntimeError('connected simulated state cannot be checkpointed')
            return self._checkpoint()

    def check_recovery_checkpoint(self, checkpoint):
        """Compare a checkpoint while the caller keeps simulator ticks stopped."""
        with self._lock:
            if self._stopped or self._event_uncertain or not self._queue.empty():
                return False
            try:
                return self._checkpoint() == checkpoint
            except (ValueError, TypeError, OverflowError, RecursionError):
                return False

    def reconnect(self, *, authorization_guard):
        """Reconnect the same simulated executor after a known MQTT disconnect.

        A fresh server-side authorization guard is mandatory on every motion
        tick. Unprocessed messages and uncertain event attempts require manual
        reconciliation, not automatic replay.
        """
        if not callable(authorization_guard):
            raise ValueError('a server-side authorization guard is required')
        with self._lock:
            if (not self._started or self._stopped or not self._failed.is_set()
                    or not self._recoverable_disconnect or self._event_uncertain):
                raise RuntimeError('simulated robot is not eligible for reconnect')
            self._stopping = True
            old_client = self._client
            try:
                old_client.disconnect()
            finally:
                old_client.loop_stop()
            if not self._queue.empty():
                self._fail()
                raise RuntimeError('unprocessed simulated orders require reconciliation')
            self._authorization_guard = authorization_guard
            self._order_authorizer = None
            self._client = self._new_client()
            self._pending_mid = None
            self._failed.clear()
            self._recoverable_disconnect = False
            self._stopping = False
            try:
                _verify_no_proxy()
                if self._client.connect('127.0.0.1', self.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('isolated broker connection failed')
                if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('isolated MQTT loop failed')
                if not self._ready.wait(10) or not self.ready:
                    raise RuntimeError('simulated order subscription is not ready')
            except Exception:
                retryable = not self._failed.is_set() or self._recoverable_disconnect
                self._fail()
                self._stopping = True
                try:
                    self._client.disconnect()
                finally:
                    self._client.loop_stop()
                self._recoverable_disconnect = (retryable and self._queue.empty()
                                                and not self._event_uncertain)
                raise
        return self

    def _tick_executor(self, dt_seconds):
        # A callback may detect socket failure while a simulation update is
        # already underway. Such an update completes before disconnect is
        # finalized; no later update can begin without a live connection.
        with self._motion_fence:
            if not self.ready:
                raise _ConnectionLost('simulated MQTT connection lost')
            return self.executor.tick(dt_seconds)

    def start_reroute(self, execution, payload, *, guard):
        """Install one guarded virtual generation and its per-tick driver."""
        from sim.reroute_execution import SimRerouteExecution
        if (type(execution) is not SimRerouteExecution or execution.executor is not self.executor
                or not isinstance(payload, bytes)
                or getattr(getattr(guard, 'robot_receiver', None), 'robot', None) is not self):
            raise ValueError('bound simulated robot, execution and guard required')
        try:
            result = execution.start(payload, guard=guard, bind_robot=self)
            if result.get('new') is False and result.get('state') in ('moving', 'stopped'):
                return result
            if result.get('new') is not True or result.get('state') != 'moving':
                raise RuntimeError('an existing generation cannot be resumed automatically')
            return result
        except Exception:
            with self._lock:
                self._fail()
            raise

    def tick(self, dt_seconds):
        with self._lock:
            if not self.ready:
                raise RuntimeError('simulated MQTT robot is unavailable')
            driver = self._reroute_driver
            if driver is None:
                return self._tick_regular_locked(dt_seconds)
            try:
                state = self.executor.snapshot()
                if self._queue.empty() and state.get('status') == 'stopped':
                    return state
                if not self._queue.empty():
                    from sim.order_inbox import _decode
                    payload, mid, qos = self._queue.get_nowait()
                    order, _canonical, _digest = _decode(payload, self.robot_id, self.executor._inbox.map_id)
                    current = state.get('order') or {}
                    if (order['seq'] != 1 or order['order_id'] == current.get('order_id')
                            or state['status'] != 'stopped' or state['current_node'] != current['nodes'][-1]):
                        raise ValueError('generation continuation requires a separate guarded driver')
                    guard = self._authorization_guard(payload) if self._authorization_guard else nullcontext()
                    with guard:
                        if not self.ready:
                            raise _ConnectionLost('simulated MQTT connection lost')
                        if self._order_authorizer is not None and self._order_authorizer(payload) is not True:
                            raise ValueError('new order does not match a reserved segment')
                        self.executor.accept(payload)
                    if self.executor._reroute_transition is not None or not self.ready:
                        raise ValueError('previous generation did not close')
                    if self._client.ack(mid, qos) != mqtt.MQTT_ERR_SUCCESS:
                        raise RuntimeError('simulated order acknowledgement failed')
                    self._reroute_driver = None
                    return self._tick_regular_locked(dt_seconds)
            except Exception as exc:
                self._fail()
                raise RuntimeError('simulated generation processing stopped') from exc
        # Never hold robot._lock while acquiring the generation guard's outer
        # lifecycle/connection locks. The guard reacquires robot._lock and
        # checks the identical driver immediately before each virtual tick.
        execution, payload, generation_guard = driver
        try:
            return execution.tick(payload, dt_seconds, guard=generation_guard, expected_driver=driver)
        except Exception as exc:
            with self._lock:
                self._fail()
            raise RuntimeError('simulated generation processing stopped') from exc

    def _tick_regular_locked(self, dt_seconds):
        """Run the unchanged ordinary-order path while robot._lock is held."""
        if not self.ready:
            raise RuntimeError('simulated MQTT robot is unavailable')
        if self._reroute_driver is not None:
            raise RuntimeError('generation driver must use its own guard')
        processing_order = False
        try:
            while True:
                try:
                    payload, mid, qos = self._queue.get_nowait()
                except queue.Empty:
                    break
                processing_order = True
                if not self.ready:
                    raise _ConnectionLost('simulated MQTT connection lost')
                guard = self._authorization_guard(payload) if self._authorization_guard else nullcontext()
                with guard:
                    if not self.ready:
                        raise _ConnectionLost('simulated MQTT connection lost')
                    if self._order_authorizer is not None and self._order_authorizer(payload) is not True:
                        raise ValueError('order does not match a reserved simulated segment')
                    self.executor.accept(payload)
                if not self.ready or self._client.ack(mid, qos) != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('simulated order acknowledgement failed')
                processing_order = False
            if not self.ready:
                raise _ConnectionLost('simulated MQTT connection lost')
            state = self.executor.snapshot()
            if (state.get('status') == 'moving' and type(dt_seconds) in (int, float)
                    and dt_seconds > 0 and (self._authorization_guard or self._order_authorizer)):
                payload = json.dumps(state['order'], ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).encode('utf-8')
                guard = self._authorization_guard(payload) if self._authorization_guard else nullcontext()
                with guard:
                    if not self.ready:
                        raise _ConnectionLost('simulated MQTT connection lost')
                    if self._order_authorizer is not None and self._order_authorizer(payload) is not True:
                        raise ValueError('reserved simulated segment is no longer current')
                    return self._tick_executor(dt_seconds)
            return self._tick_executor(dt_seconds)
        except Exception as exc:
            if (isinstance(exc, _ConnectionLost) and not processing_order and not self._event_uncertain
                    and (self._recoverable_disconnect or not self._failed.is_set())):
                # No command was removed from the queue and no event is
                # uncertain. Preserve this disconnect for reconciliation,
                # even if Paho's disconnect callback has not run yet.
                self._failed.set()
                self._ready.clear()
                self._recoverable_disconnect = True
            else:
                self._fail()
            raise RuntimeError('simulated order processing stopped') from exc

    def publish_event(self, event):
        """Publish one matching virtual stop event; PUBACK is not execution proof."""
        with self._lock:
            if not self.ready:
                raise RuntimeError('simulated MQTT robot is unavailable')
            try:
                checked, encoded, _digest = _event(event)
                with self.executor._lock:
                    self.executor._verify_field_execution()
                    state = self.executor.snapshot()
                receipt, order = state['receipt'], state['order']
                if (state['status'] != 'stopped' or not receipt or not order
                        or type(state.get('visit_index')) is not int
                        or state.get('visit_index') != state.get('released_index')
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
                self._event_uncertain = True
                info = self._client.publish(self.event_topic, encoded, qos=1, retain=False)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('virtual event queueing failed')
                info.wait_for_publish(timeout=5)
                if not self.ready or info.is_published() is not True:
                    raise RuntimeError('virtual event publication was not confirmed')
                self._event_uncertain = False
                return {'event_id': checked['event_id'], 'published': True}
            except Exception as exc:
                self._fail()
                raise RuntimeError('simulated event publication stopped') from exc
