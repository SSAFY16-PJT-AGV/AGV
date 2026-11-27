"""Durable order deduplication for an isolated simulator; never drives hardware.

Acceptance is not execution. After restart an accepted order is never returned
as new; explicit simulated reconciliation requires a caller-provided stopped
node and rear-clear confirmation. These are not physical evidence or an
authenticated robot protocol.
"""
from collections import namedtuple
from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
import re
import sqlite3
import threading

from server.order_publisher import _topic, validate_order


class OrderRejected(ValueError):
    """The simulator cannot safely accept or confirm the requested order."""


Receipt = namedtuple("Receipt", ["new", "order_id", "seq", "digest", "state"])
_MAX_BYTES = 256 * 1024
_MAX_SEQ = 9223372036854775807


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("invalid JSON constant")


def _finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("invalid JSON number")
    return value


def _decode(payload, robot_id, map_id):
    if not isinstance(payload, bytes) or len(payload) > _MAX_BYTES:
        raise OrderRejected("order payload must be bounded UTF-8 bytes")
    try:
        order = json.loads(payload.decode("utf-8"), object_pairs_hook=_object_pairs,
                           parse_constant=_constant, parse_float=_finite)
        validate_order(robot_id, order)
        if order["map_id"] != map_id:
            raise ValueError("order map does not match approved map")
        if type(order["seq"]) is not int or not 1 <= order["seq"] <= _MAX_SEQ:
            raise ValueError("order sequence must start at one")
        if len(order["nodes"]) < 2 or any(a == b for a, b in zip(order["nodes"], order["nodes"][1:])):
            raise ValueError("route must contain at least one edge and no adjacent duplicates")
        if order["released_until"] not in order["nodes"][1:]:
            raise ValueError("released segment needs an occurrence after the start")
        canonical = json.dumps(order, sort_keys=True, ensure_ascii=True,
                               allow_nan=False, separators=(",", ":"))
        return order, canonical, hashlib.sha256(canonical.encode("ascii")).hexdigest()
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise OrderRejected("invalid simulated order") from exc


def _stored_order(payload, digest, robot_id, map_id, order_id, seq):
    if not isinstance(payload, str) or not isinstance(digest, str):
        raise OrderRejected("stored order is invalid")
    try:
        order, canonical, computed = _decode(payload.encode("ascii"), robot_id, map_id)
    except UnicodeEncodeError as exc:
        raise OrderRejected("stored order encoding is invalid") from exc
    if canonical != payload or computed != digest or order["order_id"] != order_id or order["seq"] != seq:
        raise OrderRejected("stored order content does not match its identity")
    return order


def _verified_reroute(db, robot_id, map_id, previous, parent_digest, order, order_digest, *, require_motion):
    """Verify one recorded simulated generation change without granting motion."""
    try:
        from sim.reroute_acceptance import SimRerouteAcceptance
        from sim.reroute_activation import SimRerouteActivation
        from sim.reroute_execution import SimRerouteExecution
        SimRerouteAcceptance._verify_receipts(db)
        SimRerouteActivation._verify_activations(db)
        SimRerouteExecution._verify_executions(db)
        row = db.execute('SELECT transition_id,digest,payload,state FROM sim_reroute_executions '
                         'WHERE order_id=? AND parent_seq=?', (order['order_id'], previous['seq'])).fetchone()
        if row is None or row[3] != 'recorded_not_executed':
            raise ValueError('recorded generation execution request required')
        message = json.loads(row[2], object_pairs_hook=_object_pairs, parse_constant=_constant, parse_float=_finite)
        data = message['data']
        expected = {'order_id': data['task_id'], 'seq': data['parent_seq'] + 1, 'map_id': data['map_id'],
                    'nodes': data['nodes'], 'released_until': data['released_until'],
                    'action_at_end': data['action_at_end']}
        canonical = json.dumps(expected, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        actual = json.dumps(order, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        if (row[0] != data['transition_id'] or data['generation'] != 1 or data['robot_id'] != robot_id
                or data['map_id'] != map_id or data['task_id'] != previous['order_id']
                or data['parent_seq'] != previous['seq'] or data['parent_digest'] != parent_digest
                or data['stopped_at'] != previous['released_until'] or data['nodes'][0] != data['stopped_at']
                or data['nodes'][-1] != previous['nodes'][-1] or canonical != actual
                or json.dumps(data['action_at_end'], sort_keys=True, ensure_ascii=True, separators=(',', ':'))
                   != json.dumps(previous['action_at_end'], sort_keys=True, ensure_ascii=True, separators=(',', ':'))):
            raise ValueError('generation does not match its parent and route')
        if require_motion:
            motion = db.execute('SELECT transition_id,order_digest,payload,state FROM sim_reroute_motion_starts '
                                'WHERE order_id=? AND parent_seq=?', (order['order_id'], previous['seq'])).fetchone()
            if motion != (row[0], order_digest, actual, 'started'):
                raise ValueError('recorded generation motion differs')
        return row[0], tuple(data['edge_ids']), data['incoming_edge']
    except (ValueError, TypeError, KeyError, IndexError, RecursionError, UnicodeError, sqlite3.Error) as exc:
        raise OrderRejected('simulated generation history is inconsistent') from exc


def _validated_history(db, robot_id, map_id, order_id):
    """Validate every saved command and derive its released visit index.

    Stored data is not physical evidence and this does not authorize moving
    after a reboot.  A missing prefix or non-completed prior order fails shut.
    """
    rows = db.execute("SELECT seq,payload,digest,state FROM sim_inbox_orders "
                      "WHERE order_id=? ORDER BY seq", (order_id,)).fetchall()
    if not rows:
        raise OrderRejected("stored order history is missing")
    indices, reference = [], None
    previous_index = 0
    previous_order = previous_digest = None
    changed = False
    for expected, (seq, payload, digest, state) in enumerate(rows, 1):
        if type(seq) is not int or seq != expected or state not in ("accepted", "completed"):
            raise OrderRejected("stored order sequence or state is inconsistent")
        if expected < len(rows) and state != "completed":
            raise OrderRejected("unfinished prior segment in stored history")
        order = _stored_order(payload, digest, robot_id, map_id, order_id, seq)
        action = json.dumps(order["action_at_end"], sort_keys=True, ensure_ascii=True,
                            allow_nan=False, separators=(",", ":"))
        identity = (order["order_id"], order["map_id"], order["nodes"], action)
        if reference is None:
            reference = identity
        elif identity != reference:
            if changed or previous_order is None:
                raise OrderRejected("stored route or action changed")
            _verified_reroute(db, robot_id, map_id, previous_order, previous_digest, order, digest,
                              require_motion=True)
            reference, previous_index, changed = identity, 0, True
        index = next((i for i in range(previous_index + 1, len(order["nodes"]))
                      if order["nodes"][i] == order["released_until"]), None)
        if index is None:
            raise OrderRejected("stored release has no later occurrence")
        indices.append(index)
        previous_index = index
        previous_order, previous_digest = order, digest
    return rows, indices


class SimOrderInbox:
    """Persistent inbox bound to one simulated robot and approved map."""

    def __init__(self, path, robot_id, map_id):
        path = os.fspath(path)
        if not isinstance(path, str) or not path or path == ":memory:":
            raise ValueError("a file-backed SQLite database is required")
        try:
            _topic(robot_id)
        except ValueError as exc:
            raise OrderRejected("a valid simulated robot ID is required") from exc
        if not isinstance(map_id, str) or re.fullmatch(r"[^@\s]+@v[0-9]+", map_id) is None:
            raise OrderRejected("an approved versioned map ID is required")
        try:
            map_id.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise OrderRejected("map ID must be valid Unicode") from exc
        self.path, self.robot_id, self.map_id = path, robot_id, map_id
        self._owned = set()
        self._lock = threading.RLock()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5, isolation_level=None)

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def initialize(self):
        with self._lock:
            # Each explicit initialization starts a fresh simulated boot.
            self._owned.clear()
            with self._write() as db:
                db.execute("CREATE TABLE IF NOT EXISTS sim_inbox_identity "
                           "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                           "robot_id TEXT NOT NULL, map_id TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS sim_inbox_orders "
                           "(order_id TEXT NOT NULL, seq INTEGER NOT NULL, digest TEXT NOT NULL, "
                           "payload TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(order_id,seq))")
                existing = db.execute("SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1").fetchone()
                if existing is None:
                    if db.execute("SELECT 1 FROM sim_inbox_orders LIMIT 1").fetchone() is not None:
                        raise OrderRejected("inbox identity missing for existing orders")
                    db.execute("INSERT INTO sim_inbox_identity VALUES (1,?,?)", (self.robot_id, self.map_id))
                elif existing != (self.robot_id, self.map_id):
                    raise OrderRejected("inbox belongs to a different simulated robot or map")

    def accept(self, payload):
        order, canonical, digest = _decode(payload, self.robot_id, self.map_id)
        order_id, seq = order["order_id"], order["seq"]
        with self._lock:
            with self._write() as db:
                identity = db.execute("SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1").fetchone()
                if identity != (self.robot_id, self.map_id):
                    raise OrderRejected("inbox identity is not initialized or mismatched")
                existing = db.execute("SELECT digest,payload,state FROM sim_inbox_orders WHERE order_id=? AND seq=?",
                                      (order_id, seq)).fetchone()
                if existing is not None:
                    _validated_history(db, self.robot_id, self.map_id, order_id)
                    if existing[0] != digest or existing[1] != canonical:
                        raise OrderRejected("order ID and sequence were reused with different content")
                    if existing[2] not in ("accepted", "completed"):
                        raise OrderRejected("stored order state is invalid")
                    return Receipt(False, order_id, seq, digest, existing[2])
                latest = db.execute("SELECT order_id,seq,payload,state,digest FROM sim_inbox_orders ORDER BY rowid DESC LIMIT 1").fetchone()
                if latest is None:
                    if seq != 1:
                        raise OrderRejected("first sequence must be one")
                else:
                    if latest[3] != "completed":
                        raise OrderRejected("previous order has not been confirmed complete")
                    previous = _stored_order(latest[2], latest[4], self.robot_id, self.map_id, latest[0], latest[1])
                    history, indices = _validated_history(db, self.robot_id, self.map_id, latest[0])
                    if history[-1][0] != latest[1]:
                        raise OrderRejected("latest order and history disagree")
                    if latest[0] == order_id:
                        if seq != latest[1] + 1:
                            raise OrderRejected("sequence must advance by one")
                        if any(order[key] != previous[key] for key in ("order_id", "map_id", "nodes")) or (
                            json.dumps(order["action_at_end"], sort_keys=True, ensure_ascii=True, separators=(",", ":")) !=
                            json.dumps(previous["action_at_end"], sort_keys=True, ensure_ascii=True, separators=(",", ":"))
                        ):
                            raise OrderRejected("the active route or end action changed")
                        if order["released_until"] not in order["nodes"][indices[-1] + 1:]:
                            raise OrderRejected("released segment must advance")
                    else:
                        if (db.execute("SELECT 1 FROM sim_inbox_orders WHERE order_id=?", (order_id,)).fetchone()
                                or seq != 1 or indices[-1] != len(previous["nodes"]) - 1):
                            raise OrderRejected("cannot replace an unfinished route or reuse an old order ID")
                db.execute("INSERT INTO sim_inbox_orders VALUES (?,?,?,?,?)",
                           (order_id, seq, digest, canonical, "accepted"))
            self._owned.add((order_id, seq, digest))
            return Receipt(True, order_id, seq, digest, "accepted")

    def _accept_reroute(self, payload, transition_id):
        """Persist a verified generation start; caller must hold server guard.

        The normal accept() path still rejects changing the active route. This
        private entry point is only used by SimRerouteExecution while its
        generation-aware server guard holds the union reservation.
        """
        order, canonical, digest = _decode(payload, self.robot_id, self.map_id)
        if not isinstance(transition_id, str) or re.fullmatch('[0-9a-f]{32}', transition_id) is None:
            raise OrderRejected('invalid simulated generation transition')
        task, seq = order['order_id'], order['seq']
        with self._lock:
            with self._write() as db:
                if db.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone() != (
                        self.robot_id, self.map_id):
                    raise OrderRejected('simulated inbox identity changed')
                db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_motion_starts ('
                           'transition_id TEXT PRIMARY KEY NOT NULL, order_id TEXT NOT NULL, '
                           'parent_seq INTEGER NOT NULL, order_digest TEXT NOT NULL, payload TEXT NOT NULL, '
                           'state TEXT NOT NULL, UNIQUE(order_id,parent_seq))')
                existing = db.execute('SELECT digest,payload,state FROM sim_inbox_orders WHERE order_id=? AND seq=?',
                                      (task, seq)).fetchone()
                if existing is not None:
                    _validated_history(db, self.robot_id, self.map_id, task)
                    if existing != (digest, canonical, 'accepted'):
                        raise OrderRejected('generation order is not the original pending command')
                    return Receipt(False, task, seq, digest, 'accepted')
                latest = db.execute('SELECT order_id,seq,payload,digest,state FROM sim_inbox_orders '
                                    'ORDER BY rowid DESC LIMIT 1').fetchone()
                if (latest is None or latest[0] != task or latest[1] != seq - 1 or latest[4] != 'completed'):
                    raise OrderRejected('generation must follow the current completed parent')
                history, _indices = _validated_history(db, self.robot_id, self.map_id, task)
                if not history or history[-1][0] != latest[1]:
                    raise OrderRejected('generation parent history changed')
                previous = _stored_order(latest[2], latest[3], self.robot_id, self.map_id, task, seq - 1)
                actual_transition, _edges, _incoming = _verified_reroute(
                    db, self.robot_id, self.map_id, previous, latest[3], order, digest, require_motion=False)
                if actual_transition != transition_id:
                    raise OrderRejected('generation transition differs')
                try:
                    db.execute('INSERT INTO sim_reroute_motion_starts VALUES (?,?,?,?,?,?)',
                               (transition_id, task, seq - 1, digest, canonical, 'started'))
                    db.execute('INSERT INTO sim_inbox_orders VALUES (?,?,?,?,?)',
                               (task, seq, digest, canonical, 'accepted'))
                except sqlite3.IntegrityError as exc:
                    raise OrderRejected('generation start conflicts with existing history') from exc
                _validated_history(db, self.robot_id, self.map_id, task)
            self._owned.add((task, seq, digest))
            return Receipt(True, task, seq, digest, 'accepted')

    def _verify_reroute_motion(self, order, receipt, transition_id):
        with self._lock, self._write() as db:
            if (receipt.state == 'accepted'
                    and (order['order_id'], order['seq'], receipt.digest) not in self._owned):
                raise OrderRejected('this boot no longer owns the generation order')
            rows, _indices = _validated_history(db, self.robot_id, self.map_id, order['order_id'])
            canonical = json.dumps(order, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
            if (not rows or rows[-1][0] != order['seq'] or rows[-1][1] != canonical
                    or rows[-1][2] != receipt.digest or rows[-1][3] != receipt.state):
                raise OrderRejected('current generation order changed')
            data = db.execute('SELECT parent_seq,order_digest,payload,state FROM sim_reroute_motion_starts '
                              'WHERE transition_id=? AND order_id=?', (transition_id, order['order_id'])).fetchone()
            if data is None or type(data[0]) is not int or data[0] < 1 or data[3] != 'started':
                raise OrderRejected('current generation motion evidence changed')
            first = db.execute('SELECT digest,payload FROM sim_inbox_orders WHERE order_id=? AND seq=?',
                               (order['order_id'], data[0] + 1)).fetchone()
            if first != (data[1], data[2]):
                raise OrderRejected('current generation motion evidence changed')

    def _complete(self, order_id, seq, digest, stopped_at, rear_clear_confirmed, *, recovery):
        if (not isinstance(order_id, str) or type(seq) is not int or seq < 1
                or not isinstance(digest, str) or not isinstance(stopped_at, str)
                or rear_clear_confirmed is not True):
            raise OrderRejected("confirmed simulated stop and rear clearance are required")
        with self._lock:
            if not recovery and (order_id, seq, digest) not in self._owned:
                raise OrderRejected("this instance did not accept the order as new")
            with self._write() as db:
                identity = db.execute("SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1").fetchone()
                if identity != (self.robot_id, self.map_id):
                    raise OrderRejected("inbox identity is not initialized or mismatched")
                row = db.execute("SELECT digest,payload,state FROM sim_inbox_orders WHERE order_id=? AND seq=?",
                                 (order_id, seq)).fetchone()
                if row is None or row[0] != digest or row[2] not in ("accepted", "completed"):
                    raise OrderRejected("order identity or state is inconsistent")
                order = _stored_order(row[1], row[0], self.robot_id, self.map_id, order_id, seq)
                _validated_history(db, self.robot_id, self.map_id, order_id)
                if order["released_until"] != stopped_at:
                    raise OrderRejected("confirmed node differs from released destination")
                if recovery and row[2] == "accepted" and len(set(order["nodes"])) != len(order["nodes"]):
                    raise OrderRejected("repeated route needs visit-index proof after reboot")
                db.execute("UPDATE sim_inbox_orders SET state='completed' WHERE order_id=? AND seq=?",
                           (order_id, seq))
                return Receipt(False, order_id, seq, digest, "completed")

    def mark_completed(self, order_id, seq, digest, stopped_at, *, rear_clear_confirmed=False):
        return self._complete(order_id, seq, digest, stopped_at, rear_clear_confirmed, recovery=False)

    def reconcile_completed(self, order_id, seq, digest, stopped_at, *, rear_clear_confirmed=False):
        """Explicitly reconcile a simulated stopped robot after restart; never executes an order."""
        return self._complete(order_id, seq, digest, stopped_at, rear_clear_confirmed, recovery=True)
