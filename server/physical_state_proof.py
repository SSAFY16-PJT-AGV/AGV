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
import sqlite3
import threading
import time
from contextlib import closing

import paho.mqtt.client as mqtt

from server.recovery import _snapshot
from server.robot_ids import is_physical_robot

FROM_STATE = 'from_state'
MAX_STATE_BYTES = 64 * 1024
STOPPED = ('WAITING', 'IDLE')


class PhysicalStateWatcher:
    def __init__(self, robots, *, client, host, port, connect_timeout=5.0, refresh_s=15.0,
                 poll_s=0.2, log=None, clock=time.monotonic):
        robots = tuple(robots)
        if not robots or any(not is_physical_robot(r) for r in robots) or len(set(robots)) != len(robots):
            raise ValueError('unique physical- robot IDs are required')
        self.robots = robots
        self._client, self._host, self._port = client, host, port
        self._timeout, self._refresh, self._poll = float(connect_timeout), float(refresh_s), float(poll_s)
        self._clock = clock
        self._log = log or (lambda msg: print('[physical] ' + msg, flush=True))
        self._lock = threading.Lock()
        self._latest = {}          # robot -> (state dict, monotonic time)
        self._sessions = {}        # robot -> session bound into the runtime
        self._invalidated = set()  # robots whose session changed
        self._proven = {}          # robot -> ((task, seq), monotonic time)
        self._runtime = None
        self._stop = threading.Event()
        self._thread = None
        self._connected = threading.Event()
        self.last_error = None

    # MQTT -----------------------------------------------------------------
    def start(self):
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        if self._client.connect(self._host, self._port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError('physical state connection failed')
        if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError('physical state loop failed')
        if not self._connected.wait(self._timeout):
            self.stop()
            raise RuntimeError('physical state connection timed out')
        return self

    def _on_connect(self, client, _userdata, _flags, code, _properties):
        if code.is_failure:
            return
        for robot in self.robots:
            client.subscribe('agv/%s/state' % robot, qos=0)
        self._connected.set()

    def _on_message(self, _client, _userdata, message):
        try:
            parts = message.topic.split('/')
            if len(parts) != 3 or parts[0] != 'agv' or parts[2] != 'state' or parts[1] not in self.robots:
                return
            if message.retain or not isinstance(message.payload, bytes) or len(message.payload) > MAX_STATE_BYTES:
                return   # a retained state may belong to an earlier robot process
            state = json.loads(message.payload.decode('utf-8'))
            if not isinstance(state, dict) or not isinstance(state.get('robot_session'), str):
                return
        except (ValueError, UnicodeError, AttributeError):
            return
        with self._lock:
            self._latest[parts[1]] = (state, self._clock())

    # Session discovery -----------------------------------------------------
    def wait_sessions(self, timeout):
        """Return {robot: robot_session} from live states, or raise after timeout."""
        deadline = self._clock() + float(timeout)
        while True:
            with self._lock:
                found = {r: self._latest[r][0]['robot_session'] for r in self.robots if r in self._latest}
            if len(found) == len(self.robots):
                return found
            if self._clock() >= deadline:
                missing = sorted(set(self.robots) - set(found))
                raise RuntimeError('no live agv/<id>/state from physical robot(s): ' + ', '.join(missing))
            time.sleep(0.05)

    def latest(self, robot):
        with self._lock:
            item = self._latest.get(robot)
        return None if item is None else dict(item[0])

    # Runtime binding ------------------------------------------------------
    def bind(self, runtime, sessions):
        """Start proving stops for ``runtime`` with the bound ``sessions``."""
        self._runtime = runtime
        self._sessions = dict(sessions)
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
        if robot in self._invalidated:
            return False
        if state['robot_session'] == self._sessions.get(robot):
            return True
        self._invalidated.add(robot)
        tasks = self._robot_tasks(robot)
        try:
            if tasks:
                self._runtime.gate.invalidate_many(tasks)
        finally:
            self._log('%s restarted (session %s -> %s); its tasks are blocked. Check the robot and '
                      'restart the server runtime.' % (robot, self._sessions.get(robot), state['robot_session']))
        return False

    def _maybe_prove(self, robot, state):
        task, seq = state.get('order_id'), state.get('seq')
        rt = self._runtime
        if (state.get('status') not in STOPPED or not isinstance(task, str) or type(seq) is not int
                or rt._tasks.get(task, (None,))[0] != robot
                or state.get('last_node') is None or state.get('last_node') != state.get('released_until')):
            return
        key = (task, seq)
        previous = self._proven.get(robot)
        if previous is not None and previous[0] == key and self._clock() - previous[1] < self._refresh:
            return
        session = rt._tasks[task][1]
        try:
            with closing(sqlite3.connect('file:%s?mode=ro' % rt.path, uri=True)) as db:
                db.execute('BEGIN')
                expected = _snapshot(db, task, field_required=rt._field_simulation)
        except Exception:
            return   # no active allocation (finished task) or the order is still being sent
        if (expected['robot_id'] != robot or expected['seq'] != seq
                or expected['stopped_at'] != state['last_node'] or expected['map_id'] != state.get('map_id')):
            return   # the server has already moved on, or the robot reports another order
        challenge = rt.begin(task, session)['challenge']
        rt.complete(task, session, challenge, robot, task, seq, expected['map_id'],
                    expected['stopped_at'], expected['order_digest'], True)
        self._proven[robot] = (key, self._clock())
        if previous is None or previous[0] != key:   # refreshes of the same stop stay quiet
            self._log('%s stop proof: %s#%d at %s' % (robot, task, seq, expected['stopped_at']))

    def _retry_events(self):
        rt = self._runtime
        events = rt.pending_events()['events']
        occupied = set(getattr(rt.receiver, '_occupied_events', ()))
        for item in events:
            robot, event_id, task = item['robot_id'], item['event_id'], item['task_id']
            if robot not in self.robots or robot in self._invalidated:
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
