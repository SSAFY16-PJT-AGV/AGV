"""Authorize virtual MQTT orders against the current reserved segment.

This protects an isolated simulation workflow; an ID, broker PUBACK or database
snapshot is not robot authentication, physical clearance or a drive permit.
"""
from contextlib import closing, contextmanager
import json
import sqlite3

from server.sim_handoff_store import SimHandoffStore
from sim.order_inbox import _decode


class SimOrderAuthorizer:
    """Hold the simulation's normal lock order through durable inbox accept."""

    ROBOT = 'sim-r1'

    def __init__(self, runtime):
        self.runtime = runtime

    def __call__(self, payload):
        try:
            with self.guard(payload):
                return True
        except Exception:
            return False

    @contextmanager
    def guard(self, payload):
        """Reject mismatches; retain receiver/gate and DB read locks until exit.

        The caller must use a separate inbox database; it may persist the
        accepted order while this read transaction retains reservation state.
        """
        runtime = self.runtime
        with runtime._lock:
            runtime._ready()
            receiver, gate, progress = runtime.receiver, runtime.gate, runtime.progress
            if (receiver is None or gate is None or progress is None
                    or receiver.progress is not progress or receiver.gate is not gate
                    or progress._gate is not gate or progress._enabled is not True):
                raise ValueError('isolated simulated workflow is not consistent')
            with receiver._dispatch_lock:
                runtime._ready()
                with gate.serialized():
                    with closing(sqlite3.connect(runtime.path, timeout=5, isolation_level=None)) as db:
                        db.execute('BEGIN')
                        try:
                            gate._active(db)
                            progress._check_database(db)
                            parsed, canonical, _digest = _decode(payload, self.ROBOT, progress._map_id)
                            task = parsed['order_id']
                            mapping = runtime._tasks.get(task)
                            if (not isinstance(mapping, tuple) or len(mapping) != 2 or mapping[0] != self.ROBOT
                                    or receiver.robot_sessions.get(self.ROBOT, {}).get(task) != mapping[1]):
                                raise ValueError('order is not mapped to this simulated session')
                            with gate._state_lock:
                                if task in gate._blocked:
                                    raise ValueError('simulated recovery is blocked')
                            robot, path, stop = progress._job(db, task, self.ROBOT)
                            seq, _stored_payload, state, stored = progress._latest(db, task, robot, path, stop)
                            encoded = json.dumps(stored, sort_keys=True, ensure_ascii=True,
                                                 allow_nan=False, separators=(',', ':'))
                            if (state not in ('uncertain', 'acked') or parsed['seq'] != seq
                                    or canonical != encoded):
                                raise ValueError('order is not the currently attempted simulated payload')
                            expected = set(progress._segment(path, stop).resources)
                            if progress._occupancy(db, task) != expected:
                                raise ValueError('current occupancy differs from the reserved segment')
                            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sim_active_robots'").fetchone()
                            if type(runtime.store) is SimHandoffStore and table is None:
                                raise ValueError('active simulated robot ownership is missing')
                            if table is not None and db.execute(
                                    'SELECT task_id FROM sim_active_robots WHERE robot_id=?', (self.ROBOT,)
                                    ).fetchone() != (task,):
                                raise ValueError('simulated robot has a different active task')
                            runtime._ready()
                            yield
                            db.execute('COMMIT')
                        except BaseException:
                            if db.in_transaction:
                                db.execute('ROLLBACK')
                            raise
