"""Opt-in, loopback-only physical *claims* for blocked recovery observations.

The watcher never starts a proof/retry worker, publishes orders, clears a gate,
or processes inbox events. Device messages are not physical stop evidence.
"""
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from contextlib import closing
from types import MappingProxyType, SimpleNamespace

import paho.mqtt.client as mqtt

from server.recovery import RecoveryGate, _id
from server.allocation_store import _id as _allocation_id
from sim.event_inbox import _event
from sim import mqtt_order_robot


def is_physical_robot(value):
    return (isinstance(value, str) and value.startswith('physical-')
            and bool(value[9:].strip()))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate state field')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('non-finite state value')


def require_direct_mqtt_client(client):
    """Reject both process-level and explicit Paho proxy configuration."""
    # A loopback host string alone does not stop PySocks from contacting an
    # external proxy. Check immediately before each isolated connection.
    mqtt_order_robot._verify_no_proxy()
    proxy = getattr(client, '_proxy', None)
    if proxy is not None and (type(proxy) is not dict or proxy):
        raise RuntimeError('a client MQTT proxy is not allowed')


class PhysicalEvidenceInbox:
    """Read-only view of already persisted inbox rows; no event submission."""
    def __init__(self, path, robot_tasks, *, isolated=False):
        if isolated is not True or not isinstance(robot_tasks, dict) or not robot_tasks:
            raise ValueError('explicit isolated robot-task mapping is required')
        self.path = os.path.realpath(os.fspath(path))
        if not os.path.isfile(self.path):
            raise ValueError('existing inbox database required')
        mapping, seen = {}, set()
        for robot, tasks in robot_tasks.items():
            _id(robot, 'robot_id')
            _allocation_id(robot, 'robot_id', True)
            if not is_physical_robot(robot) or not isinstance(tasks, (tuple, list, set, frozenset)) or not tasks:
                raise ValueError('nonempty physical robot task mapping required')
            for task in tasks:
                _id(task, 'task_id')
                if task in seen:
                    raise ValueError('task belongs to multiple robots')
                seen.add(task)
            mapping[robot] = frozenset(tasks)
        self.robot_tasks = MappingProxyType(mapping)
        with closing(self._read()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sim_event_inbox'").fetchone():
                raise ValueError('existing event inbox required')

    def _read(self):
        return sqlite3.connect(Path(self.path).as_uri() + '?mode=ro', uri=True, timeout=5)

    def get(self, robot, event_id):
        _id(event_id, 'event_id')
        if robot not in self.robot_tasks:
            raise ValueError('unknown robot')
        with closing(self._read()) as db:
            row = db.execute('SELECT payload,digest,state,result,task_id,robot_session,seq '
                             'FROM sim_event_inbox WHERE robot_id=? AND event_id=?', (robot, event_id)).fetchone()
        if row is None:
            return None
        event, canonical, digest = _event(row[0].encode('ascii'))
        if (canonical != row[0] or digest != row[1] or event['event_id'] != event_id
                or event['order_id'] not in self.robot_tasks[robot]
                or (event['order_id'], event['robot_session'], event['seq']) != tuple(row[4:7])):
            raise ValueError('existing event is inconsistent')
        return {'event': event, 'state': row[2], 'result': json.loads(row[3]) if row[3] else None}

    def list_pending(self):
        with closing(self._read()) as db:
            rows = db.execute("SELECT robot_id,event_id FROM sim_event_inbox WHERE state='pending' "
                              'ORDER BY robot_id,event_id').fetchall()
        result = []
        for robot, event_id in rows:
            if robot in self.robot_tasks:
                item = self.get(robot, event_id)
                if item is not None and item['state'] == 'pending':
                    result.append({'robot_id': robot, 'event_id': event_id, 'event': item['event']})
        return result


class PhysicalEvidenceWatcher:
    """One-shot state observer for a blocked robot, with no resume worker."""
    def __init__(self, gate, task_sessions, *, host, port, isolated=False, client=None,
                 clock=time.monotonic, state_max_age_s=2.0, connect_timeout=5.0):
        if (isolated is not True or not isinstance(gate, RecoveryGate) or host != '127.0.0.1'
                or type(port) is not int or not 1 <= port <= 65535
                or not isinstance(task_sessions, dict) or not task_sessions or not callable(clock)):
            raise ValueError('explicit loopback-only blocked task observer required')
        if (isinstance(state_max_age_s, bool) or not isinstance(state_max_age_s, (int, float))
                or not math.isfinite(state_max_age_s) or not 0 < state_max_age_s <= 30
                or isinstance(connect_timeout, bool) or not isinstance(connect_timeout, (int, float))
                or not math.isfinite(connect_timeout) or not 0 < connect_timeout <= 10):
            raise ValueError('invalid observer timeout')
        tasks, sessions = {}, {}
        for task, pair in task_sessions.items():
            _id(task, 'task_id')
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValueError('robot and session required for each task')
            robot, session = pair
            _id(robot, 'robot_id'); _id(session, 'robot_session')
            _allocation_id(robot, 'robot_id', True)
            if not is_physical_robot(robot) or (robot in sessions and sessions[robot] != session):
                raise ValueError('consistent physical robot session required')
            sessions[robot] = session
            tasks[task] = (robot, session)
        supplied = client or mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        if getattr(supplied, '_reconnect_on_failure', False) is not False:
            raise ValueError('automatic MQTT reconnect must be disabled')
        self._client, self._host, self._port = supplied, host, port
        self._runtime = SimpleNamespace(gate=gate, _tasks=MappingProxyType(tasks))
        self.robots, self._sessions = tuple(sorted(sessions)), MappingProxyType(sessions)
        self._invalidated = set(self.robots)  # permanent hold, never a permission
        self._clock, self._max_state_age, self._timeout = clock, float(state_max_age_s), float(connect_timeout)
        self._lock = threading.RLock()
        self._connected, self._subscription_done = threading.Event(), threading.Event()
        self._latest, self._online = {}, {}
        self._connection_generation, self._state_serial = 0, 0
        self._pending_mid, self._mqtt_started, self._started, self._stopped = None, False, False, False
        self.last_error = None
        with gate._operation_lock:
            if gate._physical_watcher is not None:
                raise ValueError('another physical observation source is bound')
            gate.invalidate_many(tuple(tasks))
            gate._physical_watcher = self

    def start(self):
        if self._started or self._stopped:
            raise RuntimeError('observer is one-shot')
        self._started = self._mqtt_started = True
        c = self._client
        c.on_connect, c.on_subscribe = self._on_connect, self._on_subscribe
        c.on_message, c.on_disconnect = self._on_message, self._on_disconnect
        try:
            require_direct_mqtt_client(c)
            if c.connect(self._host, self._port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('observer connection failed')
            if c.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('observer loop failed')
            if not self._subscription_done.wait(self._timeout) or not self._connected.is_set():
                raise RuntimeError('observer subscription failed')
        except BaseException:
            self.stop()
            raise
        return self

    def _on_connect(self, client, _userdata, _flags, reason, _properties):
        if self._stopped:
            return
        if reason.is_failure:
            self._on_disconnect(client, None, None, reason, None)
            return
        with self._lock:
            self._connection_generation += 1
            generation = self._connection_generation
            self._latest.clear(); self._online.clear(); self._connected.clear(); self._subscription_done.clear()
            client.on_subscribe = lambda c, u, mid, codes, p: self._on_subscribe(c, u, mid, codes, p, generation)
            client.on_message = lambda c, u, msg: self._on_message(c, u, msg, generation)
            client.on_disconnect = lambda c, u, f, r, p: self._on_disconnect(c, u, f, r, p, generation)
        topics = [(f'agv/{robot}/{kind}', qos) for robot in self.robots for kind, qos in (('state', 0), ('conn', 1))]
        try:
            rc, mid = client.subscribe(topics)
            if rc != mqtt.MQTT_ERR_SUCCESS or type(mid) is not int or mid <= 0:
                raise RuntimeError('subscription request failed')
            with self._lock:
                if generation == self._connection_generation:
                    self._pending_mid = mid
        except Exception:
            self.last_error = 'SubscriptionFailed'
            self._on_disconnect(client, None, None, None, None, generation)

    def _on_subscribe(self, _client, _userdata, mid, codes, _properties, generation):
        with self._lock:
            if generation != self._connection_generation:
                return
            try:
                ok = (mid == self._pending_mid and mid is not None
                      and [x.value for x in codes] == [q for _ in self.robots for q in (0, 1)]
                      and all(not x.is_failure for x in codes))
            except (TypeError, AttributeError):
                ok = False
            self._pending_mid = None
            if ok:
                self._connected.set()
            else:
                self.last_error = 'SubscriptionRejected'
            self._subscription_done.set()

    def _on_disconnect(self, _client, _userdata, _flags, _reason, _properties, generation=None):
        with self._lock:
            if generation is not None and generation != self._connection_generation:
                return
            self._connection_generation += 1
            self._connected.clear(); self._latest.clear(); self._online.clear()
            self._pending_mid = None; self._subscription_done.set()

    def _on_message(self, _client, _userdata, message, generation):
        try:
            parts = message.topic.split('/')
            if len(parts) != 3 or parts[0] != 'agv' or parts[1] not in self.robots or parts[2] not in ('state', 'conn'):
                return
            robot, kind = parts[1], parts[2]
            if type(message.retain) is not bool or type(message.qos) is not int or message.qos not in (0, 1):
                raise ValueError('invalid MQTT envelope')
            payload = message.payload
            if not isinstance(payload, bytes) or len(payload) > 64 * 1024:
                raise ValueError('invalid state payload')
            if kind == 'conn':
                if payload not in (b'online', b'offline'):
                    raise ValueError('invalid connection status')
                with self._lock:
                    if generation != self._connection_generation or not self._connected.is_set():
                        return
                    self._online[robot] = payload == b'online'
                    if payload != b'online':
                        self._latest.pop(robot, None)
                return
            if message.retain:
                return
            state = json.loads(payload.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
            if not isinstance(state, dict) or state.get('robot_session') != self._sessions[robot]:
                raise ValueError('state session differs')
            with self._lock:
                if generation != self._connection_generation or not self._connected.is_set() or self._online.get(robot) is not True:
                    return
                self._state_serial += 1
                self._latest[robot] = (state, self._clock(), generation, self._state_serial)
        except (ValueError, UnicodeError, AttributeError, RecursionError):
            with self._lock:
                if generation == self._connection_generation and 'robot' in locals():
                    self._latest.pop(robot, None)
                    if 'kind' in locals() and kind == 'conn':
                        self._online[robot] = False

    def stop(self):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._connection_generation += 1
            self._connected.clear(); self._latest.clear(); self._online.clear()
            self._pending_mid = None; self._subscription_done.set()
        try:
            self._client.disconnect()
        finally:
            try:
                self._client.loop_stop()
            finally:
                gate = self._runtime.gate
                with gate._operation_lock:
                    if gate._physical_watcher is self:
                        try:
                            gate.invalidate_many(tuple(self._runtime._tasks))
                        finally:
                            gate._physical_watcher = None
