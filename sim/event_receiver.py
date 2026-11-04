"""Receive proposed rear-clear events for an isolated simulation only.

The caller owns and starts the dedicated Paho client and supplies the
network-isolated broker. This adapter neither connects nor publishes and
must not be used with physical robots or real drive permission.
"""
import json
from functools import wraps
import logging
import math
import os
import threading
from types import MappingProxyType

import paho.mqtt.client as mqtt

from server.allocation_store import _validate_json
from server.order_publisher import _topic
from server.recovery import RecoveryGate, _id


MAX_EVENT_BYTES = 256 * 1024
MAX_PENDING_ACKS = 64
_LOGGER = logging.getLogger(__name__)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('invalid JSON constant')


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('invalid JSON number')
    return number


class _AckToken:
    def __init__(self, mid):
        self.mid = mid


def _serialized(callback):
    @wraps(callback)
    def run(self, *args):
        with self._dispatch_lock:
            return callback(self, *args)
    return run


class SimEventReceiver:
    """Bind a dedicated, non-reconnecting client to explicitly listed tasks."""

    def __init__(self, client, progress, *, inbox, recovery_gate, robot_tasks,
                 simulation_enabled=False, on_result=None, on_error=None, robot_sessions=None):
        if simulation_enabled is not True:
            raise ValueError('isolated simulation must be explicitly enabled')
        if getattr(client, '_reconnect_on_failure', None) is not False:
            raise ValueError('client must disable automatic reconnect')
        if getattr(client, '_clean_session', None) is not False or not getattr(client, '_client_id', b''):
            raise ValueError('persistent MQTT session and stable client ID required')
        if not isinstance(recovery_gate, RecoveryGate) or getattr(progress, '_gate', None) is not recovery_gate:
            raise ValueError('progress and receiver must use the same RecoveryGate')
        if not callable(getattr(progress, 'advance', None)):
            raise ValueError('progress must provide advance')
        if not isinstance(robot_tasks, dict) or not robot_tasks:
            raise ValueError('explicit nonempty robot-to-task mapping is required')
        mapping, all_tasks = {}, set()
        for robot_id, task_ids in robot_tasks.items():
            _topic(robot_id)
            if not isinstance(task_ids, (list, tuple, set, frozenset)) or not task_ids:
                raise ValueError('each robot needs explicit task IDs')
            for task_id in task_ids:
                _id(task_id, 'task id')
                if task_id in all_tasks:
                    raise ValueError('task IDs must be unique across robots')
                all_tasks.add(task_id)
            mapping[robot_id] = frozenset(task_ids)
        if (not callable(getattr(inbox, 'store', None)) or not callable(getattr(inbox, 'retry', None))
                or os.path.normcase(os.path.realpath(inbox.path)) != os.path.normcase(os.path.realpath(recovery_gate.path))
                or inbox.robot_tasks != mapping):
            raise ValueError('event inbox must match gate database and robot mapping')
        client.manual_ack_set(True)
        if getattr(client, '_manual_ack', None) is not True:
            raise ValueError('manual MQTT acknowledgement is required')
        if (on_result is not None and not callable(on_result)) or (on_error is not None and not callable(on_error)):
            raise ValueError('observers must be callable')
        sessions = {}
        if robot_sessions is not None:
            if not isinstance(robot_sessions, dict) or set(robot_sessions) != set(mapping):
                raise ValueError('sessions must match configured robots')
            for robot, values in robot_sessions.items():
                if not isinstance(values, dict) or set(values) != set(mapping[robot]):
                    raise ValueError('sessions must match configured tasks')
                sessions[robot] = {}
                for task, session in values.items():
                    _id(session, 'robot_session')
                    sessions[robot][task] = session
        for name in ('on_connect', 'on_subscribe', 'on_message', 'on_disconnect'):
            if getattr(client, name, None) is not None:
                raise ValueError('a dedicated client without existing callbacks is required')
        self.client, self.progress, self.gate, self.inbox = client, progress, recovery_gate, inbox
        self.robot_tasks = MappingProxyType(mapping)
        self.robot_sessions = MappingProxyType({robot: MappingProxyType(values) for robot, values in sessions.items()})
        self.task_ids = frozenset(all_tasks)
        self.on_result, self.on_error = on_result, on_error
        self._lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._pending_acks = []
        self._ready = False
        self._connected = False
        self._closed = False
        self._pending_mid = None
        self.last_result = None
        self.last_error = None
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

    @property
    def ready(self):
        with self._lock:
            return self._ready and not self._closed

    def _report(self, error):
        # Exception tracebacks may retain the original MQTT message frame.
        error.__traceback__ = None
        error.__context__ = None
        error.__cause__ = None
        with self._lock:
            self.last_error = error
        if self.on_error is None:
            _LOGGER.warning('simulation event reception failed: %s', type(error).__name__)
            return
        try:
            self.on_error(error)
        except Exception:
            _LOGGER.warning('simulation event error observer failed')

    def _invalidate(self, tasks):
        try:
            self.gate.invalidate_many(tasks)
        except Exception:
            return [RuntimeError('recovery invalidation failed')]
        return []

    def _close_and_invalidate(self, cause, *, extra_errors=(), disconnect=True):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connected = False
            self._ready = False
            self._pending_mid = None
            self._pending_acks.clear()
        # The gate blocks all tasks locally before attempting one database
        # transaction, including if the database subsequently fails.
        failures = self._invalidate(self.task_ids)
        if disconnect:
            try:
                self.client.disconnect()
            except Exception:
                failures.append(RuntimeError('simulation MQTT disconnect failed'))
        self._report(cause)
        for error in (*extra_errors, *failures):
            self._report(error)

    @_serialized
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            if self._closed:
                return
            if self._connected:
                unexpected = True
            else:
                unexpected = False
                self._connected = not reason_code.is_failure
        if unexpected or reason_code.is_failure:
            self._close_and_invalidate(RuntimeError('simulation MQTT connection rejected or repeated'))
            return
        try:
            result, mid = client.subscribe([('agv/+/event', 1), ('agv/+/conn', 1)])
            if result != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('simulation event subscription request failed')
            with self._lock:
                if not self._closed:
                    self._pending_mid = mid
        except Exception:
            self._close_and_invalidate(RuntimeError('simulation event subscription request failed'))

    @_serialized
    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        with self._lock:
            if self._closed:
                return
            if not self._connected or mid != self._pending_mid:
                self._close_and_invalidate(RuntimeError('unexpected simulation subscription acknowledgement'))
                return
            self._pending_mid = None
            try:
                accepted = (len(reason_codes) == 2 and all(
                    not code.is_failure and code.value == 1 for code in reason_codes))
            except (TypeError, AttributeError):
                accepted = False
        if not accepted:
            self._close_and_invalidate(RuntimeError('simulation event subscription rejected or downgraded'))
            return
        for message in self._pending_acks:
            if not self._ack(client, message):
                return
        self._pending_acks.clear()
        with self._lock:
            self._ready = True

    def _queue_ack(self, message):
        if (type(message.mid) is not int or not 1 <= message.mid <= 65535
                or len(self._pending_acks) >= MAX_PENDING_ACKS
                or any(existing.mid == message.mid for existing in self._pending_acks)):
            raise ValueError('invalid or excessive pending MQTT acknowledgements')
        # Keep only a tiny acknowledgement token, never a message payload.
        self._pending_acks.append(_AckToken(message.mid))

    def _ack(self, client, message):
        try:
            if client.ack(message.mid, 1) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('MQTT acknowledgement failed')
        except Exception:
            self._close_and_invalidate(RuntimeError('MQTT acknowledgement failed'))
            return False
        return True

    @_serialized
    def _on_message(self, client, userdata, message):
        early = not self.ready
        if early and (not self._connected or self._pending_mid is None):
            self._close_and_invalidate(RuntimeError('message arrived outside an active subscription'))
            return
        try:
            if not isinstance(message.topic, str):
                raise ValueError('invalid event topic')
            parts = message.topic.split('/')
            if len(parts) != 3 or parts[0] != 'agv' or parts[2] not in ('event', 'conn'):
                raise ValueError('invalid event topic')
            _topic(parts[1])  # validates simulated robot ID without network use
            tasks = self.robot_tasks.get(parts[1])
            if not tasks:
                raise ValueError('robot is not explicitly allowed')
            if parts[2] == 'conn':
                valid_flags = (type(message.retain) is bool and type(message.qos) is int
                               and message.qos in (0, 1))
                valid_payload = isinstance(message.payload, bytes) and message.payload in (b'online', b'offline')
                if not valid_flags or not valid_payload:
                    failures = self._invalidate(tasks)
                    if failures:
                        self._close_and_invalidate(RuntimeError('invalid connection recovery invalidation failed'),
                                                   extra_errors=failures)
                        return
                    raise ValueError('invalid simulated connection state')
                if message.payload == b'offline':
                    failures = self._invalidate(tasks)
                    if failures:
                        self._close_and_invalidate(RuntimeError('offline recovery invalidation failed'),
                                                   extra_errors=failures)
                        return
                if message.qos == 1:
                    if early:
                        self._queue_ack(message)
                    else:
                        self._ack(client, message)
                return
            if type(message.retain) is not bool or type(message.qos) is not int or message.qos not in (0, 1):
                raise ValueError('invalid MQTT message flags')
            if not isinstance(message.payload, bytes) or len(message.payload) > MAX_EVENT_BYTES:
                raise ValueError('invalid event payload size')
            if message.qos != 1:
                raise ValueError('event must arrive at QoS 1')
            if message.retain:
                raise ValueError('retained events are not accepted')
            invalid_encoding_or_json = False
            try:
                event = json.loads(message.payload.decode('utf-8'), object_pairs_hook=_pairs,
                                   parse_constant=_constant, parse_float=_finite)
            except (UnicodeError, json.JSONDecodeError):
                invalid_encoding_or_json = True
            if invalid_encoding_or_json:
                raise ValueError('invalid UTF-8 or JSON')
            if not isinstance(event, dict):
                raise ValueError('event must be a JSON object')
            _validate_json(event)
            if event.get('order_id') not in tasks:
                raise ValueError('event task is not explicitly allowed')
            task_id = event['order_id']
            expected_session = self.robot_sessions.get(parts[1], {}).get(task_id)
            if expected_session is not None and event.get('robot_session') != expected_session:
                failures = self._invalidate((task_id,))
                if failures:
                    self._close_and_invalidate(RuntimeError('session recovery invalidation failed'),
                                               extra_errors=failures)
                    return
                if early:
                    self._queue_ack(message)
                elif not self._ack(client, message):
                    return
                self._report(RuntimeError('simulation session changed; recovery proof revoked'))
                return
            try:
                stored = self.inbox.store(parts[1], event)
            except Exception as exc:
                self._close_and_invalidate(exc)
                return
            if early:
                self._queue_ack(message)
                return
            if not self._ack(client, message):
                return
            if stored['status'] == 'duplicate':
                result = stored
            else:
                result = self.inbox.retry(parts[1], stored['event_id'], self.progress)
            with self._lock:
                self.last_result = result
            if self.on_result is not None:
                self.on_result(result)
        except Exception as exc:
            if early:
                self._close_and_invalidate(exc)
            else:
                self._report(exc)

    @_serialized
    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._close_and_invalidate(RuntimeError('simulation MQTT disconnected; recovery proof revoked'), disconnect=False)
