"""Durably record a virtual reroute execution request without moving.

The caller must hold the server's execution guard around record(). This is an
inert, idempotent robot-side journal boundary: recording a request does not
authenticate its sender, authorize execution, move a robot, or release space.
"""
import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager

from server.field_map_planner import plan_field_route, validate_field_route
from server.segment_progress import _resources
from server.segments import plan_segment
from sim.order_inbox import OrderRejected, _object_pairs, _constant, _finite, _validated_history
from sim.reroute_acceptance import _FIELDS, _MAX_BYTES, _canonical
from sim.reroute_activation import SimRerouteActivation


class SimRerouteExecution:
    """Record an execution request only after the matching activation record."""
    def __init__(self, activation):
        if type(activation) is not SimRerouteActivation:
            raise ValueError('a bound simulated activation journal is required')
        self.activation = activation
        self.acceptance = activation.acceptance
        self.executor = activation.executor
        self.inbox = activation.inbox
        self.robot_session = activation.robot_session

    def initialize(self):
        try:
            with self.inbox._lock, self.inbox._write() as db:
                if db.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone() != (
                        self.inbox.robot_id, self.inbox.map_id):
                    raise OrderRejected('simulated inbox identity does not match')
                self.acceptance._verify_receipts(db)
                self.activation._verify_activations(db)
                db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_executions ('
                           'transition_id TEXT PRIMARY KEY NOT NULL, order_id TEXT NOT NULL, '
                           'parent_seq INTEGER NOT NULL, activation_digest TEXT NOT NULL, '
                           'digest TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, '
                           'UNIQUE(order_id,parent_seq))')
                self._verify_executions(db)
        except sqlite3.Error as exc:
            raise OrderRejected('simulated execution journal is unavailable') from exc

    @staticmethod
    def _verify_executions(db):
        rows = db.execute('SELECT transition_id,order_id,parent_seq,activation_digest,digest,payload,state '
                          'FROM sim_reroute_executions LIMIT 1025').fetchall()
        if len(rows) > 1024:
            raise OrderRejected('too many saved reroute execution requests')
        fields = _FIELDS | {'proposal_digest', 'activation_digest'}
        for transition, task, seq, activation_digest, digest, payload, state in rows:
            try:
                value = json.loads(payload, object_pairs_hook=_object_pairs, parse_constant=_constant,
                                   parse_float=_finite)
                if (not isinstance(value, dict) or set(value) != {'type', 'data'}
                        or value['type'] != 'sim.reroute.execute.v1' or not isinstance(value['data'], dict)
                        or set(value['data']) != fields or _canonical(value) != payload
                        or hashlib.sha256(payload.encode('ascii')).hexdigest() != digest
                        or value['data']['transition_id'] != transition or value['data']['task_id'] != task
                        or value['data']['parent_seq'] != seq or value['data']['activation_digest'] != activation_digest
                        or state != 'recorded_not_executed'):
                    raise ValueError('execution metadata changed')
                activation = {'type': 'sim.reroute.activate.v1',
                              'data': {key: value['data'][key] for key in _FIELDS | {'proposal_digest'}}}
                encoded = _canonical(activation)
                if hashlib.sha256(encoded.encode('ascii')).hexdigest() != activation_digest:
                    raise ValueError('execution activation digest differs')
                prior = db.execute('SELECT order_id,parent_seq,proposal_digest,digest,payload,state '
                                   'FROM sim_reroute_activations WHERE transition_id=?', (transition,)).fetchone()
                if prior != (task, seq, value['data']['proposal_digest'], activation_digest, encoded,
                             'recorded_not_executed'):
                    raise ValueError('execution lacks its staged activation')
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
                raise OrderRejected('saved simulated execution is inconsistent') from exc

    def record(self, payload):
        """Record a matching execution request; the executor stays stopped."""
        if not isinstance(payload, bytes) or len(payload) > _MAX_BYTES:
            raise OrderRejected('bounded simulated execution bytes required')
        try:
            message = json.loads(payload.decode('utf-8'), object_pairs_hook=_object_pairs,
                                 parse_constant=_constant, parse_float=_finite)
            fields = _FIELDS | {'proposal_digest', 'activation_digest'}
            if (not isinstance(message, dict) or set(message) != {'type', 'data'}
                    or message['type'] != 'sim.reroute.execute.v1' or not isinstance(message['data'], dict)
                    or set(message['data']) != fields):
                raise ValueError('invalid simulated execution request')
            data = message['data']
            if (not isinstance(data['transition_id'], str)
                    or re.fullmatch('[0-9a-f]{32}', data['transition_id']) is None
                    or type(data['generation']) is not int or data['generation'] != 1
                    or data['robot_id'] != self.inbox.robot_id or data['robot_session'] != self.robot_session
                    or data['map_id'] != self.inbox.map_id or type(data['parent_seq']) is not int
                    or data['parent_seq'] < 1 or not isinstance(data['parent_digest'], str)
                    or re.fullmatch('[0-9a-f]{64}', data['parent_digest']) is None
                    or any(not isinstance(data[key], str) or re.fullmatch('[0-9a-f]{64}', data[key]) is None
                           for key in ('proposal_digest', 'activation_digest'))
                    or not isinstance(data['nodes'], list) or not 2 <= len(data['nodes']) <= 256
                    or any(not isinstance(item, str) or not item for item in data['nodes'])
                    or len(set(data['nodes'])) != len(data['nodes'])):
                raise ValueError('invalid simulated execution identity')
            activation = _canonical({'type': 'sim.reroute.activate.v1',
                                     'data': {key: data[key] for key in _FIELDS | {'proposal_digest'}}})
            if hashlib.sha256(activation.encode('ascii')).hexdigest() != data['activation_digest']:
                raise ValueError('execution request differs from staged activation')
            encoded = _canonical(message)
            digest = hashlib.sha256(encoded.encode('ascii')).hexdigest()
            blocked = _resources(data['blocked_resources'])
            with self.executor._lock:
                stopped, incoming = self.executor.field_stop_direction()
                current = self.executor.snapshot()
                old, receipt = current['order'], current['receipt']
                if (current['status'] != 'stopped' or not old or not receipt
                        or receipt['state'] != 'completed' or receipt['order_id'] != data['task_id']
                        or receipt['seq'] != data['parent_seq'] or receipt['digest'] != data['parent_digest']
                        or stopped != data['stopped_at'] or incoming != data['incoming_edge']
                        or data['nodes'][0] != stopped or data['nodes'][-1] != old['nodes'][-1]
                        or _canonical(data['action_at_end']) != _canonical(old['action_at_end'])):
                    raise ValueError('execution request does not match the current stopped parent')
                route = plan_field_route(self.executor._field_map, map_ref=self.inbox.map_id,
                    incoming_edge=incoming, goal=old['nodes'][-1], blocked_resources=blocked)
                witness = validate_field_route(self.executor._field_map, map_ref=self.inbox.map_id,
                    incoming_edge=incoming, nodes=data['nodes'], edge_ids=data['edge_ids'])
                if (list(route.nodes) != data['nodes'] or list(route.edge_ids) != data['edge_ids']
                        or list(witness.edge_ids) != data['edge_ids']
                        or plan_segment(self.executor._field_map, data['nodes']).released_until != data['released_until']):
                    raise ValueError('execution path does not match the checked plan')
                with self.inbox._lock, self.inbox._write() as db:
                    if db.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone() != (
                            self.inbox.robot_id, self.inbox.map_id):
                        raise ValueError('simulated inbox identity changed')
                    self.acceptance._verify_receipts(db)
                    self.activation._verify_activations(db)
                    self._verify_executions(db)
                    prior = db.execute('SELECT order_id,parent_seq,proposal_digest,digest,payload,state '
                        'FROM sim_reroute_activations WHERE transition_id=?', (data['transition_id'],)).fetchone()
                    if prior != (data['task_id'], data['parent_seq'], data['proposal_digest'],
                                 data['activation_digest'], activation, 'recorded_not_executed'):
                        raise ValueError('matching durable activation receipt required')
                    history, _indices = _validated_history(db, self.inbox.robot_id, self.inbox.map_id, data['task_id'])
                    latest = db.execute('SELECT order_id,seq,digest,state FROM sim_inbox_orders '
                                        'ORDER BY rowid DESC LIMIT 1').fetchone()
                    if (not history or history[-1][0] != data['parent_seq']
                            or latest != (data['task_id'], data['parent_seq'], data['parent_digest'], 'completed')):
                        raise ValueError('durable parent order changed')
                    existing = db.execute('SELECT transition_id,activation_digest,digest,payload,state '
                        'FROM sim_reroute_executions WHERE transition_id=? OR (order_id=? AND parent_seq=?)',
                        (data['transition_id'], data['task_id'], data['parent_seq'])).fetchall()
                    expected = (data['transition_id'], data['activation_digest'], digest, encoded, 'recorded_not_executed')
                    if existing:
                        if existing != [expected]:
                            raise ValueError('simulated execution conflicts with an earlier request')
                        new = False
                    else:
                        db.execute('INSERT INTO sim_reroute_executions VALUES (?,?,?,?,?,?,?)',
                            (data['transition_id'], data['task_id'], data['parent_seq'], data['activation_digest'],
                             digest, encoded, 'recorded_not_executed'))
                        new = True
            return {'new': new, 'transition_id': data['transition_id'], 'generation': data['generation'],
                    'task_id': data['task_id'], 'parent_seq': data['parent_seq'], 'digest': digest,
                    'activation_digest': data['activation_digest'], 'state': 'recorded_not_executed'}
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError, sqlite3.Error) as exc:
            if isinstance(exc, OrderRejected):
                raise
            raise OrderRejected('invalid or stale simulated execution request') from exc

    @contextmanager
    def _authority(self, payload, guard, *, next_order=None):
        """Hold the real simulation server's guard for exactly one operation."""
        try:
            from server.sim_reroute_execute_wire import SimRerouteExecutionGuard
        except ImportError as exc:
            raise OrderRejected('generation-aware server guard is unavailable') from exc
        method_name = 'guard' if next_order is None else 'guard_next'
        if (type(guard) is not SimRerouteExecutionGuard
                or getattr(getattr(guard, method_name, None), '__func__', None)
                   is not getattr(SimRerouteExecutionGuard, method_name, None)
                or getattr(getattr(getattr(guard, 'robot_receiver', None), 'robot', None), 'executor', None)
                   is not self.executor):
            raise OrderRejected('bound generation-aware server guard is required')
        authority = guard.guard(payload) if next_order is None else guard.guard_next(payload, next_order)
        with authority:
            with self.executor._lock:
                if self.executor._reroute_guard_thread is not None:
                    raise OrderRejected('generation guard cannot be reentered')
                self.executor._reroute_guard_thread = threading.get_ident()
                try:
                    yield
                finally:
                    self.executor._reroute_guard_thread = None

    def _matching_record(self, payload):
        """Read the exact already-recorded request without making new claims."""
        if not isinstance(payload, bytes) or len(payload) > _MAX_BYTES:
            raise OrderRejected('bounded simulated execution bytes required')
        try:
            message = json.loads(payload.decode('utf-8'), object_pairs_hook=_object_pairs,
                                 parse_constant=_constant, parse_float=_finite)
            if (not isinstance(message, dict) or set(message) != {'type', 'data'}
                    or message['type'] != 'sim.reroute.execute.v1' or not isinstance(message['data'], dict)
                    or set(message['data']) != _FIELDS | {'proposal_digest', 'activation_digest'}):
                raise ValueError('invalid execution request')
            encoded = _canonical(message)
            digest = hashlib.sha256(encoded.encode('ascii')).hexdigest()
            data = message['data']
            with self.inbox._lock, self.inbox._write() as db:
                self.acceptance._verify_receipts(db)
                self.activation._verify_activations(db)
                self._verify_executions(db)
                row = db.execute('SELECT order_id,parent_seq,activation_digest,digest,payload,state '
                                 'FROM sim_reroute_executions WHERE transition_id=?',
                                 (data['transition_id'],)).fetchone()
                if row != (data['task_id'], data['parent_seq'], data['activation_digest'], digest, encoded,
                           'recorded_not_executed'):
                    raise ValueError('exact recorded execution request required')
            return data
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError, sqlite3.Error) as exc:
            if isinstance(exc, OrderRejected):
                raise
            raise OrderRejected('invalid or missing simulated execution request') from exc

    def start(self, payload, *, guard, bind_robot=None):
        """Start the exact recorded virtual generation under the server guard."""
        with self._authority(payload, guard):
            if bind_robot is not None:
                if (getattr(guard.robot_receiver, 'robot', None) is not bind_robot
                        or bind_robot.executor is not self.executor or not bind_robot.ready):
                    raise OrderRejected('simulated robot cannot bind this generation')
                existing = bind_robot._reroute_driver
                if existing is None:
                    if self.executor._reroute_transition is not None:
                        raise OrderRejected('previous generation has no live driver')
                    if not bind_robot._queue.empty():
                        raise OrderRejected('simulated robot has pending orders')
                elif (existing[0] is not self or existing[1] != payload or existing[2] is not guard):
                    raise OrderRejected('another generation driver is already bound')
            data = self._matching_record(payload)
            if self.executor._reroute_transition is not None:
                current = self.executor.snapshot()
                if (self.executor._reroute_transition != data['transition_id'] or not current.get('receipt')
                        or current['receipt']['order_id'] != data['task_id']
                        or current['receipt']['seq'] < data['parent_seq'] + 1):
                    raise OrderRejected('another generation is already installed')
                self.executor._verify_field_execution()
                return {'new': False, 'state': current['status'], 'transition_id': data['transition_id']}
            order = {'order_id': data['task_id'], 'seq': data['parent_seq'] + 1, 'map_id': data['map_id'],
                     'nodes': data['nodes'], 'released_until': data['released_until'],
                     'action_at_end': data['action_at_end']}
            payload_order = json.dumps(order, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            receipt = self.executor._begin_reroute(payload_order, transition_id=data['transition_id'],
                incoming_edge=data['incoming_edge'], edge_ids=data['edge_ids'])
            if not receipt.new:
                raise OrderRejected('a previously recorded generation cannot be resumed automatically')
            if bind_robot is not None:
                bind_robot._reroute_driver = (self, bytes(payload), guard)
            return {'new': True, 'state': 'moving', 'transition_id': data['transition_id']}

    def tick(self, payload, dt_seconds, *, guard, expected_driver=None):
        """Advance the virtual pose only while the server guard is held."""
        with self._authority(payload, guard):
            if expected_driver is not None:
                robot = guard.robot_receiver.robot
                if robot._reroute_driver is not expected_driver or not robot.ready:
                    raise OrderRejected('simulated generation driver or connection changed')
            data = self._matching_record(payload)
            if (self.executor._reroute_transition != data['transition_id']
                    or self.executor._status not in ('moving', 'stopped')):
                raise OrderRejected('the requested generation is not installed')
            return self.executor.tick(dt_seconds)

    def accept_next(self, payload, order_payload, *, guard):
        """Accept a later released segment only under its own server fence."""
        if not isinstance(order_payload, bytes):
            raise OrderRejected('next simulated order must be bytes')
        with self._authority(payload, guard, next_order=order_payload):
            data = self._matching_record(payload)
            if self.executor._reroute_transition != data['transition_id']:
                raise OrderRejected('the requested generation is not installed')
            return self.executor.accept(order_payload)
