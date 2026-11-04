"""Bounded, explicitly enabled progression workflow for isolated simulations."""
import os
import queue
import sqlite3
import threading
from contextlib import closing
from types import MappingProxyType

from server.allocation_store import _id
from server.progress_publisher import ProgressPublisher
from server.recovery import RecoveryError
from sim.event_inbox import _event


class SimProgressWorkflow:
    def __init__(self, receiver, *, robot_sessions, publisher_factory,
                 simulation_enabled=False, queue_size=16, on_result=None, on_error=None):
        if simulation_enabled is not True:
            raise ValueError('isolated simulation must be explicitly enabled')
        if type(queue_size) is not int or not 1 <= queue_size <= 256:
            raise ValueError('queue_size must be between 1 and 256')
        if not callable(publisher_factory):
            raise ValueError('dedicated publisher factory is required')
        if any(value is not None and not callable(value) for value in (on_result, on_error)):
            raise ValueError('observers must be callable')
        if (receiver.progress._gate is not receiver.gate or not receiver.progress._enabled
                or os.path.normcase(os.path.realpath(receiver.progress.store.path))
                != os.path.normcase(os.path.realpath(receiver.inbox.path))
                or os.path.normcase(os.path.realpath(receiver.gate.path))
                != os.path.normcase(os.path.realpath(receiver.inbox.path))
                or receiver.inbox.robot_tasks != receiver.robot_tasks):
            raise ValueError('receiver progress, gate and inbox must share the database')
        if not isinstance(robot_sessions, dict) or set(robot_sessions) != set(receiver.robot_tasks):
            raise ValueError('sessions must match receiver robots and tasks')
        mapping, tasks = {}, {}
        for robot, sessions in robot_sessions.items():
            if not isinstance(sessions, dict) or set(sessions) != set(receiver.robot_tasks[robot]):
                raise ValueError('sessions must match receiver robots and tasks')
            mapping[robot] = MappingProxyType(dict(sessions))
            for task, session in sessions.items():
                _id(task, 'task_id'); _id(session, 'robot_session')
                if task in tasks:
                    raise ValueError('task must map to one robot')
                tasks[task] = (robot, session)
        self.receiver, self.progress = receiver, receiver.progress
        self.robot_sessions = MappingProxyType(mapping)
        self._tasks = MappingProxyType(tasks)
        self._factory, self._on_result, self._on_error = publisher_factory, on_result, on_error
        self._queue = queue.Queue(maxsize=queue_size)
        self._lock = threading.RLock()
        self._publication_lock = threading.Lock()
        self._active = set()
        self._started = False
        self._stopping = False
        self._thread = None
        self._observer = self._event_result
        self.last_error = None
        self._deferred_error = None

    def start(self):
        with self.receiver._dispatch_lock:
            with self._lock:
                if self._started or self._stopping or self.receiver._closed:
                    raise ValueError('workflow cannot be started')
                if self.receiver.on_result is not None:
                    raise ValueError('receiver already has a result observer')
                self._started = True
                self.receiver.on_result = self._observer
                self._thread = threading.Thread(target=self._run, name='sim-progress-workflow', daemon=True)
                self._thread.start()

    def _record_error(self, category):
        with self._lock:
            self.last_error = category
            self._deferred_error = category

    def _deliver_error(self):
        with self._lock:
            error, self._deferred_error = self._deferred_error, None
        if error is not None and self._on_error is not None:
            try:
                self._on_error(error)
            except Exception:
                pass

    def _submit(self, task, seq, session, *, payload=None):
        _id(task, 'task_id'); _id(session, 'robot_session')
        if type(seq) is not int or seq < 2:
            raise ValueError('invalid next sequence')
        mapping = self._tasks.get(task)
        if mapping is None or mapping[1] != session:
            raise ValueError('unknown task or session')
        with self._lock:
            if not self._started or self._stopping or not self.receiver.ready:
                raise ValueError('workflow or receiver is not ready')
            if task in self._active:
                raise ValueError('task already queued or in progress')
            try:
                self._queue.put_nowait((mapping[0], task, seq, session, payload))
            except queue.Full as exc:
                raise ValueError('workflow queue is full') from exc
            self._active.add(task)

    def submit_pending(self, task_id, seq, session):
        """Explicitly submit an existing pending order after fresh recovery."""
        self._submit(task_id, seq, session)

    def _event_result(self, result):
        if not isinstance(result, dict) or result.get('status') != 'advanced':
            return
        try:
            task = result['task_id']
            session = self._tasks[task][1]
            if not isinstance(result['payload'], str):
                raise ValueError('invalid advanced payload')
            self._submit(task, result['next_seq'], session, payload=result['payload'])
        except Exception:
            # Never call arbitrary observers or perform disk/network I/O on
            # the MQTT callback thread. A worker reports the fixed category.
            self._record_error('submission rejected')

    def _verify_auto(self, robot, task, seq, session, payload):
        with closing(sqlite3.connect(self.inbox_path, isolation_level=None)) as db:
            db.execute('BEGIN')
            rows = db.execute(
                'SELECT i.payload,i.digest,i.robot_session,i.task_id,i.seq,p.digest,p.task_id,p.seq,p.next_seq,p.result,i.event_id '
                'FROM sim_event_inbox i JOIN progress_events p ON p.robot_id=i.robot_id AND p.event_id=i.event_id '
                "WHERE i.robot_id=? AND i.task_id=? AND i.seq=? AND i.state='applied'",
                (robot, task, seq - 1)).fetchall()
            order = db.execute('SELECT payload,state FROM progress_orders WHERE task_id=? AND seq=?',
                               (task, seq)).fetchone()
            if len(rows) != 1 or order != (payload, 'pending'):
                raise ValueError('automatic event and order do not match')
            row = rows[0]
            event, canonical, digest = _event(row[0].encode('ascii'))
            if (canonical != row[0] or digest != row[1] or digest != row[5]
                    or row[2:5] != (session, task, seq - 1)
                    or row[6:10] != (task, seq - 1, seq, 'advanced')
                    or event['event_id'] != row[10]
                    or event['robot_session'] != session or event['order_id'] != task
                    or event['seq'] != seq - 1):
                raise ValueError('automatic event and session do not match')

    @property
    def inbox_path(self):
        return self.receiver.inbox.path

    def _run(self):
        while True:
            self._deliver_error()
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                with self._lock:
                    if self._stopping:
                        return
                continue
            robot, task, seq, session, payload = item
            stage = 'receiver not ready'
            try:
                with self._lock:
                    if self._stopping:
                        continue
                if not self.receiver.ready:
                    raise ValueError('receiver is not ready')
                if payload is not None:
                    stage = 'event verification failed'
                    self._verify_auto(robot, task, seq, session, payload)
                stage = 'publisher setup or publication failed'
                with self._factory(robot, task, seq) as publisher:
                    if getattr(publisher, '_client', None) is self.receiver.client:
                        raise ValueError('publisher must not reuse the receiver client')
                    runner = ProgressPublisher(self.progress, publisher)
                    with self._publication_lock:
                        with self._lock:
                            if self._stopping or not self.receiver.ready:
                                raise ValueError('workflow or receiver is not ready')
                        result = runner.publish(task, seq, session)
                if self._on_result is not None:
                    try:
                        self._on_result(result)
                    except Exception:
                        self._record_error('result observer failed')
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    category = 'broker acknowledgement timeout; outcome uncertain'
                elif isinstance(exc, RecoveryError):
                    category = 'recovery proof rejected'
                elif isinstance(exc, sqlite3.Error):
                    category = 'database operation failed'
                else:
                    category = stage
                self._record_error(category)
            finally:
                with self._lock:
                    self._active.discard(task)
                self._queue.task_done()
                self._deliver_error()

    def stop(self):
        with self.receiver._dispatch_lock:
            with self._lock:
                self._stopping = True
                if self.receiver.on_result is self._observer:
                    self.receiver.on_result = None
                thread = self._thread
                discarded = False
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    self._active.discard(item[1])
                    self._queue.task_done()
                    discarded = True
                if discarded:
                    self._record_error('queued work discarded')
        if thread is not None and thread is not threading.current_thread():
            # The dispatch lock has already been released; callbacks never
            # wait on this lock. An admitted publish is allowed to finish.
            with self._publication_lock:
                pass
            thread.join()
