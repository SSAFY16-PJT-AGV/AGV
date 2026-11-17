"""Lifecycle owner for the proposed, isolated-simulation control workflow.

The local endpoint and simulated identities are not authentication or network
isolation. Never use this runtime for a physical robot or a shared broker.
"""
import copy
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

import paho.mqtt.client as mqtt

from server.allocation_store import AllocationStore, _id
from server.coordinator import Coordinator
from server.dispatch import RobotCandidate
from server.order_publisher import OrderPublisher, _topic
from server.mqtt_security import configure_mqtt_client, read_mqtt_security
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress
from sim.event_inbox import SimEventInbox
from sim.event_receiver import SimEventReceiver
from sim.workflow import SimProgressWorkflow
from sim.order_inbox import _decode


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
                 host='127.0.0.1', connect_timeout=5.0, ack_timeout=3.0, simulation_store=None):
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
        if simulation_store is not None:
            from server.sim_handoff_store import SimHandoffStore
            if type(simulation_store) is not SimHandoffStore:
                raise ValueError('fresh isolated simulation store required')
            simulation_store.validate_bound(self.path)
        self._simulation_store = simulation_store
        self.map_data, self.host, self.port = copy.deepcopy(map_data), host, port
        self.client_id, self.connect_timeout, self.ack_timeout = client_id, float(connect_timeout), float(ack_timeout)
        self._mqtt_security = read_mqtt_security()
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
        configure_mqtt_client(client, *self._mqtt_security)
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
            if self._simulation_store is not None:
                self._simulation_store.validate_bound(self.path)
            self._owner.acquire()
            try:
                self.store = self._simulation_store or AllocationStore(self.path)
                if self._simulation_store is not None:
                    # Repeat after the process-wide lock: a validated file may
                    # have been replaced or modified while ownership was pending.
                    self.store.validate_bound(self.path, check_schema=True)
                if self._simulation_store is not None and self.store.reopened:
                    expected = SegmentProgress(self.store, self.map_data, simulation_enabled=True)
                    with closing(sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
                        tasks = {row[0] for row in db.execute('SELECT task_id FROM tasks')}
                        jobs = db.execute('SELECT task_id,robot_id,map_id FROM allocation_jobs').fetchall()
                        meta = dict(db.execute("SELECT key,value FROM allocation_meta WHERE key IN ('active_map','map_fingerprint')"))
                    if not tasks.issubset(self._tasks):
                        raise ValueError('existing simulation tasks are not fully mapped')
                    if any(self._tasks.get(task, (None,))[0] != robot or map_id != expected._map_id
                           for task, robot, map_id in jobs):
                        raise ValueError('existing simulation allocation mapping changed')
                    if (jobs or meta) and meta != {'active_map': expected._map_id,
                                                  'map_fingerprint': expected._fingerprint}:
                        raise ValueError('existing simulation map differs from stored map')
                self.store.initialize()
                self.gate = RecoveryGate(self.path); self.gate.initialize()
                if self._simulation_store is not None and self.store.reopened:
                    # Never let a queued event or a stale publisher move a
                    # restored task before a separately verified reconciliation.
                    self.gate.invalidate_many(tuple(self._tasks))
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

    def reconnect(self):
        """Reconnect a failed receiver without replacing the recovery epoch.

        Only the isolated MQTT transport and workflow are recreated. Existing
        tasks remain blocked until separately reconciled; this does not itself
        authorize motion or release any reserved resource.
        """
        with self._lock:
            if not self._started or self._stopped:
                raise RuntimeError('simulation runtime is not started')
            if self.receiver is not None and self.receiver.ready:
                raise RuntimeError('simulation receiver is already connected')
            if self.receiver is not None and not self.receiver._closed:
                raise RuntimeError('previous simulation receiver is still closing')
            old_workflow, old_client = self.workflow, self._client
            self.workflow = self.receiver = self._client = None
            if old_workflow is not None:
                old_workflow.stop()
            self._close_client(old_client)
            for _coordinator, client, _generation in self._prepared.values():
                self._close_client(client)
            self._prepared.clear()

            mapping = {robot: tuple(tasks) for robot, tasks in self._sessions.items()}
            client = self._new_client(client_id=self.client_id, persistent=True)
            receiver = SimEventReceiver(client, self.progress, inbox=self.inbox,
                recovery_gate=self.gate, robot_tasks=mapping, robot_sessions=self._sessions,
                simulation_enabled=True)
            workflow = SimProgressWorkflow(receiver, robot_sessions=self._sessions,
                publisher_factory=self._publisher_factory, simulation_enabled=True)
            self._client, self.receiver, self.workflow = client, receiver, workflow
            try:
                workflow.start()
                if client.connect(self.host, self.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('simulation receiver reconnection failed')
                if client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError('simulation receiver loop failed')
                deadline = time.monotonic() + self.connect_timeout
                while not receiver.ready:
                    if receiver._closed or time.monotonic() >= deadline:
                        raise RuntimeError('simulation receiver was not ready')
                    time.sleep(.01)
            except BaseException:
                receiver._close_and_invalidate(RuntimeError('simulation receiver reconnection failed'))
                workflow.stop()
                self._close_client(client)
                raise
            return self.status()

    def status(self):
        with self._lock:
            return {'ready': bool(self._started and not self._stopped and self.receiver and self.receiver.ready),
                    'simulation_only': True,
                    'last_workflow_error': self.workflow.last_error if self.workflow else None}

    def restore_completed_executor(self, executor, *, task_id, session, event_id,
                                   action_ack, expected_twin_id):
        """Reconstruct a historical completed *simulator* stop from durable records.

        The ACK must independently come from the caller's durable order journal.
        This method provides no current Unreal world, pose, or carrying proof and
        does not unblock recovery or authorize motion. A fresh external world
        check is mandatory before any simulation can use the returned evidence.
        """
        from sim.order_executor import SimOrderExecutor
        from sim.event_inbox import _event
        from server.segment_progress import _resources
        if type(executor) is not SimOrderExecutor:
            raise ValueError('a fresh simulation executor is required')
        for value, name in ((task_id, 'task_id'), (session, 'session'),
                            (event_id, 'event_id'), (expected_twin_id, 'twin_id')):
            _id(value, name)
        if (not isinstance(action_ack, dict)
                or set(action_ack) != {'order_id', 'action_id', 'agv_id', 'action', 'applied'}
                or action_ack.get('order_id') != task_id or action_ack.get('agv_id') != expected_twin_id
                or action_ack.get('action') != 'place' or action_ack.get('applied') is not True):
            raise ValueError('durable successful simulated place acknowledgement required')
        _id(action_ack['action_id'], 'action_id')
        robot = executor._inbox.robot_id
        if self._tasks.get(task_id) != (robot, session):
            raise ValueError('recovery task identity differs from static simulation mapping')
        with self._lock, self.receiver._dispatch_lock, executor._lock, executor._inbox._lock:
            self._ready()
            if self._simulation_store is None or not self.store.reopened:
                raise ValueError('reopened isolated simulation required')
            if executor.snapshot()['status'] != 'idle':
                raise ValueError('a fresh idle simulation executor is required')
            with self.gate.transaction() as db:
                self.progress._check_database(db)
                robot_id, route, stopped = self.progress._job(db, task_id, robot)
                seq, payload, state, order = self.progress._latest(db, task_id, robot_id, route, stopped)
                parsed, canonical, digest = _decode(payload.encode('utf-8'), robot, self.progress._map_id)
                if (parsed != order or state != 'acked' or stopped != route[-1]
                        or order['released_until'] != stopped
                        or order['action_at_end'].get('type') != 'unload'
                        or self.progress._occupancy(db, task_id) != {('node', stopped)}
                        or db.execute('SELECT task_id FROM sim_active_robots WHERE robot_id=?', (robot,)).fetchone() != (task_id,)
                        or db.execute('SELECT 1 FROM sim_handoff_transfers WHERE source_task_id=? OR action_id=?',
                                      (task_id, action_ack['action_id'])).fetchone()):
                    raise ValueError('latest completed allocation is not an active terminal stop')
                row = db.execute('SELECT i.payload,i.digest,i.robot_session,i.task_id,i.seq,i.state,i.result, '
                                 'p.digest,p.task_id,p.seq,p.next_seq,p.result FROM sim_event_inbox i '
                                 'JOIN progress_events p ON p.robot_id=i.robot_id AND p.event_id=i.event_id '
                                 'WHERE i.robot_id=? AND i.event_id=?', (robot, event_id)).fetchone()
                if row is None:
                    raise ValueError('completed final event unavailable')
                event, event_json, event_digest = _event(row[0].encode('ascii'))
                result = json.loads(row[6]) if row[6] else None
                cleared = set(self.progress._segment(route, stopped).resources) - {('node', stopped)}
                if (event_json != row[0] or event_digest != row[1] or row[1] != row[7]
                        or row[2:6] != (session, task_id, seq, 'applied')
                        or row[8:12] != (task_id, seq, None, 'finished')
                        or event['event_id'] != event_id or event['robot_session'] != session
                        or event['order_id'] != task_id or event['seq'] != seq
                        or event['map_id'] != self.progress._map_id or event['stopped_at'] != stopped
                        or _resources(event['cleared_resources']) != cleared
                        or not isinstance(result, dict) or result.get('status') != 'finished'
                        or result.get('task_id') != task_id or result.get('next_seq') is not None):
                    raise ValueError('completed final event differs from allocation')
                uri = Path(executor._inbox.path).resolve().as_uri() + '?mode=ro'
                with closing(sqlite3.connect(uri, uri=True)) as inbox:
                    identity = inbox.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone()
                    latest = inbox.execute('SELECT order_id,seq,digest,payload,state FROM sim_inbox_orders '
                                           'ORDER BY rowid DESC LIMIT 1').fetchone()
                if (identity != (robot, self.progress._map_id)
                        or latest != (task_id, seq, digest, canonical, 'completed')):
                    raise ValueError('completed robot inbox differs from final allocation')
                evidence = {'task_id': task_id, 'session': session, 'robot_id': robot, 'seq': seq,
                            'stopped_at': stopped, 'event_id': event_id,
                            'map_id': self.progress._map_id, 'digest': digest}
                executor._restore_completed(order, digest, evidence)
                return copy.deepcopy(evidence)

    def reconcile_moving(self, robot_id, task_id, session, robot, checkpoint):
        """Permit only the frozen simulated executor's current reserved segment.

        This is an in-process continuity check, not a stop proof, a clearance
        event, or an authorization for physical hardware or a new segment.
        """
        from sim.mqtt_order_robot import SimMqttOrderRobot
        from server.sim_handoff_store import SimHandoffStore
        from server.sim_order_authorization import SimOrderAuthorizer
        if type(robot) is not SimMqttOrderRobot:
            raise ValueError('the original simulated MQTT robot is required')
        with robot._lock:
            if not robot.ready or not robot.check_recovery_checkpoint(checkpoint):
                raise ValueError('simulated executor continuity is not established')
            guard = robot._authorization_guard
            authorizer = getattr(guard, '__self__', None)
            if (type(authorizer) is not SimOrderAuthorizer or authorizer.runtime is not self
                    or authorizer.robot_id != robot_id
                    or getattr(guard, '__func__', None) is not SimOrderAuthorizer.guard):
                raise ValueError('the simulated robot requires its matching runtime guard')
            executor, inbox = robot.executor, robot.executor._inbox
            with self._lock, self.receiver._dispatch_lock, self.gate.serialized(), executor._lock, inbox._lock:
                self._ready()
                if self._identity(task_id, session) != robot_id or inbox.robot_id != robot_id:
                    raise ValueError('simulated robot or session differs')
                state = executor.snapshot()
                order, receipt = state['order'], state['receipt']
                if (state['status'] != 'moving' or not isinstance(order, dict)
                        or not isinstance(receipt, dict) or receipt.get('new') is not True
                        or receipt.get('state') != 'accepted'):
                    raise ValueError('no active simulated moving order')
                payload = json.dumps(order, ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).encode('utf-8')
                parsed, canonical, digest = _decode(payload, robot_id, self.progress._map_id)
                if (parsed['order_id'] != task_id or receipt.get('order_id') != task_id
                        or receipt.get('seq') != parsed['seq'] or receipt.get('digest') != digest
                        or (task_id, parsed['seq'], digest) not in inbox._owned):
                    raise ValueError('simulated inbox receipt does not match')
                with closing(sqlite3.connect(inbox.path, timeout=5, isolation_level=None)) as local:
                    if (local.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone()
                            != (robot_id, self.progress._map_id)
                            or local.execute('SELECT digest,payload,state FROM sim_inbox_orders WHERE order_id=? AND seq=?',
                                             (task_id, parsed['seq'])).fetchone() != (digest, canonical, 'accepted')):
                        raise ValueError('durable simulated inbox differs')
                with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
                    db.execute('BEGIN')
                    try:
                        self.gate._active(db)
                        self.progress._check_database(db)
                        owner, path, stop = self.progress._job(db, task_id, robot_id)
                        seq, _raw, status, stored = self.progress._latest(db, task_id, owner, path, stop)
                        encoded = json.dumps(stored, sort_keys=True, ensure_ascii=True,
                                             allow_nan=False, separators=(',', ':'))
                        if (status not in ('uncertain', 'acked') or seq != parsed['seq']
                                or encoded != canonical or self.progress._occupancy(db, task_id)
                                != set(self.progress._segment(path, stop).resources)):
                            raise ValueError('current command or reservation differs')
                        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sim_active_robots'").fetchone()
                        if (type(self.store) is SimHandoffStore and table is None) or (table is not None and
                                db.execute('SELECT task_id FROM sim_active_robots WHERE robot_id=?',
                                           (robot_id,)).fetchone() != (task_id,)):
                            raise ValueError('simulated robot ownership differs')
                        if not robot.ready or not robot.check_recovery_checkpoint(checkpoint):
                            raise ValueError('simulated executor changed during reconciliation')
                        db.execute('COMMIT')
                        with self.gate._state_lock:
                            if task_id not in self.gate._blocked:
                                raise ValueError('simulated task is not recovery blocked')
                            generation = self.gate._generations.get(task_id, 0)
                            self.gate._moving_permits[task_id] = (generation, robot_id, seq, digest, id(authorizer))
                    except BaseException:
                        if db.in_transaction:
                            db.execute('ROLLBACK')
                        raise
            return {'task_id': task_id, 'seq': seq, 'resume_current_segment': True}

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

    def create_task(self, task_id, *, reset_unallocated=False):
        with self._lock:
            self._ready()
            if task_id not in self._tasks:
                raise ValueError('task is not statically mapped')
            if type(reset_unallocated) is not bool:
                raise ValueError('reset_unallocated must be boolean')
            with self.receiver._dispatch_lock:
                self._ready()
                # A duplicate insert must fail before any recovery reset.
                self.store.create_task(task_id)
                if reset_unallocated:
                    with self.gate._state_lock:
                        blocked = task_id in self.gate._blocked
                    if blocked:
                        self.gate.reset_unallocated(task_id)
            return {'task_id': task_id, 'state': 'PENDING'}

    def prepare(self, task_id, destination, candidates, action, *, handoff_store=None):
        with self._lock:
            self._ready()
            robot, _session = self._tasks.get(task_id, (None, None))
            if robot is None or not isinstance(candidates, list) or not candidates:
                raise ValueError('mapped task and candidates required')
            if handoff_store is not None:
                from server.sim_handoff_store import SimHandoffCoordinatorStore
                if (type(handoff_store) is not SimHandoffCoordinatorStore
                        or self._simulation_store is None or self.store is not self._simulation_store
                        or handoff_store._store is not self.store or handoff_store._target != task_id
                        or handoff_store._robot != robot or handoff_store._context['gate'] is not self.gate
                        or handoff_store._context['progress'] is not self.progress
                        or handoff_store._context['receiver'] is not self.receiver):
                    raise ValueError('handoff store does not match this simulation task and runtime')
                self._simulation_store.validate_bound(self.path)
            checked = []
            for item in candidates:
                if not isinstance(item, dict) or set(item) != {'robot_id', 'node', 'available'} or item['robot_id'] != robot:
                    raise ValueError('candidate must match the configured task robot')
                checked.append(RobotCandidate(item['robot_id'], item['node'], item['available']))
            client, publisher = self._connect_publisher()
            try:
                coordinator = Coordinator(handoff_store or self.store, self.map_data, publisher, ack_timeout=self.ack_timeout)
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

    def invalidate_robot(self, robot_id):
        """Revoke only one disconnected simulated robot's task proofs.

        The fleet coordinator must first freeze its ticks, and is responsible
        for validating and reconnecting that robot before any resumption.
        This neither changes reservations nor proves a physical stop.
        """
        with self._lock:
            self._ready()
            if robot_id not in self._sessions:
                raise ValueError('simulated robot is not configured')
            with self.receiver._dispatch_lock:
                self._ready()
                self.gate.invalidate_many(tuple(self._sessions[robot_id]))
            return {'robot_id': robot_id, 'invalidated': True}

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
