"""Isolated, authenticated same-session ledger reconciliation; never moves robots.

This is explicitly opt-in and does not unblock the recovery gate.  A signed
robot report is still a self-report, not physical position or clearance proof.
"""
import copy
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

from server.recovery import RecoveryGate, _id, _snapshot, _decode
from server.route_occurrence import visit_indices_from_orders
from server.physical_evidence_observer import PhysicalEvidenceInbox, PhysicalEvidenceWatcher, is_physical_robot
from sim.event_inbox import _event

DOMAIN = b'AGV-RECONCILE-V1\0'
COMMON = frozenset(('v', 'kind', 'nonce', 'server_epoch', 'robot_id', 'robot_session',
                    'order_id', 'seq', 'order_digest', 'map_id', 'release_index',
                    'stopped_at', 'event_id', 'event_digest', 'mac'))
MAX_BYTES = 16 * 1024


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False,
                          separators=(',', ':')).encode('ascii')
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError('invalid reconciliation message') from exc


def _mac(message, key):
    raw = _canonical({k: v for k, v in message.items() if k != 'mac'})
    return hmac.new(key, DOMAIN + raw, hashlib.sha256).hexdigest()


def _hex(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _signed(value, key):
    result = copy.deepcopy(value)
    result['mac'] = _mac(result, key)
    return result


class PhysicalReconciler:
    """Persist and confirm a stopped robot report without authorizing motion."""
    def __init__(self, gate, inbox, robot_keys, *, isolated=False, clock=time.monotonic, ttl=10.0):
        if isolated is not True or not isinstance(gate, RecoveryGate) or not isinstance(inbox, PhysicalEvidenceInbox):
            raise ValueError('isolated reconciliation requires the existing gate and inbox')
        if gate.path != os.path.normcase(os.path.realpath(inbox.path)):
            raise ValueError('gate and inbox must share one database')
        if (not isinstance(gate._physical_watcher, PhysicalEvidenceWatcher)
                or getattr(getattr(gate._physical_watcher, '_runtime', None), 'gate', None) is not gate):
            raise ValueError('the original physical watcher must be bound to the gate')
        if not callable(clock) or isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or not 0 < ttl <= 10:
            raise ValueError('invalid reconciliation deadline')
        if not isinstance(robot_keys, dict) or not robot_keys:
            raise ValueError('per-robot keys required')
        keys = {}
        for robot, key in robot_keys.items():
            if not is_physical_robot(robot) or robot not in inbox.robot_tasks or not isinstance(key, bytes) or len(key) < 32:
                raise ValueError('unique physical robot keys of at least 32 bytes required')
            if key in keys.values():
                raise ValueError('each robot must use a distinct key')
            keys[robot] = key
        self.gate, self.inbox, self._keys = gate, inbox, keys
        self._watcher = gate._physical_watcher
        self._clock, self._ttl = clock, float(ttl)
        self._lock = threading.RLock()
        self._local = threading.local()
        with self.gate.transaction() as db:
            self.gate._active(db)
            db.execute('CREATE TABLE IF NOT EXISTS physical_reconcile ('
                'nonce TEXT PRIMARY KEY, epoch TEXT NOT NULL, robot TEXT NOT NULL, task TEXT NOT NULL, '
                'session TEXT NOT NULL, snapshot TEXT NOT NULL, deadline REAL NOT NULL, '
                'state TEXT NOT NULL, probe TEXT NOT NULL, response_hash TEXT, commit_json TEXT, '
                'event_state TEXT, ack_hash TEXT, watcher_generation INTEGER NOT NULL, '
                'event_json TEXT, event_digest TEXT, gate_generation INTEGER NOT NULL, '
                'seq INTEGER NOT NULL, release_index INTEGER NOT NULL, event_id TEXT)')

    @contextmanager
    def _transaction(self):
        watcher = self.gate._physical_watcher
        if watcher is None or watcher is not self._watcher:
            raise ValueError('original physical watcher is unavailable')
        # Gate -> watcher -> SQLite is the same ordering as the runtime. Keep
        # the watcher locked until COMMIT, not merely through the last check.
        with self._lock, self.gate._operation_lock, watcher._lock, self.gate.transaction() as db:
            self._local.commit_nonce = None
            try:
                yield db
                nonce = self._local.commit_nonce
                if nonce is not None:
                    # Last operation before RecoveryGate commits: no earlier
                    # check may stand in for the deadline/generation check.
                    self._record(db, nonce)
            finally:
                self._local.commit_nonce = None

    def _bound_generation(self, robot, task, session, expected):
        watcher = self.gate._physical_watcher
        if (watcher is None or watcher is not self._watcher
                or getattr(getattr(watcher, '_runtime', None), 'gate', None) is not self.gate):
            raise ValueError('original physical watcher is unavailable')
        with watcher._lock:
            item = watcher._latest.get(robot)
            age = None if item is None else watcher._clock() - item[1]
            if (robot not in watcher.robots or robot not in watcher._invalidated
                    or watcher._sessions.get(robot) != session
                    or getattr(watcher._runtime, '_tasks', {}).get(task) != (robot, session)
                    or not watcher._connected.is_set() or not watcher._subscription_done.is_set()
                    or watcher._online.get(robot) is not True or item is None
                    or item[2] != watcher._connection_generation or not 0 <= age <= watcher._max_state_age
                    or item[0].get('robot_session') != session):
                raise ValueError('same original session and fresh reconnected watcher are required')
            state = item[0]
            stopped = (state.get('status') in ('WAITING', 'IDLE') and state.get('error') is None
                       or state.get('status') == 'ERROR' and state.get('error') in ('link_lost', 'event_unconfirmed'))
            if (not stopped or 'error' not in state or 'rejected' not in state or state['rejected'] is not None
                    or state.get('order_id') != task or type(state.get('seq')) is not int
                    or state['seq'] != expected['seq'] or state.get('map_id') != expected['map_id']
                    or state.get('last_node') != expected['stopped_at']
                    or state.get('released_until') != expected['stopped_at']):
                raise ValueError('live robot state is not the expected stopped order')
            return watcher._connection_generation

    def _time(self):
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError('invalid reconciliation clock')
        return float(now)

    def _record(self, db, nonce):
        row = db.execute('SELECT epoch,robot,task,session,snapshot,deadline,state,probe,response_hash,commit_json,event_state,ack_hash,watcher_generation,gate_generation,event_json,event_digest,seq,release_index,event_id '
                         'FROM physical_reconcile WHERE nonce=?', (nonce,)).fetchone()
        now = self._time()
        if row is None or row[0] != self.gate._epoch or not row[5] - self._ttl <= now < row[5]:
            raise ValueError('unknown, stale, or expired reconciliation')
        self.gate._active(db)
        with self.gate._state_lock:
            if row[2] not in self.gate._blocked or self.gate._generations.get(row[2], 0) != row[13]:
                raise ValueError('physical task must remain blocked')
        current = _snapshot(db, row[2], field_required=self.gate._field_required, _physical_observation=True)
        if current['snapshot_digest'] != row[4]:
            raise ValueError('allocation or reservation changed during reconciliation')
        if self._bound_generation(row[1], row[2], row[3], current) != row[12]:
            raise ValueError('server connection generation changed')
        return row

    @staticmethod
    def _release_index(db, task, seq):
        first = db.execute('SELECT payload FROM allocation_outbox WHERE task_id=?', (task,)).fetchone()
        if first is None:
            raise ValueError('initial order is missing')
        has_history = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='progress_orders'").fetchone()
        rows = (db.execute('SELECT seq,payload FROM progress_orders WHERE task_id=? AND seq>1 AND seq<=? ORDER BY seq',
                           (task, seq)).fetchall() if has_history else [])
        if [row[0] for row in rows] != list(range(2, seq + 1)):
            raise ValueError('order history is incomplete')
        history = [_decode(first[0])] + [_decode(row[1]) for row in rows]
        if history[-1].get('action_at_end') != {'type': 'none'}:
            raise ValueError('load or unload results cannot be reconciled as hold evidence')
        return visit_indices_from_orders(history)[-1]

    def begin(self, robot, task, session, *, event_id=None, event_digest=None):
        if robot not in self._keys or task not in self.inbox.robot_tasks.get(robot, ()):
            raise ValueError('unknown robot or task')
        _id(session, 'robot_session')
        if (event_id is None) != (event_digest is None) or (event_id is not None and
             (not isinstance(event_id, str) or not event_id or not _hex(event_digest))):
            raise ValueError('event identity and digest must be provided together')
        if event_id is not None:
            _id(event_id, 'event_id')
        with self._transaction() as db:
            self.gate._active(db)
            with self.gate._state_lock:
                if task not in self.gate._blocked:
                    raise ValueError('robot must be blocked before reconciliation')
                gate_generation = self.gate._generations.get(task, 0)
            current = _snapshot(db, task, field_required=self.gate._field_required, _physical_observation=True)
            if current['robot_id'] != robot:
                raise ValueError('task does not belong to robot')
            watcher_generation = self._bound_generation(robot, task, session, current)
            index = self._release_index(db, task, current['seq'])
            now = self._time()
            probe = _signed({'v': 1, 'kind': 'probe', 'nonce': secrets.token_hex(32),
                'server_epoch': self.gate._epoch, 'robot_id': robot, 'robot_session': session,
                'order_id': task, 'seq': current['seq'], 'order_digest': current['order_digest'],
                'map_id': current['map_id'], 'release_index': index, 'stopped_at': current['stopped_at'],
                'event_id': event_id, 'event_digest': event_digest}, self._keys[robot])
            db.execute('INSERT INTO physical_reconcile VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (probe['nonce'], self.gate._epoch, robot, task, session, current['snapshot_digest'],
                 now + self._ttl, 'probed', _canonical(probe).decode('ascii'), None, None, None, None,
                 watcher_generation, None, None, gate_generation, current['seq'], index, None))
            self._local.commit_nonce = probe['nonce']
            self._record(db, probe['nonce'])
        return probe

    def _inspect_event(self, db, row, event, event_json, event_digest):
        # A robot sequence/visit must not acquire two event identities, even
        # under distinct nonces or an event receiver racing this transaction.
        history = db.execute('SELECT event_id,event_digest FROM physical_reconcile WHERE robot=? AND task=? '
                             'AND session=? AND seq=? AND release_index=? AND event_digest IS NOT NULL',
                             (row[1], row[2], row[3], row[16], row[17])).fetchall()
        if any(old != (event['event_id'], event_digest) for old in history):
            raise ValueError('different event already reported for this visit')
        incoming = db.execute('SELECT event_id,payload,digest,state,task_id,robot_session,seq FROM sim_event_inbox '
                              'WHERE robot_id=? AND task_id=? AND robot_session=? AND seq=?',
                              (row[1], row[2], row[3], event['seq'])).fetchall()
        if len(incoming) > 1:
            raise ValueError('multiple inbox events for this visit')
        if not incoming:
            return 'not_observed'
        existing = incoming[0]
        if (existing[0] != event['event_id'] or existing[1] != event_json or existing[2] != event_digest
                or existing[3] not in ('pending', 'applied') or existing[4:] != (row[2], row[3], event['seq'])):
            raise ValueError('existing event differs')
        if existing[3] == 'applied':
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='progress_events'").fetchone():
                raise ValueError('applied event has no progress journal')
            journal = db.execute('SELECT digest,task_id,seq,result FROM progress_events WHERE robot_id=? AND event_id=?',
                                 (row[1], event['event_id'])).fetchone()
            if journal not in ((event_digest, row[2], event['seq'], 'advanced'),
                               (event_digest, row[2], event['seq'], 'finished')):
                raise ValueError('applied event has no matching progress')
        return existing[3]

    @contextmanager
    def publication(self, nonce, kind):
        """Fence one nonblocking MQTT queue operation with all live checks.

        Caller must not wait for PUBACK in this context. The yielded bytes are
        the exact signed, persisted message; replays do not extend its TTL.
        """
        if kind not in ('probe', 'commit') or not _hex(nonce):
            raise ValueError('invalid reconciliation publication')
        with self._transaction() as db:
            row = self._record(db, nonce)
            self._local.commit_nonce = nonce
            if kind == 'probe':
                if row[6] != 'probed':
                    raise ValueError('reconciliation probe is no longer current')
                body = row[7]
            else:
                if row[6] != 'commit_sent' or row[9] is None or row[14] is None:
                    raise ValueError('reconciliation commit is unavailable')
                event, event_json, event_digest = _event(row[14].encode('ascii'))
                if event_digest != row[15] or event['event_id'] != row[18]:
                    raise ValueError('stored evidence is inconsistent')
                self._inspect_event(db, row, event, event_json, event_digest)
                body = row[9]
            yield row[1], body.encode('ascii')
            self._record(db, nonce)

    def _check(self, value, kind, extra):
        if not isinstance(value, dict) or set(value) != COMMON | set(extra) or value.get('v') != 1 or type(value.get('v')) is not int or value.get('kind') != kind:
            raise ValueError('invalid reconciliation schema')
        if len(_canonical(value)) > MAX_BYTES:
            raise ValueError('reconciliation message too large')
        robot = value.get('robot_id')
        if robot not in self._keys or not _hex(value.get('mac')) or not hmac.compare_digest(value['mac'], _mac(value, self._keys[robot])):
            raise ValueError('invalid reconciliation authentication')
        if not _hex(value.get('nonce')) or not _hex(value.get('server_epoch')) or not _hex(value.get('robot_nonce')):
            raise ValueError('invalid reconciliation nonce')
        if type(value.get('connection_generation')) is not int or value['connection_generation'] < 1:
            raise ValueError('invalid robot connection generation')

    def accept_snapshot(self, message):
        response = copy.deepcopy(message)
        self._check(response, 'snapshot', ('connection_generation', 'robot_nonce', 'event'))
        event, event_json, event_digest = _event(response['event'])
        if (not isinstance(response['event_id'], str) or not response['event_id']
                or not _hex(response['event_digest']) or response['event_digest'] != event_digest
                or event['event_id'] != response['event_id'] or event['robot_session'] != response['robot_session']
                or event['order_id'] != response['order_id'] or event['seq'] != response['seq']
                or event['map_id'] != response['map_id'] or event['stopped_at'] != response['stopped_at']):
            raise ValueError('event does not match reconciliation')
        response_hash = hashlib.sha256(_canonical(response)).hexdigest()
        with self._transaction() as db:
            row = self._record(db, response['nonce'])
            self._local.commit_nonce = response['nonce']
            probe = json.loads(row[7])
            for name in COMMON - {'kind', 'mac', 'event_id', 'event_digest'}:
                if type(response[name]) is not type(probe[name]) or response[name] != probe[name]:
                    raise ValueError('snapshot differs from probe')
            if probe['event_id'] is not None and (probe['event_id'] != response['event_id'] or probe['event_digest'] != event_digest):
                raise ValueError('event differs from probe')
            if row[6] in ('commit_sent', 'confirmed'):
                if row[8] != response_hash:
                    raise ValueError('nonce reused with different response')
                return json.loads(row[9])
            if row[6] != 'probed':
                raise ValueError('invalid reconciliation state')
            event_state = self._inspect_event(db, row, event, event_json, event_digest)
            fields = {k: v for k, v in response.items() if k not in ('mac', 'event')}
            fields.update(kind='commit', decision='hold_confirmed')
            commit = _signed(fields, self._keys[row[1]])
            db.execute("UPDATE physical_reconcile SET state='commit_sent', response_hash=?,commit_json=?,event_state=?,event_json=?,event_digest=?,event_id=? WHERE nonce=?",
                       (response_hash, _canonical(commit).decode('ascii'), event_state, event_json, event_digest, event['event_id'], response['nonce']))
            self._record(db, response['nonce'])
        return commit

    def accept_ack(self, message):
        message = copy.deepcopy(message)
        self._check(message, 'commit_ack', ('connection_generation', 'robot_nonce', 'decision'))
        ack_hash = hashlib.sha256(_canonical(message)).hexdigest()
        with self._transaction() as db:
            row = self._record(db, message['nonce'])
            self._local.commit_nonce = message['nonce']
            if row[6] not in ('commit_sent', 'confirmed') or row[9] is None:
                raise ValueError('reconciliation commit is unavailable')
            expected = json.loads(row[9])
            expected['kind'] = 'commit_ack'
            expected = _signed(expected, self._keys[row[1]])
            if not hmac.compare_digest(_canonical(message), _canonical(expected)):
                raise ValueError('reconciliation acknowledgement differs')
            if row[6] == 'confirmed' and row[11] != ack_hash:
                raise ValueError('conflicting acknowledgement replay')
            event, event_json, event_digest = _event(row[14].encode('ascii'))
            if event_digest != row[15] or event['event_id'] != row[18]:
                raise ValueError('stored evidence is inconsistent')
            event_state = self._inspect_event(db, row, event, event_json, event_digest)
            db.execute("UPDATE physical_reconcile SET state='confirmed', ack_hash=?,event_state=? WHERE nonce=?", (ack_hash, event_state, message['nonce']))
            self._record(db, message['nonce'])
            return {'status': 'signed_ack_recorded', 'event_state': event_state,
                    'current_hold_proven': False, 'motion_authorized': False}
