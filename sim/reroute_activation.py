"""Durably stage a virtual reroute activation without allowing motion.

The caller must hold the server's SimRerouteActivationGuard around record().
This class verifies only the robot's local boot/session, parent stop, durable
proposal, and requested route. It never authenticates the sender, opens MQTT,
changes the current order, permits movement, or releases any reservation.
"""
import hashlib
import json
import re
import sqlite3

from server.field_map_planner import plan_field_route, validate_field_route
from server.segment_progress import _resources
from server.segments import plan_segment
from sim.order_inbox import OrderRejected, _object_pairs, _constant, _finite, _validated_history
from sim.reroute_acceptance import SimRerouteAcceptance, _FIELDS, _MAX_BYTES, _canonical


class SimRerouteActivation:
    """Record a single server-guarded activation proposal for a stopped robot."""
    def __init__(self, acceptance):
        if type(acceptance) is not SimRerouteAcceptance:
            raise ValueError('an explicitly bound simulated reroute receiver is required')
        self.acceptance = acceptance
        self.executor = acceptance.executor
        self.inbox = acceptance.inbox
        self.robot_session = acceptance.robot_session

    def initialize(self):
        try:
            with self.inbox._lock, self.inbox._write() as db:
                if db.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone() != (
                        self.inbox.robot_id, self.inbox.map_id):
                    raise OrderRejected('simulated inbox identity does not match')
                self.acceptance._verify_receipts(db)
                db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_activations ('
                           'transition_id TEXT PRIMARY KEY NOT NULL, order_id TEXT NOT NULL, '
                           'parent_seq INTEGER NOT NULL, proposal_digest TEXT NOT NULL, '
                           'digest TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, '
                           'UNIQUE(order_id,parent_seq))')
                self._verify_activations(db)
        except sqlite3.Error as exc:
            raise OrderRejected('simulated activation journal is unavailable') from exc

    @staticmethod
    def _verify_activations(db):
        rows = db.execute('SELECT transition_id,order_id,parent_seq,proposal_digest,digest,payload,state '
                          'FROM sim_reroute_activations LIMIT 1025').fetchall()
        if len(rows) > 1024:
            raise OrderRejected('too many saved reroute activations')
        for transition, task, seq, proposal_digest, digest, payload, state in rows:
            try:
                value = json.loads(payload, object_pairs_hook=_object_pairs, parse_constant=_constant,
                                   parse_float=_finite)
                if (not isinstance(value, dict) or set(value) != {'type', 'data'}
                        or value['type'] != 'sim.reroute.activate.v1' or not isinstance(value['data'], dict)
                        or set(value['data']) != _FIELDS | {'proposal_digest'} or _canonical(value) != payload
                        or hashlib.sha256(payload.encode('ascii')).hexdigest() != digest
                        or value['data']['transition_id'] != transition or value['data']['task_id'] != task
                        or value['data']['parent_seq'] != seq or value['data']['proposal_digest'] != proposal_digest
                        or state != 'recorded_not_executed'):
                    raise ValueError('activation metadata changed')
                proposal = {'type': 'sim.reroute.prepare.v1',
                            'data': {key: value['data'][key] for key in _FIELDS}}
                proposed = _canonical(proposal)
                if hashlib.sha256(proposed.encode('ascii')).hexdigest() != proposal_digest:
                    raise ValueError('activation proposal digest differs')
                previous = db.execute('SELECT order_id,parent_seq,digest,payload,state FROM sim_reroute_receipts '
                                      'WHERE transition_id=?', (transition,)).fetchone()
                if previous != (task, seq, proposal_digest, proposed, 'accepted_not_executed'):
                    raise ValueError('activation lacks its accepted proposal')
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
                raise OrderRejected('saved simulated activation is inconsistent') from exc

    def record(self, payload):
        """Persist a matching staged activation; the executor stays stopped."""
        if not isinstance(payload, bytes) or len(payload) > _MAX_BYTES:
            raise OrderRejected('bounded simulated activation bytes required')
        try:
            message = json.loads(payload.decode('utf-8'), object_pairs_hook=_object_pairs,
                                 parse_constant=_constant, parse_float=_finite)
            if (not isinstance(message, dict) or set(message) != {'type', 'data'}
                    or message['type'] != 'sim.reroute.activate.v1' or not isinstance(message['data'], dict)
                    or set(message['data']) != _FIELDS | {'proposal_digest'}):
                raise ValueError('invalid simulated activation message')
            data = message['data']
            if (not isinstance(data['transition_id'], str)
                    or re.fullmatch('[0-9a-f]{32}', data['transition_id']) is None
                    or type(data['generation']) is not int or data['generation'] != 1
                    or data['robot_id'] != self.inbox.robot_id or data['robot_session'] != self.robot_session
                    or data['map_id'] != self.inbox.map_id or type(data['parent_seq']) is not int
                    or data['parent_seq'] < 1 or not isinstance(data['parent_digest'], str)
                    or re.fullmatch('[0-9a-f]{64}', data['parent_digest']) is None
                    or not isinstance(data['proposal_digest'], str)
                    or re.fullmatch('[0-9a-f]{64}', data['proposal_digest']) is None
                    or not isinstance(data['nodes'], list) or not 2 <= len(data['nodes']) <= 256
                    or any(not isinstance(item, str) or not item for item in data['nodes'])
                    or len(set(data['nodes'])) != len(data['nodes'])):
                raise ValueError('invalid simulated activation identity')
            proposed = _canonical({'type': 'sim.reroute.prepare.v1',
                                   'data': {key: data[key] for key in _FIELDS}})
            if hashlib.sha256(proposed.encode('ascii')).hexdigest() != data['proposal_digest']:
                raise ValueError('activation does not match its proposal digest')
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
                    raise ValueError('activation does not match the current stopped parent')
                route = plan_field_route(self.executor._field_map, map_ref=self.inbox.map_id,
                    incoming_edge=incoming, goal=old['nodes'][-1], blocked_resources=blocked)
                witness = validate_field_route(self.executor._field_map, map_ref=self.inbox.map_id,
                    incoming_edge=incoming, nodes=data['nodes'], edge_ids=data['edge_ids'])
                if (list(route.nodes) != data['nodes'] or list(route.edge_ids) != data['edge_ids']
                        or list(witness.edge_ids) != data['edge_ids']
                        or plan_segment(self.executor._field_map, data['nodes']).released_until != data['released_until']):
                    raise ValueError('activation path does not match the checked plan')
                with self.inbox._lock, self.inbox._write() as db:
                    if db.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone() != (
                            self.inbox.robot_id, self.inbox.map_id):
                        raise ValueError('simulated inbox identity changed')
                    self.acceptance._verify_receipts(db)
                    self._verify_activations(db)
                    previous = db.execute('SELECT order_id,parent_seq,digest,payload,state FROM sim_reroute_receipts '
                        'WHERE transition_id=?', (data['transition_id'],)).fetchone()
                    if previous != (data['task_id'], data['parent_seq'], data['proposal_digest'],
                                    proposed, 'accepted_not_executed'):
                        raise ValueError('matching durable proposal receipt required')
                    history, _indices = _validated_history(db, self.inbox.robot_id, self.inbox.map_id, data['task_id'])
                    latest = db.execute('SELECT order_id,seq,digest,state FROM sim_inbox_orders '
                                        'ORDER BY rowid DESC LIMIT 1').fetchone()
                    if (not history or history[-1][0] != data['parent_seq']
                            or latest != (data['task_id'], data['parent_seq'], data['parent_digest'], 'completed')):
                        raise ValueError('durable parent order changed')
                    existing = db.execute('SELECT transition_id,proposal_digest,digest,payload,state '
                        'FROM sim_reroute_activations WHERE transition_id=? OR (order_id=? AND parent_seq=?)',
                        (data['transition_id'], data['task_id'], data['parent_seq'])).fetchall()
                    expected = (data['transition_id'], data['proposal_digest'], digest, encoded, 'recorded_not_executed')
                    if existing:
                        if existing != [expected]:
                            raise ValueError('simulated activation conflicts with an earlier request')
                        new = False
                    else:
                        db.execute('INSERT INTO sim_reroute_activations VALUES (?,?,?,?,?,?,?)',
                            (data['transition_id'], data['task_id'], data['parent_seq'], data['proposal_digest'],
                             digest, encoded, 'recorded_not_executed'))
                        new = True
            return {'new': new, 'transition_id': data['transition_id'], 'generation': data['generation'],
                    'task_id': data['task_id'], 'parent_seq': data['parent_seq'], 'digest': digest,
                    'proposal_digest': data['proposal_digest'], 'state': 'recorded_not_executed'}
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError, sqlite3.Error) as exc:
            if isinstance(exc, OrderRejected):
                raise
            raise OrderRejected('invalid or stale simulated activation') from exc
