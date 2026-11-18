"""Experimental isolated visit-index journal; not a motion or clearance proof.

This is a separate file-backed SQLite database. It does not publish, claim or
release resources, inspect live robots, integrate with the production workflow,
or authorize automatic recovery. A session ID and fingerprint are only caller
supplied labels, not authenticated evidence. Keep existing safety gates closed.
"""
from contextlib import closing, contextmanager
import hashlib
import json
import os
import re
import sqlite3

from server.field_robot_contract import validate_field_robot_order
from server.route_occurrence import visit_indices_from_orders


_TABLES = {
    'occurrence_meta': 'CREATE TABLE occurrence_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)',
    'occurrence_jobs': ('CREATE TABLE occurrence_jobs ('
                        'task_id TEXT PRIMARY KEY NOT NULL, robot_id TEXT NOT NULL, session_id TEXT NOT NULL, '
                        'map_id TEXT NOT NULL, fingerprint TEXT NOT NULL, last_seq INTEGER NOT NULL, '
                        'last_index INTEGER NOT NULL)'),
    'occurrence_orders': ('CREATE TABLE occurrence_orders ('
                          'task_id TEXT NOT NULL REFERENCES occurrence_jobs(task_id), seq INTEGER NOT NULL, '
                          'payload TEXT NOT NULL, digest TEXT NOT NULL, visit_index INTEGER NOT NULL, '
                          'PRIMARY KEY(task_id,seq))'),
}


class JournalConflict(ValueError):
    """Journal state is missing, stale, inconsistent, or cannot be reconciled."""


def _id(value, label):
    if not isinstance(value, str) or not value or value != value.strip() or '\x00' in value:
        raise ValueError(label + ' must be a nonempty string')
    try:
        if len(value.encode('utf-8')) > 128:
            raise ValueError(label + ' exceeds 128 bytes')
    except UnicodeError as exc:
        raise ValueError(label + ' must be UTF-8') from exc
    return value


def _fingerprint(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('fingerprint must be a lowercase SHA-256 value')
    return value


def _encode(order):
    validate_field_robot_order(order)
    payload = json.dumps(order, sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(',', ':'))
    return payload, hashlib.sha256(payload.encode('ascii')).hexdigest()


def _decode(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def constant(_value):
        raise ValueError('nonfinite value')
    if not isinstance(payload, str) or len(payload) > 256 * 1024:
        raise ValueError('invalid stored payload')
    return json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)


class OccurrenceJournal:
    """An explicitly separate simulation journal with transactional updates."""

    def __init__(self, path):
        value = os.fspath(path)
        if not isinstance(value, str) or not value or value == ':memory:':
            raise ValueError('file-backed journal path required')
        self.path = value

    @contextmanager
    def _transaction(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            db.execute('PRAGMA foreign_keys = ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    @staticmethod
    def _schema(db):
        objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        expected = {('table', name, sql) for name, sql in _TABLES.items()}
        if set(objects) != expected or len(objects) != len(expected):
            raise JournalConflict('journal database schema differs')
        rows = db.execute('SELECT key,value FROM occurrence_meta').fetchall()
        if rows != [('schema_version', '1')]:
            raise JournalConflict('unknown journal schema version')

    def initialize(self):
        with self._transaction(write=True) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if names:
                self._schema(db)
                return
            other = db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone()
            if other:
                raise JournalConflict('database is not an empty journal')
            for sql in _TABLES.values():
                db.execute(sql)
            db.execute("INSERT INTO occurrence_meta VALUES ('schema_version','1')")

    @staticmethod
    def _read(db, task_id, session, fingerprint, robot_id):
        OccurrenceJournal._schema(db)
        row = db.execute('SELECT robot_id,session_id,map_id,fingerprint,last_seq,last_index '
                         'FROM occurrence_jobs WHERE task_id=?', (task_id,)).fetchone()
        if row is None or row[0] != robot_id or row[1] != session or row[3] != fingerprint:
            raise JournalConflict('journal identity or fingerprint mismatch')
        try:
            robot, saved_session, map_id, saved_fp, last_seq, last_index = row
            _id(task_id, 'task_id'); _id(robot, 'robot_id'); _id(saved_session, 'session_id')
            _id(map_id, 'map_id'); _fingerprint(saved_fp)
            if not robot.startswith('sim-') or not robot[4:].strip():
                raise ValueError('only simulation identities are accepted')
            rows = db.execute('SELECT seq,payload,digest,visit_index FROM occurrence_orders '
                              'WHERE task_id=? ORDER BY seq', (task_id,)).fetchall()
            if not rows:
                raise ValueError('empty history')
            orders, stored_indices = [], []
            for seq, payload, digest, index in rows:
                decoded = _decode(payload)
                encoded, expected_digest = _encode(decoded)
                if (type(seq) is not int or type(index) is not int or payload != encoded
                        or digest != expected_digest or decoded['seq'] != seq):
                    raise ValueError('stored order or index is inconsistent')
                orders.append(decoded)
                stored_indices.append(index)
            indices = visit_indices_from_orders(orders)
            if (orders[0]['order_id'] != task_id or orders[0]['map_id'] != map_id
                    or tuple(stored_indices) != indices or type(last_seq) is not int
                    or type(last_index) is not int or last_seq != len(orders)
                    or last_index != indices[-1]):
                raise ValueError('stored job and order history disagree')
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError) as exc:
            raise JournalConflict('journal history is inconsistent') from exc
        return {'task_id': task_id, 'robot_id': robot, 'session_id': saved_session,
                'map_id': map_id, 'fingerprint': saved_fp, 'last_seq': last_seq,
                'last_index': last_index, 'indices': indices, 'orders': orders}

    def start(self, robot_id, session, fingerprint, order):
        _id(robot_id, 'robot_id'); _id(session, 'session_id'); _fingerprint(fingerprint)
        if not robot_id.startswith('sim-') or not robot_id[4:].strip():
            raise ValueError('only simulation identities are accepted')
        payload, digest = _encode(order)
        index = visit_indices_from_orders([order])[0]
        with self._transaction(write=True) as db:
            self._schema(db)
            if db.execute('SELECT 1 FROM occurrence_jobs WHERE task_id=?', (order['order_id'],)).fetchone():
                raise JournalConflict('task already exists')
            db.execute('INSERT INTO occurrence_jobs VALUES (?,?,?,?,?,?,?)',
                       (order['order_id'], robot_id, session, order['map_id'], fingerprint, 1, index))
            db.execute('INSERT INTO occurrence_orders VALUES (?,?,?,?,?)',
                       (order['order_id'], 1, payload, digest, index))
            return self._read(db, order['order_id'], session, fingerprint, robot_id)

    def read(self, task_id, session, fingerprint, *, robot_id):
        _id(task_id, 'task_id'); _id(robot_id, 'robot_id'); _id(session, 'session_id'); _fingerprint(fingerprint)
        with self._transaction() as db:
            return self._read(db, task_id, session, fingerprint, robot_id)

    def append(self, task_id, expected_seq, expected_index, session, fingerprint, order, *, robot_id):
        _id(task_id, 'task_id'); _id(robot_id, 'robot_id'); _id(session, 'session_id'); _fingerprint(fingerprint)
        if type(expected_seq) is not int or type(expected_index) is not int:
            raise ValueError('expected sequence and index must be integers')
        payload, digest = _encode(order)
        with self._transaction(write=True) as db:
            current = self._read(db, task_id, session, fingerprint, robot_id)
            if (current['last_seq'], current['last_index']) != (expected_seq, expected_index):
                raise JournalConflict('journal advanced or changed')
            try:
                index = visit_indices_from_orders(current['orders'] + [order])[-1]
            except (ValueError, TypeError, RecursionError) as exc:
                raise JournalConflict('new order does not extend journal history') from exc
            db.execute('INSERT INTO occurrence_orders VALUES (?,?,?,?,?)',
                       (task_id, order['seq'], payload, digest, index))
            changed = db.execute('UPDATE occurrence_jobs SET last_seq=?,last_index=? '
                                 'WHERE task_id=? AND last_seq=? AND last_index=?',
                                 (order['seq'], index, task_id, expected_seq, expected_index))
            if changed.rowcount != 1:
                raise JournalConflict('journal changed during update')
            return self._read(db, task_id, session, fingerprint, robot_id)
