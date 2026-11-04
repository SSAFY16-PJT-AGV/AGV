"""Lifecycle owner for the proposed, isolated-simulation control workflow.

The local endpoint and simulated identities are not authentication or network
isolation. Never use this runtime for a physical robot or a shared broker.
"""
import copy
import math
import os
import sqlite3
import threading
import time
from contextlib import closing, contextmanager

import paho.mqtt.client as mqtt

from server.allocation_store import AllocationStore, _id
from server.coordinator import Coordinator
from server.dispatch import RobotCandidate
from server.order_publisher import OrderPublisher, _topic
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress
from sim.event_inbox import SimEventInbox
from sim.event_receiver import SimEventReceiver
from sim.workflow import SimProgressWorkflow


class _DatabaseOwner:
    """Exclusive, nonblocking OS lock; released by the OS on process death."""
    def __init__(self, path):
        self.path = os.path.realpath(os.fspath(path)) + '.runtime.lock'
        self.file = None

    def acquire(self):
        file = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            file.close()
            raise RuntimeError('simulation database is already in use') from exc
        self.file = file

    def release(self):
        file, self.file = self.file, None
        if file is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()


class SimRuntime:
    def __init__(self, db_path, map_data, robot_sessions, *, port, client_id,
                 host='127.0.0.1', connect_timeout=5.0, ack_timeout=3.0):
        if host != '127.0.0.1':
            raise ValueError('simulation broker must use explicit IPv4 loopback')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid simulation broker port')
        _id(client_id, 'client_id')
        if len(client_id.encode('utf-8')) > 128:
            raise ValueError('client_id too long')
        for value in (connect_timeout, ack_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 30:
                raise ValueError('timeout must be between 0 and 30 seconds')
        if not isinstance(robot_sessions, dict) or not robot_sessions:
            raise ValueError('static robot sessions are required')
        self._tasks, self._sessions = {}, copy.deepcopy(robot_sessions)
        for robot, sessions in self._sessions.items():
            _topic(robot)
            if not isinstance(sessions, dict) or not sessions:
                raise ValueError('each robot needs explicitly mapped tasks')
            for task, session in sessions.items():
                _id(task, 'task_id'); _id(session, 'robot_session')
                if task in self._tasks:
                    raise ValueError('task mapped to multiple robots')
                self._tasks[task] = (robot, session)
        self.path = os.path.realpath(os.fspath(db_path))
        if os.fspath(db_path) == ':memory:':
            raise ValueError('file-backed database required')
        self.map_data, self.host, self.port = copy.deepcopy(map_data), host, port
        self.client_id, self.connect_timeout, self.ack_timeout = client_id, float(connect_timeout), float(ack_timeout)
        self._lock = threading.RLock()
        self._owner = _DatabaseOwner(self.path)
        self._started = False
        self._stopped = False
        self._prepared = {}
        self.store = self.gate = self.progress = self.inbox = self.receiver = self.workflow = self._client = None

    def _new_client(self, *, client_id='', persistent=False):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                             clean_session=not persistent, protocol=mqtt.MQTTv311,
                             reconnect_on_failure=False)
        client.connect_timeout = self.connect_timeout
        return client

    @staticmethod
    def _close_client(client):
        if client is not None:
            try:
                client.disconnect()
            finally:
                client.loop_stop()

    def _connect_publisher(self):
        client = self._new_client()
        connected, failure = threading.Event(), []
        def on_connect(_client, _userdata, _flags, code, _properties):
            if code.is_failure:
                failure.append(True)
            connected.set()
        client.on_connect = on_connect
        try:
            if client.connect(self.host, self.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('simulation publisher connection failed')
            if client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('simulation publisher loop failed')
            if not connected.wait(self.connect_timeout) or failure or not client.is_connected():
                raise RuntimeError('simulation publisher connection timed out')
            return client, OrderPublisher(client, enabled=True)
        except BaseException:
            self._close_client(client)
            raise

    @contextmanager
    def _publisher_factory(self, robot, task, seq):
        if self._tasks.get(task, (None,))[0] != robot:
            raise ValueError('unmapped publisher task')
        client, publisher = self._connect_publisher()
        try:
            yield publisher
        finally:
            self._close_client(client)

    def start(self):
        with self._lock:
            if self._started or self._stopped:
                raise RuntimeError('runtime may only be started once')
            self._owner.acquire()
            try:
                self.store = AllocationStore(self.path); self.store.initialize()
                self.gate = RecoveryGate(self.path); self.gate.initialize()
                self.progress = SegmentProgress(self.store, self.map_data, simulation_enabled=True, recovery_gate=self.gate)
                self.progress.initialize()
                mapping = {robot: tuple(tasks) for robot, tasks in self._sessions.items()}
                self.inbox = SimEventInbox(self.path, robot_tasks=mapping, simulation_enabled=True)
                self.inbox.initialize()
                self._client = self._new_client(client_id=self.client_id, persistent=True)
                self.receiver = SimEventReceiver(self._client, self.progress, inbox=self.inbox,
                    recovery_gate=self.gate, robot_tasks=mapping, robot_sessions=self._sessions,
                    simulation_enabled=True)
                self.workflow = SimProgressWorkflow(self.receiver, robot_sessions=self._sessions,
                    publisher_factory=self._publisher_factory, simulation_enabled=True)
                self.workflow.start()
                if self._client.connect(self.host, self.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('simulation receiver connection failed')
                if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('simulation receiver loop failed')
                deadline = time.monotonic() + self.connect_timeout
                while not self.receiver.ready:
                    if self.receiver._closed or time.monotonic() >= deadline:
                        raise RuntimeError('simulation receiver was not ready')
                    time.sleep(0.01)
                self._started = True
            except BaseException:
                self._shutdown()
                raise
        return self

    def _ready(self):
        if not self._started or self._stopped or self.receiver is None or not self.receiver.ready:
            raise RuntimeError('simulation runtime is not ready')

    def status(self):
        with self._lock:
            return {'ready': bool(self._started and not self._stopped and self.receiver and self.receiver.ready),
                    'simulation_only': True,
                    'last_workflow_error': self.workflow.last_error if self.workflow else None}

    def pending_events(self):
        """List bounded metadata even when the broker is disconnected."""
        with self._lock:
            if not self._started or self._stopped:
                raise RuntimeError('simulation runtime is not started')
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('CREATE TEMP TABLE allowed(robot_id TEXT, task_id TEXT, PRIMARY KEY(robot_id,task_id))')
                db.executemany('INSERT INTO allowed VALUES (?,?)',
                               ((robot, task) for task, (robot, _session) in self._tasks.items()))
                rows = db.execute("SELECT i.robot_id,i.event_id,i.task_id,i.seq FROM sim_event_inbox i "
                                  "JOIN allowed a ON a.robot_id=i.robot_id AND a.task_id=i.task_id "
                                  "WHERE i.state='pending' ORDER BY i.robot_id,i.event_id LIMIT 101").fetchall()
            return {'events': [dict(zip(('robot_id', 'event_id', 'task_id', 'seq'), row))
                               for row in rows[:100]], 'truncated': len(rows) > 100}

    def task_status(self, task_id):
        """Read a bounded, consistent allocation snapshot; never authorize movement."""
        with self._lock:
            if not self._started or self._stopped:
                raise RuntimeError('simulation runtime is not started')
            if task_id not in self._tasks:
                raise ValueError('task is not configured')
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('BEGIN')
                task = db.execute('SELECT status FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                if task is None:
                    raise ValueError('task does not exist')
                job = db.execute('SELECT robot_id,map_id,released_until FROM allocation_jobs WHERE task_id=?',
                                 (task_id,)).fetchone()
                first = db.execute('SELECT state FROM allocation_outbox WHERE task_id=?', (task_id,)).fetchone()
                later = db.execute('SELECT seq,state FROM progress_orders WHERE task_id=? AND seq>1 ORDER BY seq LIMIT 100',
                                   (task_id,)).fetchall()
                resources = db.execute('SELECT kind,resource_id FROM allocation_occupancy WHERE task_id=? '
                                       'ORDER BY kind,resource_id LIMIT 501', (task_id,)).fetchall()
            if (job is None) != (first is None) or (job is not None and job[0] != self._tasks[task_id][0]):
                raise RuntimeError('inconsistent simulation allocation')
            orders = ([{'seq': 1, 'state': first[0]}] if first else [])
            orders.extend({'seq': seq, 'state': state} for seq, state in later[:99])
            return {'task_id': task_id, 'status': task[0], 'robot_id': job[0] if job else None,
                    'map_id': job[1] if job else None, 'released_until': job[2] if job else None,
                    'orders': orders, 'orders_truncated': len(later) > 99,
                    'occupancy': [list(row) for row in resources[:500]],
                    'occupancy_truncated': len(resources) > 500}

    def create_task(self, task_id):
        with self._lock:
            self._ready()
            if task_id not in self._tasks:
                raise ValueError('task is not statically mapped')
            self.store.create_task(task_id)
            return {'task_id': task_id, 'state': 'PENDING'}

    def prepare(self, task_id, destination, candidates, action):
        with self._lock:
            self._ready()
            robot, _session = self._tasks.get(task_id, (None, None))
            if robot is None or not isinstance(candidates, list) or not candidates:
                raise ValueError('mapped task and candidates required')
            checked = []
            for item in candidates:
                if not isinstance(item, dict) or set(item) != {'robot_id', 'node', 'available'} or item['robot_id'] != robot:
                    raise ValueError('candidate must match the configured task robot')
                checked.append(RobotCandidate(item['robot_id'], item['node'], item['available']))
            client, publisher = self._connect_publisher()
            try:
                coordinator = Coordinator(self.store, self.map_data, publisher, ack_timeout=self.ack_timeout)
                with self.receiver._dispatch_lock, self.gate.serialized():
                    self._ready()
                    with self.gate._state_lock:
                        if task_id in self.gate._blocked:
                            raise RuntimeError('robot recovery is blocked')
                        generation = self.gate._generations.get(task_id, 0)
                    result = coordinator.prepare(task_id, destination, checked, action)
            except BaseException:
                self._close_client(client)
                raise
            self._prepared[task_id] = (coordinator, client, generation)
            return result

    def publish(self, task_id):
        with self._lock:
            self._ready()
            pair = self._prepared.pop(task_id, None)
            if pair is None:
                raise ValueError('task was not prepared by this runtime')
            coordinator, client, generation = pair
            try:
                with self.receiver._dispatch_lock, self.gate.serialized():
                    self._ready()
                    with self.gate._state_lock:
                        if task_id in self.gate._blocked or self.gate._generations.get(task_id, 0) != generation:
                            raise RuntimeError('robot connection or recovery changed after preparation')
                    return coordinator.publish(task_id)
            finally:
                self._close_client(client)

    def _identity(self, task_id, session):
        pair = self._tasks.get(task_id)
        if pair is None or pair[1] != session:
            raise ValueError('task or session not configured')
        return pair[0]

    def reset_unallocated(self, task_id, session):
        """Explicitly unblock a never-allocated simulation task; no movement proof."""
        with self._lock, self.receiver._dispatch_lock:
            self._ready()
            self._identity(task_id, session)
            self.gate.reset_unallocated(task_id)
            return {'task_id': task_id, 'reset': True}

    def begin(self, task_id, session):
        with self._lock, self.receiver._dispatch_lock:
            self._ready(); self._identity(task_id, session)
            return {'challenge': self.gate.begin(task_id, session)}

    def complete(self, task_id, session, challenge, robot_id, order_id, seq, map_id, stopped_at, order_digest, stopped):
        with self._lock, self.receiver._dispatch_lock:
            self._ready()
            if self._identity(task_id, session) != robot_id or order_id != task_id:
                raise ValueError('recovery identity is not configured')
            self.gate.complete(task_id, session, challenge, robot_id=robot_id, order_id=order_id,
                               seq=seq, map_id=map_id, stopped_at=stopped_at,
                               order_digest=order_digest, stopped=stopped)
            return {'task_id': task_id, 'reconciled': True}

    def retry(self, robot_id, event_id):
        with self._lock, self.receiver._dispatch_lock:
            self._ready()
            if robot_id not in self._sessions:
                raise ValueError('robot is not configured')
            return self.inbox.retry(robot_id, event_id, self.progress)

    def submit(self, task_id, seq, session):
        with self._lock, self.receiver._dispatch_lock:
            self._ready(); self._identity(task_id, session)
            self.workflow.submit_pending(task_id, seq, session)
            return {'task_id': task_id, 'seq': seq, 'queued': True}

    def _shutdown(self):
        self._stopped = True
        errors = []
        actions = []
        if self.workflow is not None:
            actions.append(self.workflow.stop)
        if self._client is not None:
            actions.append(lambda: self._close_client(self._client))
        actions.extend(lambda client=client: self._close_client(client)
                       for _coordinator, client, _generation in self._prepared.values())
        self._prepared.clear()
        for action in actions:
            try:
                action()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError('simulation runtime cleanup failed') from errors[0]
        self._owner.release()

    def stop(self):
        with self._lock:
            if not self._stopped:
                self._shutdown()
