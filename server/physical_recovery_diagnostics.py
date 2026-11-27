"""Read-only diagnostics for explicitly bound isolated physical claims.

This module does not open an MQTT connection, publish, prove a physical stop,
release a reservation or authorize a robot to move.
"""
from contextlib import closing
import math
from pathlib import Path
import sqlite3

from server.physical_evidence_observer import PhysicalEvidenceWatcher
from server.recovery import RecoveryGate, _snapshot


class PhysicalRecoveryDiagnostics:
    """Read the live gate, its original watcher, and non-executable ledger."""

    def __init__(self, gate, watcher, *, isolated=False):
        if (isolated is not True or type(gate) is not RecoveryGate
                or type(watcher) is not PhysicalEvidenceWatcher
                or gate._physical_watcher is not watcher
                or watcher._runtime.gate is not gate
                or not 1 <= len(watcher._runtime._tasks) <= 32):
            raise ValueError('original isolated gate and watcher required')
        self.gate, self.watcher = gate, watcher

    def snapshot(self):
        gate, watcher = self.gate, self.watcher
        # Match the reconciliation lock ordering. A report is only an
        # observation of this instant, never an authorization to act.
        with gate._operation_lock, watcher._lock:
            if (gate._physical_watcher is not watcher or watcher._runtime.gate is not gate
                    or watcher._stopped):
                raise ValueError('original isolated watcher unavailable')
            tasks = watcher._runtime._tasks
            if not 1 <= len(tasks) <= 32:
                raise ValueError('observation mapping unavailable')
            with gate._state_lock:
                blocked = set(gate._blocked)
            with closing(sqlite3.connect(Path(gate.path).as_uri() + '?mode=ro', uri=True,
                                         isolation_level=None, timeout=5)) as db:
                db.execute('BEGIN')
                gate._active(db)
                has_ledger = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                        "AND name='physical_reconcile'").fetchone() is not None
                now = watcher._clock()
                if type(now) not in (int, float) or not math.isfinite(now):
                    raise ValueError('observation clock unavailable')
                result = []
                for task, (robot, session) in sorted(tasks.items()):
                    if task not in blocked or robot not in watcher._invalidated:
                        raise ValueError('physical task is not blocked')
                    allocation = _snapshot(db, task, field_required=gate._field_required,
                                           _physical_observation=True)
                    if allocation['robot_id'] != robot or watcher._sessions.get(robot) != session:
                        raise ValueError('physical task binding differs')
                    item = watcher._latest.get(robot)
                    if (not watcher._connected.is_set() or not watcher._subscription_done.is_set()
                            or watcher._online.get(robot) is not True):
                        report = 'unavailable'
                    elif item is None:
                        report = 'not_observed'
                    elif (len(item) != 4 or item[2] != watcher._connection_generation
                          or not isinstance(item[0], dict) or item[0].get('robot_session') != session
                          or type(item[1]) not in (int, float) or not math.isfinite(item[1])):
                        raise ValueError('physical observation differs')
                    else:
                        age = now - item[1]
                        report = 'recent_report' if 0 <= age <= watcher._max_state_age else 'stale'
                    record, event_state = 'not_recorded', None
                    if has_ledger:
                        row = db.execute('SELECT state,event_state,ack_hash FROM physical_reconcile '
                                         'WHERE epoch=? AND robot=? AND task=? ORDER BY rowid DESC LIMIT 1',
                                         (gate._epoch, robot, task)).fetchone()
                        if row is not None:
                            if row[0] not in ('probed', 'commit_sent', 'confirmed'):
                                raise ValueError('invalid reconciliation record')
                            if row[0] == 'confirmed':
                                if (row[1] not in ('not_observed', 'pending', 'applied')
                                        or not isinstance(row[2], str) or len(row[2]) != 64
                                        or any(c not in '0123456789abcdef' for c in row[2])):
                                    raise ValueError('invalid recorded acknowledgement')
                                record, event_state = 'recorded', row[1]
                    result.append({'task_id': task, 'robot_id': robot, 'gate': 'blocked',
                                   'report': report, 'signed_record': record, 'event_state': event_state,
                                   'motion_authorized': False, 'physical_stop_proven': False})
                db.execute('COMMIT')
        return {'scope': 'isolated_physical_observation', 'tasks': result}
