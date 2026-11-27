"""Live, per-stop virtual reroute evidence; never a physical clearance proof.

The lease is deliberately process-local and bound to one exact server guard,
robot, broker connections, key and recovery epoch. The original challenge row
is never modified. A rollback leaves no matching applied event and makes the
new lease unusable.
"""
from dataclasses import dataclass
from contextlib import contextmanager, closing
import hashlib
import hmac
import json
import sqlite3
import threading

from server.allocation_store import _verify_field_global
from server.segments import plan_segment
from server.segment_progress import _resources
from server.sim_reroute_activation import _canonical
from sim.event_inbox import _event
from sim.order_inbox import _decode


@dataclass(frozen=True)
class _Lease:
    task: str
    transition: str
    session: str
    robot_id: str
    generation: int
    gate_epoch: str
    parent_seq: int
    event_id: str
    event_digest: str
    event_payload: str
    stop: str
    stop_index: int
    receipt_digest: str
    seq: int
    prior_steps: tuple
    next_payload: bytes | None
    final: bool
    created: float
    expires: float
    refs: tuple
    key: bytes
    challenge_digest: str


def _fingerprint(steps):
    return tuple((row['seq'], row['stop_index'], row['event_id'],
                  hashlib.sha256(row['next_payload']).hexdigest(), row['state']) for row in steps)


class FreshStopProof:
    """Issue fresh short-lived authority only from a validated live virtual stop."""
    def __init__(self, guard):
        from server.sim_reroute_execute_wire import SimRerouteExecutionGuard
        if type(guard) is not SimRerouteExecutionGuard:
            raise ValueError('a bound virtual generation guard is required')
        self.guard = guard
        self._lease = None
        self._last_now = None
        self._clock_failed = False
        self._motion_owner = None
        self._motion_lock = None

    @contextmanager
    def motion_fence(self, robot):
        """Acquire the unchanged non-reentrant lock and record its real owner."""
        if self.guard.robot_receiver.robot is not robot:
            raise ValueError('motion fence robot changed')
        owner = threading.get_ident()
        if self._motion_owner == owner:
            raise RuntimeError('virtual motion fence cannot be reentered')
        lock = robot._motion_fence
        with lock:
            self._motion_owner, self._motion_lock = owner, lock
            try:
                yield
            finally:
                self._motion_owner, self._motion_lock = None, None

    def _now(self):
        if self._clock_failed:
            raise ValueError('simulated proof clock is invalidated')
        try:
            now = self.guard.runtime.gate._now()
        except Exception:
            self._lease = None
            self._clock_failed = True
            raise
        if self._last_now is not None and now < self._last_now:
            self._lease = None
            self._clock_failed = True
            raise ValueError('simulated proof clock moved backwards')
        self._last_now = now
        return now

    def _check_fences(self, db, robot):
        guard, runtime = self.guard, self.guard.runtime
        endpoint = guard._execution_receiver
        receiver, gate = runtime.receiver, runtime.gate
        if (endpoint is None or receiver is None or gate is None or getattr(gate._local, 'db', None) is not db
                or not getattr(db, 'in_transaction', False)):
            raise ValueError('the active virtual recovery transaction is required')
        locks = (guard.robot_receiver._lifecycle_lock, guard.robot_receiver._connection_fence,
                 guard.activation_receiver.endpoint._connection_fence, endpoint.endpoint._connection_fence,
                 robot._lock, runtime._lock, receiver._dispatch_lock, runtime.inbox._lock)
        if (any(not lock._is_owned() for lock in locks) or self._motion_owner != threading.get_ident()
                or self._motion_lock is not robot._motion_fence or not robot._motion_fence.locked()):
            raise ValueError('all virtual execution fences must be held')
        runtime._ready()

    def _binding(self, task_id, robot):
        guard, runtime = self.guard, self.guard.runtime
        endpoint = guard._execution_receiver
        receiver, gate = runtime.receiver, runtime.gate
        from server.sim_reroute_activate_wire import _check_binding
        from server.sim_reroute_execute_wire import _live_endpoint
        if (_check_binding(runtime, task_id, guard.receipt_inbox, guard.robot_receiver) is not robot
                or not robot.ready or robot._event_uncertain or robot._client is not guard.robot_receiver._robot_client
                or not receiver.ready or receiver.client is not runtime._client
                or not guard.robot_receiver._ready.is_set() or guard.robot_receiver._failed.is_set()
                or guard.robot_receiver._stopping.is_set() or not guard.robot_receiver._client.is_connected()
                or not _live_endpoint(guard.activation_receiver.endpoint) or endpoint is None
                or not _live_endpoint(endpoint.endpoint)):
            raise ValueError('live virtual execution connections changed')
        key = guard.receipt_inbox._keys.get(robot.robot_id)
        if not isinstance(key, bytes) or not hmac.compare_digest(key, guard.robot_receiver._key):
            raise ValueError('live virtual session key changed')
        driver = robot._reroute_driver
        if (not isinstance(driver, tuple) or len(driver) != 3 or driver[0] is not endpoint.execution
                or driver[2] is not guard or not isinstance(driver[1], bytes)):
            raise ValueError('the exact live virtual generation driver is required')
        refs = (runtime, gate, robot, robot.executor, robot._client, receiver, receiver.client,
                guard.robot_receiver, guard.robot_receiver._client, guard.activation_receiver,
                guard.activation_receiver.endpoint._client, endpoint, endpoint.endpoint._client, driver)
        return refs, key

    def issue(self, db, task_id, robot, event_id, *, final):
        """Observe a fresh stop before the same transaction applies its event."""
        if type(final) is not bool:
            raise ValueError('the virtual stop type must be explicit')
        self._task = task_id
        self._check_fences(db, robot)
        previous = self._lease
        if previous is not None and previous.task == task_id:
            # Continuity cannot be reset by a still-valid original challenge.
            # Every later stop must chain from a committed, unexpired lease.
            self.authorize(db, task_id, robot, finished=previous.final)
        from server.sim_reroute_execute_wire import _execution
        from server.sim_reroute_multi_progress import validate_steps
        _payload, data = _execution(db, self.guard, task_id, robot, state='uncertain')
        refs, key = self._binding(task_id, robot)
        if robot._reroute_driver[1] != _payload:
            raise ValueError('live virtual driver payload changed')
        if data['released_until'] == data['nodes'][-1]:
            raise ValueError('fresh stop proofs apply only to multiple-segment generations')
        steps = validate_steps(db, self.guard.runtime.progress._map, data)
        seq = data['parent_seq'] + 1 + len(steps)
        state = robot.executor.snapshot(); order, receipt = state.get('order') or {}, state.get('receipt') or {}
        if (state.get('status') != 'stopped' or receipt.get('state') != 'completed'
                or receipt.get('order_id') != task_id or receipt.get('seq') != seq or order.get('seq') != seq
                or order.get('nodes') != data['nodes'] or order.get('released_until') != state.get('current_node')):
            raise ValueError('the current virtual generation is not stopped')
        index = state.get('visit_index')
        if (type(index) is not int or not 1 <= index < len(data['nodes'])
                or state.get('released_index') != index or data['nodes'][index] != state['current_node']
                or final is not (index == len(data['nodes']) - 1)):
            raise ValueError('the virtual visit index differs')
        stop, incoming = robot.executor.field_stop_direction()
        if stop != data['nodes'][index] or incoming != data['edge_ids'][index - 1]:
            raise ValueError('the virtual stopped direction differs')
        row = db.execute('SELECT payload,digest,task_id,robot_session,seq,state FROM sim_event_inbox '
                         'WHERE robot_id=? AND event_id=?', (robot.robot_id, event_id)).fetchone()
        if row is None or row[2:] != (task_id, data['robot_session'], seq, 'pending'):
            raise ValueError('fresh stopped event is unavailable')
        event, encoded, digest = _event(row[0].encode('ascii'))
        held = set(db.execute('SELECT kind,resource_id FROM allocation_occupancy WHERE task_id=?', (task_id,)))
        expected_clear = held - {('node', stop)} if final else set()
        if (encoded != row[0] or digest != row[1] or event['event_id'] != event_id or event['order_id'] != task_id
                or event['robot_session'] != data['robot_session'] or event['seq'] != seq
                or event['map_id'] != data['map_id'] or event['stopped_at'] != stop
                or _resources(event['cleared_resources']) != expected_clear):
            raise ValueError('fresh stopped event does not match')
        if (robot.robot_id, event_id) not in self.guard.runtime.receiver._occupied_events:
            raise ValueError('fresh stopped event was not classified')
        next_payload = None
        if not final:
            segment = plan_segment(self.guard.runtime.progress._map, data['nodes'][index:])
            next_payload = _canonical({'order_id': task_id, 'seq': seq + 1, 'map_id': data['map_id'],
                'nodes': data['nodes'], 'released_until': segment.released_until,
                'action_at_end': data['action_at_end']})
        gate = self.guard.runtime.gate
        with gate._state_lock:
            if task_id in gate._blocked:
                raise ValueError('virtual generation is blocked')
            generation = gate._generations.get(task_id, 0)
        now = self._now()
        challenge = db.execute('SELECT * FROM recovery_challenges WHERE task_id=?', (task_id,)).fetchone()
        if challenge is None:
            raise ValueError('original virtual proof is unavailable')
        challenge_digest = hashlib.sha256(_canonical(list(challenge))).hexdigest()
        prior = self._lease
        if prior is not None and prior.task == task_id and seq <= prior.seq:
            raise ValueError('virtual stop proof cannot be replayed or extended')
        ttl = min(gate._ttl, 30.0)
        self._lease = _Lease(task_id, data['transition_id'], data['robot_session'], robot.robot_id,
            generation, gate._epoch, data['parent_seq'], event_id, digest, encoded, stop, index,
            receipt['digest'], seq, _fingerprint(steps), next_payload, final, now, now + ttl, refs, bytes(key),
            challenge_digest)
        return {'state': 'issued', 'expires': now + ttl}

    def authorize(self, db, task_id, robot, *, finished):
        """Recheck one committed stop against its live session; return original proposal."""
        if type(finished) is not bool:
            raise ValueError('the virtual finish state must be explicit')
        self._task = task_id
        self._check_fences(db, robot)
        lease = self._lease
        if lease is None or lease.task != task_id or lease.robot_id != robot.robot_id or lease.final != finished:
            raise ValueError('matching live virtual stop proof is unavailable')
        refs, key = self._binding(task_id, robot)
        if any(left is not right for left, right in zip(refs, lease.refs)) or not hmac.compare_digest(key, lease.key):
            raise ValueError('virtual connection or session identity changed')
        gate, runtime = self.guard.runtime.gate, self.guard.runtime
        now = self._now()
        if now < lease.created or now >= lease.expires or gate._epoch != lease.gate_epoch:
            raise ValueError('live virtual stop proof expired')
        with gate._state_lock:
            if task_id in gate._blocked or gate._generations.get(task_id, 0) != lease.generation:
                raise ValueError('virtual recovery generation changed')
        gate._active(db)
        _verify_field_global(db, field_required=True)
        row = db.execute('SELECT generation,transition_id,robot_id,robot_session,parent_seq,old_payload,stopped_at,'
            'incoming_edge,nodes_json,edges_json,blocked_json,recovery_epoch,state FROM sim_reroute_generations WHERE task_id=?',
            (task_id,)).fetchone()
        if row is None:
            raise ValueError('virtual generation is missing')
        generation, trans, rid, session, seq, payload, stopped, incoming, nodes, edges, blocked, epoch, state = row
        if (generation != 1 or trans != lease.transition or rid != lease.robot_id or session != lease.session
                or seq != lease.parent_seq or epoch != lease.generation or state != 'accepted'
                or runtime._tasks.get(task_id) != (rid, session)):
            raise ValueError('virtual generation identity changed')
        old, _, digest = _decode(payload.encode('utf-8'), rid, runtime.progress._map_id)
        challenge = db.execute('SELECT epoch,robot_session,seq,stopped_at,expires,status,robot_id,map_id,'
            'order_id,order_digest,map_fingerprint FROM recovery_challenges WHERE task_id=?', (task_id,)).fetchone()
        complete_challenge = db.execute('SELECT * FROM recovery_challenges WHERE task_id=?', (task_id,)).fetchone()
        if (challenge is None or challenge[:4] != (gate._epoch, session, seq, stopped)
                or challenge[5:] != ('verified', rid, runtime.progress._map_id,
                    task_id, digest, runtime.progress._fingerprint) or complete_challenge is None
                or hashlib.sha256(_canonical(list(complete_challenge))).hexdigest() != lease.challenge_digest):
            raise ValueError('expired original virtual proof identity differs')
        owner, path, previous_stop = runtime.progress._job(db, task_id, rid)
        latest_seq, latest_payload, previous_state, previous = runtime.progress._latest(db, task_id, rid, path, previous_stop)
        if (owner != rid or latest_seq != seq or previous_stop != stopped or latest_payload != payload
                or previous_state not in ('uncertain', 'acked') or (finished and previous_state != 'acked')
                or previous != old):
            raise ValueError('original virtual order changed')
        nodes, edges, blocked = json.loads(nodes), json.loads(edges), json.loads(blocked)
        data = {'transition_id': trans, 'generation': generation, 'robot_id': rid, 'robot_session': session,
                'task_id': task_id, 'parent_seq': seq, 'parent_digest': digest, 'map_id': runtime.progress._map_id,
                'stopped_at': stopped, 'incoming_edge': incoming, 'nodes': nodes, 'edge_ids': edges,
                'blocked_resources': blocked, 'released_until': plan_segment(runtime.progress._map, nodes).released_until,
                'action_at_end': old['action_at_end']}
        from server.sim_reroute_multi_progress import validate_steps
        steps = validate_steps(db, runtime.progress._map, data)
        inbox = robot.executor._inbox
        with inbox._lock, closing(sqlite3.connect(inbox.path)) as robot_db:
            saved = robot_db.execute('SELECT digest,state FROM sim_inbox_orders WHERE order_id=? AND seq=?',
                                     (task_id, lease.seq)).fetchone()
        if saved != (lease.receipt_digest, 'completed'):
            raise ValueError('fresh stopped virtual order history changed')
        event_row = db.execute('SELECT payload,digest,state,result FROM sim_event_inbox WHERE robot_id=? AND event_id=?',
                               (rid, lease.event_id)).fetchone()
        if event_row is None or event_row[0:3] != (lease.event_payload, lease.event_digest, 'applied'):
            raise ValueError('fresh stopped event was not committed')
        if finished:
            expected_result = _canonical({'status': 'reroute_reached_goal', 'task_id': task_id}).decode('ascii')
            finish = db.execute('SELECT transition_id,robot_id,event_id,event_digest FROM sim_reroute_finishes WHERE task_id=?',
                                (task_id,)).fetchone()
            if (event_row[3] != expected_result or finish != (trans, rid, lease.event_id, lease.event_digest)
                    or _fingerprint(steps) != lease.prior_steps):
                raise ValueError('fresh final stop is inconsistent')
        else:
            if (len(steps) != len(lease.prior_steps) + 1 or _fingerprint(steps[:-1]) != lease.prior_steps
                    or steps[-1]['seq'] != lease.seq or steps[-1]['stop_index'] != lease.stop_index
                    or steps[-1]['event_id'] != lease.event_id or steps[-1]['next_payload'] != lease.next_payload):
                raise ValueError('fresh intermediate stop is inconsistent')
        return _canonical({'type': 'sim.reroute.prepare.v1', 'data': data})
