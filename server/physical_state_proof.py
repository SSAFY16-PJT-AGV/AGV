"""Session discovery and stop proof for ``physical-`` robots from their state topic.

The team robot (feature/field-run ``serve`` mode) creates a new
``robot_session`` every time its process starts and publishes
``agv/<id>/state`` at about 5 Hz. It has no challenge/response channel, so the
existing recovery gate (``server/recovery.py``) cannot be answered by the robot
itself. This module lets ``SimRuntime`` drive physical robots without changing
the gate's rules:

1. **Session from state.** At runtime start, a ``"from_state"`` session in the
   configuration is replaced with the ``robot_session`` of the robot's first
   *live* (non-retained) state message. A later state with a different session
   means the robot process restarted: all of that robot's tasks are invalidated
   (fail closed) and the runtime must be restarted after checking the robot.
2. **Stop proof from state.** When a live state of the configured session shows
   the robot stopped (``WAITING`` or ``IDLE``) at the ``released_until`` node of
   the latest order the server sent for that task (same ``order_id``, ``seq``,
   ``map_id``), the watcher opens a gate challenge and completes it with the
   server's own stored order digest. The proof is refreshed while the robot
   stays there because gate proofs expire.
3. **Event retry.** A ``rear_clear`` that arrived before the proof, or that
   waited for a segment held by another robot, is retried, and the resulting
   next order is submitted through the existing workflow.

What this does *not* prove: the state message is the robot's own report, the
same evidence class as its ``rear_clear`` event. It does not measure that the
robot's rear actually left a resource, and it does not authenticate the robot.
Use broker authentication/ACLs for identity (``server/mqtt_security.py``).
"""
import json
import math
import threading
import time

import paho.mqtt.client as mqtt

from server.robot_ids import is_physical_robot

FROM_STATE = 'from_state'
MAX_STATE_BYTES = 64 * 1024
DEFAULT_MAX_STATE_AGE_S = 2.0
STOPPED = ('WAITING', 'IDLE')


def _unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate state field')
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError('non-finite state number')


class PhysicalStateWatcher:
    def __init__(self, robots, *, client, host, port, connect_timeout=5.0, refresh_s=15.0,
                 poll_s=0.2, log=None, clock=time.monotonic,
                 state_max_age_s=DEFAULT_MAX_STATE_AGE_S):
        robots = tuple(robots)
        if not robots or any(not is_physical_robot(r) for r in robots) or len(set(robots)) != len(robots):
            raise ValueError('unique physical- robot IDs are required')
        self.robots = robots
        self._client, self._host, self._port = client, host, port
        self._timeout, self._refresh, self._poll = float(connect_timeout), float(refresh_s), float(poll_s)
        self._clock = clock
        if (isinstance(state_max_age_s, bool) or not isinstance(state_max_age_s, (int, float))
                or not math.isfinite(state_max_age_s) or not 0 < state_max_age_s <= 30):
            raise ValueError('state_max_age_s must be finite and in (0, 30]')
        self._max_state_age = float(state_max_age_s)
        self._log = log or (lambda msg: print('[physical] ' + msg, flush=True))
        self._lock = threading.RLock()
        self._latest = {}          # robot -> (state dict, monotonic time, connection generation, serial)
        self._connection_generation = 0
        self._state_serial = 0
        self._mqtt_started = False
        self._started_client = None
        self._sessions = {}        # robot -> session bound into the runtime
        self._invalidated = set()  # robots whose session changed
        self._proven = {}          # robot -> ((task, seq), monotonic time)
        self._motion_permits = {}  # robot -> queued (task, session, seq, map, release)
        self._runtime = None
        self._stop = threading.Event()
        self._thread = None
        self._connected = threading.Event()
        self._subscription_done = threading.Event()
        self._pending_mid = None
        self._online = {}          # robot -> last accepted broker connection report
        self.last_error = None

    # MQTT -----------------------------------------------------------------
    def start(self):
        # Paho's loop_start allocates a socketpair each time. A disconnected
        # client must not be reused: a new client is also a new MQTT generation.
        if self._started_client is self._client:
            raise RuntimeError('physical watcher requires a fresh MQTT client')
        self._started_client = self._client
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._mqtt_started = True
        # A previous disconnect signals the old subscription wait. Never let
        # that stale event stand in for the new connection's SUBACK.
        with self._lock:
            self._connected.clear()
            self._subscription_done.clear()
        if self._client.connect(self._host, self._port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError('physical state connection failed')
        if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError('physical state loop failed')
        if not self._subscription_done.wait(self._timeout) or not self._connected.is_set():
            self.stop()
            raise RuntimeError('physical state subscription failed or timed out')
        return self

    def _on_connect(self, client, _userdata, _flags, code, _properties):
        if code.is_failure:
            self._on_disconnect(client, None, None, code, None)
            return
        with self._lock:
            self._connected.clear()
            repeated = self._connection_generation > 0
            self._connection_generation += 1
            generation = self._connection_generation
            self._latest.clear()
            self._proven.clear()
            self._motion_permits.clear()
            self._online.clear()
            self._pending_mid = None
            self._subscription_done.clear()
            invalidated = self._latch_robots_locked(self.robots) if repeated and self._runtime is not None else set()
        self._revoke_robots(invalidated)
        # Bind callbacks to the connection which registered them. In-flight
        # callbacks from an earlier generation must not change new evidence.
        with self._lock:
            if generation != self._connection_generation:
                return
            client.on_subscribe = lambda c, u, mid, codes, p: self._on_subscribe(
                c, u, mid, codes, p, _generation=generation)
            client.on_message = lambda c, u, msg: self._on_message(c, u, msg, _generation=generation)
            client.on_disconnect = lambda c, u, flags, reason, p: self._on_disconnect(
                c, u, flags, reason, p, _generation=generation)
        topics = []
        for robot in self.robots:
            topics.extend([('agv/%s/state' % robot, 0), ('agv/%s/conn' % robot, 1)])
        try:
            rc, mid = client.subscribe(topics)
            if rc != mqtt.MQTT_ERR_SUCCESS or type(mid) is not int or mid <= 0:
                raise RuntimeError('physical subscription request failed')
            with self._lock:
                if generation != self._connection_generation:
                    return
                self._pending_mid = mid
        except Exception as exc:
            self.last_error = type(exc).__name__
            self._on_disconnect(client, None, None, None, None, _generation=generation)

    def _on_subscribe(self, client, _userdata, mid, reason_codes, _properties, *, _generation=None):
        with self._lock:
            if _generation is not None and _generation != self._connection_generation:
                return
            try:
                granted = [code.value for code in reason_codes]
                success = (mid == self._pending_mid and self._pending_mid is not None
                           and granted == [qos for _robot in self.robots for qos in (0, 1)]
                           and all(not code.is_failure for code in reason_codes))
            except (TypeError, AttributeError):
                success = False
            self._pending_mid = None
            if success:
                self._connected.set()
                self._subscription_done.set()
        if not success:
            self.last_error = 'SubscriptionRejected'
            self._on_disconnect(client, None, None, None, None, _generation=_generation)

    def _latch_robots_locked(self, robots):
        new = set(robots) - self._invalidated
        self._invalidated.update(new)
        for robot in new:
            self._proven.pop(robot, None)
            self._motion_permits.pop(robot, None)
        return new

    def _revoke_robots(self, robots):
        runtime = self._runtime
        if runtime is None or not robots:
            return
        tasks = tuple(task for task, (robot, _session) in runtime._tasks.items() if robot in robots)
        if tasks:
            try:
                runtime.gate.invalidate_many(tasks)
            except Exception as exc:
                self.last_error = type(exc).__name__
                self._log('failed to invalidate stale physical state: %s' % type(exc).__name__)

    def _invalidate_robots(self, robots):
        # Permanent local latch. A new message or MQTT reconnect cannot clear
        # it; re-establishing physical state requires a separate protocol.
        with self._lock:
            new = self._latch_robots_locked(robots)
        self._revoke_robots(new)

    def _on_disconnect(self, _client, _userdata, _flags, _reason_code, _properties, *, _generation=None):
        with self._lock:
            if _generation is not None and _generation != self._connection_generation:
                return
            self._connected.clear()
            self._connection_generation += 1
            self._latest.clear()
            self._proven.clear()
            self._motion_permits.clear()
            self._online.clear()
            self._pending_mid = None
            self._subscription_done.set()
            invalidated = self._latch_robots_locked(self.robots) if self._runtime is not None else set()
        self._revoke_robots(invalidated)

    def _on_message(self, _client, _userdata, message, *, _generation=None):
        state_robot = None
        try:
            parts = message.topic.split('/')
            if len(parts) != 3 or parts[0] != 'agv' or parts[2] not in ('state', 'conn') or parts[1] not in self.robots:
                return
            if parts[2] == 'conn':
                payload = getattr(message, 'payload', None)
                valid = (type(getattr(message, 'retain', None)) is bool
                         and type(getattr(message, 'qos', None)) is int
                         and message.qos in (0, 1) and isinstance(payload, bytes)
                         and payload in (b'online', b'offline'))
                offline = not valid or payload == b'offline'
                with self._lock:
                    if _generation is not None and _generation != self._connection_generation:
                        return
                    if self._mqtt_started and not self._connected.is_set():
                        return
                    self._online[parts[1]] = not offline
                    if offline:
                        self._state_serial += 1
                        self._latest.pop(parts[1], None)
                        self._proven.pop(parts[1], None)
                    invalidated = (self._latch_robots_locked((parts[1],))
                                   if offline and self._runtime is not None else set())
                self._revoke_robots(invalidated)
                return
            if message.retain is True:
                return   # a retained state may belong to an earlier robot process
            state_robot = parts[1]
            if (type(message.retain) is not bool or not isinstance(message.payload, bytes)
                    or len(message.payload) > MAX_STATE_BYTES):
                raise ValueError('invalid live state payload')
            state = json.loads(message.payload.decode('utf-8'), object_pairs_hook=_unique_pairs,
                               parse_constant=_invalid_constant)
            session = state.get('robot_session') if isinstance(state, dict) else None
            if (not isinstance(session, str) or not session or session != session.strip()
                    or len(session.encode('utf-8')) > 256):
                raise ValueError('invalid live robot session')
        except (ValueError, UnicodeError, AttributeError, RecursionError):
            if state_robot is not None:
                with self._lock:
                    active = ((_generation is None or _generation == self._connection_generation)
                              and (not self._mqtt_started or
                                   (self._connected.is_set() and self._online.get(state_robot) is True)))
                    if active:
                        self._state_serial += 1
                        self._latest.pop(state_robot, None)
                        invalidated = (self._latch_robots_locked((state_robot,))
                                       if self._runtime is not None else set())
                    else:
                        invalidated = set()
                self._revoke_robots(invalidated)
            return
        with self._lock:
            if _generation is not None and _generation != self._connection_generation:
                return
            if self._mqtt_started and (not self._connected.is_set() or not self._online.get(parts[1], False)):
                return
            robot = parts[1]
            previous = self._proven.get(robot)
            prior_item = self._latest.get(robot)
            failed = (state.get('status') not in ('IDLE', 'WAITING', 'MOVING')
                      or 'error' not in state or state['error'] is not None
                      or 'rejected' not in state or state['rejected'] is not None
                      or (self._runtime is not None and robot in self._sessions
                          and state['robot_session'] != self._sessions[robot]))
            if previous is not None and state.get('status') not in STOPPED:
                expected_moving = (state.get('status') == 'MOVING'
                                   and type(state.get('seq')) is int and state['seq'] > 0
                                   and self._motion_permits.get(robot) ==
                                       (state.get('order_id'), state['robot_session'], state['seq'],
                                        state.get('map_id'), state.get('released_until'))
                                   and state.get('error', object()) is None
                                   and state.get('rejected', object()) is None)
                self._proven.pop(robot, None)
                if not expected_moving:
                    failed = True
            if state.get('status') == 'MOVING':
                expected = (state.get('order_id'), state['robot_session'], state.get('seq'),
                            state.get('map_id'), state.get('released_until'))
                if self._motion_permits.get(robot) != expected:
                    failed = True
            elif state.get('status') in STOPPED and state.get('order_id') is not None:
                current = (state.get('order_id'), state.get('seq'))
                permit = (state.get('order_id'), state['robot_session'], state.get('seq'),
                          state.get('map_id'), state.get('released_until'))
                valid_stop = (type(state.get('seq')) is int and state['seq'] > 0
                              and isinstance(state.get('last_node'), str)
                              and state['last_node'] == state.get('released_until'))
                if previous is not None and current == previous[0]:
                    old = None if prior_item is None else prior_item[0]
                    if (not valid_stop or old is None
                            or any(old.get(key) != state.get(key)
                                   for key in ('robot_session', 'order_id', 'seq', 'map_id',
                                               'last_node', 'released_until'))):
                        failed = True
                elif previous is not None:
                    if valid_stop and self._motion_permits.get(robot) == permit:
                        # A queued next order may stop before MOVING is observed.
                        # The old proof cannot authorize this new stop.
                        self._proven.pop(robot, None)
                    else:
                        failed = True
                elif self._motion_permits.get(robot) is not None:
                    if not valid_stop or self._motion_permits[robot] != permit:
                        failed = True
            elif (state.get('status') in STOPPED
                  and (previous is not None or self._motion_permits.get(robot) is not None)):
                failed = True
            invalidated = (self._latch_robots_locked((robot,))
                           if failed and self._runtime is not None else set())
            self._state_serial += 1
            self._latest[robot] = (state, self._clock(), self._connection_generation, self._state_serial)
        self._revoke_robots(invalidated)

    # Session discovery -----------------------------------------------------
    def wait_sessions(self, timeout):
        """Return {robot: robot_session} from live states, or raise after timeout."""
        deadline = self._clock() + float(timeout)
        while True:
            found = {}
            for robot in self.robots:
                state = self.latest(robot)
                if state is not None:
                    found[robot] = state['robot_session']
            if len(found) == len(self.robots):
                return found
            if self._clock() >= deadline:
                missing = sorted(set(self.robots) - set(found))
                raise RuntimeError('no live agv/<id>/state from physical robot(s): ' + ', '.join(missing))
            time.sleep(0.05)

    def latest(self, robot):
        with self._lock:
            item = self._latest.get(robot)
            age = None if item is None else self._clock() - item[1]
            stale = item is not None and (item[2] != self._connection_generation
                    or age < 0 or age > self._max_state_age
                    or (self._mqtt_started and not self._connected.is_set()))
            if stale:
                self._latest.pop(robot, None)
            invalidated = (self._latch_robots_locked((robot,))
                           if stale and self._runtime is not None else set())
        if stale:
            self._revoke_robots(invalidated)
            return None
        return None if item is None else dict(item[0])

    def _token(self, robot, state):
        # Freshness is checked again after the DB lookup and challenge.
        current = self.latest(robot)
        if current is None or current != state:
            return None
        with self._lock:
            item = self._latest.get(robot)
            if item is None or item[0] != state or robot in self._invalidated:
                return None
            return item[2], item[3]

    def _start_evidence_locked(self, robot, session, node, map_id, *, source_task=None, source_seq=None,
                               proof_ttl=30.0):
        """Capture a fresh initial or handoff report; caller holds state lock."""
        item = self._latest.get(robot)
        age = None if item is None else self._clock() - item[1]
        if (robot not in self.robots or robot in self._invalidated
                or not self._mqtt_started or not self._connected.is_set()
                or not self._subscription_done.is_set() or self._online.get(robot) is not True
                or self._sessions.get(robot) != session
                or item is None or item[2] != self._connection_generation
                or age < 0 or age > self._max_state_age):
            raise ValueError('fresh physical start evidence is unavailable')
        state = item[0]
        if (state.get('robot_session') != session or state.get('map_id') != map_id
                or state.get('last_node') != node
                or 'error' not in state or state['error'] is not None
                or 'rejected' not in state or state['rejected'] is not None):
            raise ValueError('physical start report does not match')
        if source_task is None:
            if (state.get('status') != 'IDLE' or 'order_id' not in state or state['order_id'] is not None
                    or 'seq' not in state or state['seq'] is not None
                    or 'released_until' not in state or state['released_until'] is not None):
                raise ValueError('physical robot has no fresh initial idle report')
        else:
            proof = self._proven.get(robot)
            if (state.get('status') not in STOPPED or state.get('order_id') != source_task
                    or type(state.get('seq')) is not int or state['seq'] != source_seq
                    or state.get('released_until') != node or proof is None
                    or proof[0] != (source_task, source_seq)
                    or self._clock() - proof[1] < 0 or self._clock() - proof[1] >= proof_ttl):
                raise ValueError('physical robot has no fresh source stop report')
        return (self._connection_generation, robot, session, node, map_id, source_task, source_seq)

    def _queued_ack_evidence_locked(self, robot, session, order, original, *, proof_ttl=30.0):
        """Validate live state for broker-ACK bookkeeping, not physical completion."""
        try:
            current = self._start_evidence_locked(
                robot, session, original[3], original[4], source_task=original[5],
                source_seq=original[6], proof_ttl=proof_ttl)
            if current == original:
                return True
        except ValueError:
            pass
        item = self._latest.get(robot)
        age = None if item is None else self._clock() - item[1]
        expected = (order['order_id'], session, order['seq'], order['map_id'], order['released_until'])
        if (robot in self._invalidated or not self._connected.is_set() or not self._subscription_done.is_set()
                or self._online.get(robot) is not True or self._sessions.get(robot) != session
                or self._motion_permits.get(robot) != expected or item is None
                or item[2] != self._connection_generation or age < 0 or age > self._max_state_age):
            raise ValueError('physical acknowledgement evidence is unavailable')
        state = item[0]
        if (state.get('robot_session') != session or state.get('order_id') != order['order_id']
                or type(state.get('seq')) is not int or state['seq'] != order['seq']
                or state.get('map_id') != order['map_id'] or state.get('released_until') != order['released_until']
                or state.get('status') not in ('MOVING', 'WAITING', 'IDLE')
                or state.get('error', object()) is not None or state.get('rejected', object()) is not None
                or (state['status'] != 'MOVING' and state.get('last_node') != order['released_until'])):
            raise ValueError('physical acknowledgement report does not match queued order')
        return True

    # Runtime binding ------------------------------------------------------
    def bind(self, runtime, sessions):
        """Start proving stops for ``runtime`` with the bound ``sessions``."""
        self._runtime = runtime
        self._sessions = dict(sessions)
        runtime.gate.bind_physical_watcher(self)
        self._thread = threading.Thread(target=self._run, name='physical-state-proof', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(5)
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def _run(self):
        last_retry = 0.0
        while not self._stop.wait(self._poll):
            try:
                for robot in self.robots:
                    state = self.latest(robot)
                    if state is None:
                        continue
                    if not self._check_session(robot, state):
                        continue
                    self._maybe_prove(robot, state)
                now = self._clock()
                if now - last_retry >= 1.0:
                    last_retry = now
                    self._retry_events()
            except Exception as exc:   # keep the worker alive; report the category
                self.last_error = type(exc).__name__
                self._log('worker error: %s: %s' % (type(exc).__name__, exc))

    def _robot_tasks(self, robot):
        return tuple(task for task, (r, _s) in self._runtime._tasks.items() if r == robot)

    def _check_session(self, robot, state):
        with self._lock:
            if robot in self._invalidated:
                return False
            if state['robot_session'] == self._sessions.get(robot):
                return True
            invalidated = self._latch_robots_locked((robot,))
        self._revoke_robots(invalidated)
        self._log('%s restarted (session %s -> %s); its tasks are blocked. Check the robot and '
                  'restart the server runtime.' % (robot, self._sessions.get(robot), state['robot_session']))
        return False

    def _maybe_prove(self, robot, state):
        token = self._token(robot, state)
        if token is None or not self._check_session(robot, state):
            return
        task, seq = state.get('order_id'), state.get('seq')
        rt = self._runtime
        # An expected next order in motion has no stop proof. A fresh stop at
        # its sent sequence must later be proven against the durable order.
        if (state.get('status') == 'MOVING' and state.get('error', object()) is None
                and state.get('rejected', object()) is None):
            with self._lock:
                expected = (state.get('order_id'), state.get('robot_session'), state.get('seq'),
                            state.get('map_id'), state.get('released_until'))
                invalidated = (self._latch_robots_locked((robot,))
                               if self._motion_permits.get(robot) != expected else set())
            self._revoke_robots(invalidated)
            return
        if (state.get('status') not in STOPPED or not isinstance(task, str) or type(seq) is not int
                or 'error' not in state or state['error'] is not None
                or 'rejected' not in state or state['rejected'] is not None
                or rt._tasks.get(task, (None,))[0] != robot
                or state.get('last_node') is None or state.get('last_node') != state.get('released_until')):
            # When this robot has a prior proof, let the shared boundary revoke
            # it. This remains a poll-time check, not an authorization fence
            # for other event or publishing paths.
            if self._proven.get(robot) is not None:
                rt._physical_prove(self, robot, state, token)
            return
        key = (task, seq)
        previous = self._proven.get(robot)
        if previous is not None and previous[0] == key and self._clock() - previous[1] < self._refresh:
            return
        if not rt._physical_prove(self, robot, state, token):
            return
        if previous is None or previous[0] != key:   # refreshes of the same stop stay quiet
            self._log('%s stop proof: %s#%d at %s' % (robot, task, seq, state['last_node']))

    def _retry_events(self):
        rt = self._runtime
        events = rt.pending_events()['events']
        occupied = set(getattr(rt.receiver, '_occupied_events', ()))
        for item in events:
            robot, event_id, task = item['robot_id'], item['event_id'], item['task_id']
            if robot not in self.robots or robot in self._invalidated:
                continue
            state = self.latest(robot)
            if (state is None or not self._check_session(robot, state)
                    or state.get('status') not in STOPPED or state.get('order_id') != task
                    or self._token(robot, state) is None
                    or self._proven.get(robot, (None,))[0] != (task, state.get('seq'))):
                continue
            key = (robot, event_id)
            try:
                if key in occupied:
                    result = rt.receiver.retry_occupied_event(robot, event_id)   # submits on success
                    if result.get('status') == 'advanced':
                        self._log('%s %s advanced after a shared segment cleared' % (robot, task))
                    continue
                result = rt.retry(robot, event_id)
            except Exception:
                continue   # proof not ready yet or still occupied; try again next round
            if isinstance(result, dict) and result.get('status') == 'advanced' and result.get('next_seq'):
                rt.submit(task, result['next_seq'], rt._tasks[task][1])
                self._log('%s %s#%d submitted after retry' % (robot, task, result['next_seq']))
            elif isinstance(result, dict) and result.get('status') == 'finished':
                self._log('%s %s finished at its goal' % (robot, task))
