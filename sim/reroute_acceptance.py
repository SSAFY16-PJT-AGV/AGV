"""Durably check a virtual reroute proposal without authorizing movement.

This is the receiving-side message contract, not an MQTT subscriber or proof
that the sender controls the server. A future MQTT integration must verify the
prepared server reservation and bind the actual connection/session before it
can use a receipt. Acceptance here does not change or execute an order.
"""
import hashlib
import json
import re
import sqlite3

from server.field_map_planner import plan_field_route
from server.segment_progress import _resources
from server.segments import plan_segment
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import OrderRejected, _object_pairs, _constant, _finite, _validated_history


_FIELDS = frozenset(('transition_id', 'generation', 'robot_id', 'robot_session', 'task_id',
                     'parent_seq', 'parent_digest', 'map_id', 'stopped_at', 'incoming_edge',
                     'nodes', 'edge_ids', 'blocked_resources', 'released_until', 'action_at_end'))
_MAX_BYTES = 256 * 1024


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


class SimRerouteAcceptance:
    """Validate a proposed generation against this virtual executor's stop."""
    def __init__(self, executor, *, robot_session):
        if (type(executor) is not SimOrderExecutor or executor._field_map is None
                or not isinstance(robot_session, str) or not robot_session
                or len(robot_session.encode('utf-8')) > 256):
            raise ValueError('a field simulator and explicit boot session are required')
        self.executor = executor
        self.inbox = executor._inbox
        self.robot_session = robot_session

    def initialize(self):
        with self.inbox._lock, self.inbox._write() as db:
            if db.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone() != (
                    self.inbox.robot_id, self.inbox.map_id):
                raise OrderRejected('simulated inbox identity does not match')
            db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_receipts ('
                       'transition_id TEXT PRIMARY KEY NOT NULL, order_id TEXT NOT NULL, '
                       'parent_seq INTEGER NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, '
                       'state TEXT NOT NULL, UNIQUE(order_id,parent_seq))')
            self._verify_receipts(db)

    @staticmethod
    def _verify_receipts(db):
        rows = db.execute('SELECT transition_id,order_id,parent_seq,digest,payload,state '
                          'FROM sim_reroute_receipts LIMIT 1025').fetchall()
        if len(rows) > 1024:
            raise OrderRejected('too many saved reroute receipts')
        for transition_id, task, seq, digest, payload, state in rows:
            try:
                value = json.loads(payload, object_pairs_hook=_object_pairs, parse_constant=_constant,
                                   parse_float=_finite)
                if (not isinstance(value, dict) or set(value) != {'type', 'data'}
                        or value['type'] != 'sim.reroute.prepare.v1' or not isinstance(value['data'], dict)
                        or set(value['data']) != _FIELDS or _canonical(value) != payload
                        or hashlib.sha256(payload.encode('ascii')).hexdigest() != digest
                        or value['data']['transition_id'] != transition_id
                        or value['data']['task_id'] != task or value['data']['parent_seq'] != seq
                        or state != 'accepted_not_executed'):
                    raise ValueError('invalid receipt')
            except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
                raise OrderRejected('saved reroute receipt is inconsistent') from exc

    def accept(self, payload):
        if not isinstance(payload, bytes) or len(payload) > _MAX_BYTES:
            raise OrderRejected('bounded simulated reroute bytes required')
        try:
            message = json.loads(payload.decode('utf-8'), object_pairs_hook=_object_pairs,
                                 parse_constant=_constant, parse_float=_finite)
            if (not isinstance(message, dict) or set(message) != {'type', 'data'}
                    or message['type'] != 'sim.reroute.prepare.v1'
                    or not isinstance(message['data'], dict) or set(message['data']) != _FIELDS):
                raise ValueError('invalid reroute message')
            data = message['data']
            if (not isinstance(data['transition_id'], str)
                    or re.fullmatch('[0-9a-f]{32}', data['transition_id']) is None
                    or type(data['generation']) is not int or data['generation'] != 1
                    or data['robot_id'] != self.inbox.robot_id or data['robot_session'] != self.robot_session
                    or data['map_id'] != self.inbox.map_id or type(data['parent_seq']) is not int
                    or data['parent_seq'] < 1 or not isinstance(data['parent_digest'], str)
                    or re.fullmatch('[0-9a-f]{64}', data['parent_digest']) is None
                    or not isinstance(data['nodes'], list) or not 2 <= len(data['nodes']) <= 256
                    or any(not isinstance(item, str) or not item for item in data['nodes'])
                    or len(set(data['nodes'])) != len(data['nodes'])):
                raise ValueError('invalid reroute identity')
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
                    raise ValueError('reroute does not match the stopped parent')
                route = plan_field_route(self.executor._field_map, map_ref=self.inbox.map_id,
                    incoming_edge=incoming, goal=old['nodes'][-1], blocked_resources=blocked)
                if (list(route.nodes) != data['nodes'] or list(route.edge_ids) != data['edge_ids']
                        or plan_segment(self.executor._field_map, data['nodes']).released_until != data['released_until']):
                    raise ValueError('reroute path does not match the checked plan')
                with self.inbox._lock, self.inbox._write() as db:
                    if db.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone() != (
                            self.inbox.robot_id, self.inbox.map_id):
                        raise ValueError('simulated inbox identity changed')
                    self._verify_receipts(db)
                    history, _indices = _validated_history(db, self.inbox.robot_id, self.inbox.map_id, data['task_id'])
                    latest = db.execute('SELECT order_id,seq,digest,state FROM sim_inbox_orders '
                                        'ORDER BY rowid DESC LIMIT 1').fetchone()
                    if (not history or history[-1][0] != data['parent_seq']
                            or latest != (data['task_id'], data['parent_seq'], data['parent_digest'], 'completed')):
                        raise ValueError('durable parent order changed')
                    existing = db.execute('SELECT transition_id,digest,payload,state FROM sim_reroute_receipts '
                        'WHERE transition_id=? OR (order_id=? AND parent_seq=?)',
                        (data['transition_id'], data['task_id'], data['parent_seq'])).fetchall()
                    if existing:
                        if existing != [(data['transition_id'], digest, encoded, 'accepted_not_executed')]:
                            raise ValueError('reroute transition conflicts with an accepted proposal')
                        new = False
                    else:
                        db.execute('INSERT INTO sim_reroute_receipts VALUES (?,?,?,?,?,?)',
                            (data['transition_id'], data['task_id'], data['parent_seq'], digest, encoded,
                             'accepted_not_executed'))
                        new = True
            return {'new': new, 'transition_id': data['transition_id'], 'generation': data['generation'],
                    'task_id': data['task_id'], 'parent_seq': data['parent_seq'], 'digest': digest,
                    'state': 'accepted_not_executed'}
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError, sqlite3.Error) as exc:
            if isinstance(exc, OrderRejected):
                raise
            raise OrderRejected('invalid or stale simulated reroute proposal') from exc
