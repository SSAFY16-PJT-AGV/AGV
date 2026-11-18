"""Isolated simulation journal for a proposed command/response contract.

No MQTT, physical identity, delivery, motion, reservation or clearance is
provided here. A stored response is only an untrusted simulated claim.
"""
from contextlib import closing, contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3

from server.field_robot_contract import validate_field_robot_order
from server.order_publisher import _payload, _topic


class JournalConflict(ValueError):
    """A proposed record is inconsistent, stale or no longer mutable."""


_APPLICATION_ID = 0x4147564A
_SCHEMA = {
    'journal_meta': 'CREATE TABLE journal_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)',
    'commands': ('CREATE TABLE commands (robot_id TEXT NOT NULL, order_id TEXT NOT NULL, '
                 'seq INTEGER NOT NULL, session_id TEXT NOT NULL, map_id TEXT NOT NULL, '
                 'payload BLOB NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, '
                 'generation TEXT NOT NULL, PRIMARY KEY(robot_id,order_id,seq), '
                 'UNIQUE(order_id,seq))'),
    'responses': ('CREATE TABLE responses (robot_id TEXT NOT NULL, event_id TEXT NOT NULL, '
                  'order_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, '
                  'PRIMARY KEY(robot_id,event_id), UNIQUE(robot_id,order_id,seq), '
                  'FOREIGN KEY(robot_id,order_id,seq) REFERENCES commands(robot_id,order_id,seq))'),
}
_RESPONSE_KEYS = {'event_id', 'robot_session', 'order_id', 'seq', 'map_id', 'order_digest', 'outcome'}


def _text(value, label):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid ' + label)
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeEncodeError as exc:
        raise ValueError('invalid ' + label) from exc
    return value


def _sequence(value):
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ValueError('invalid sequence')
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _decode(payload):
    value = json.loads(payload, object_pairs_hook=_unique_pairs,
                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError('invalid number')))
    if not isinstance(value, dict):
        raise ValueError('object required')
    return value


class ProposedCommandJournal:
    """A fenced, file-backed journal for isolated simulation inputs only."""

    def __init__(self, path, *, simulation=False, _create=False, _expected_creation_id=None):
        if simulation is not True:
            raise ValueError('explicit isolated simulation required')
        if not _create and (not isinstance(_expected_creation_id, str)
                            or re.fullmatch('[0-9a-f]{64}', _expected_creation_id) is None):
            raise ValueError('independently stored journal creation identity required')
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or raw == ':memory:':
            raise ValueError('file-backed journal path required')
        if os.path.islink(raw):
            raise ValueError('regular journal file required')
        self.path = os.path.realpath(raw)
        if _create:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        elif not os.path.isfile(self.path):
            raise ValueError('existing regular journal file required')
        info = os.stat(self.path)
        self._file_identity = (info.st_dev, info.st_ino)
        self._generation = secrets.token_hex(32)
        self._creation_id = None if _create else _expected_creation_id
        if _create:
            with self._transaction() as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise JournalConflict('new journal is not empty')
                db.execute('PRAGMA application_id={}'.format(_APPLICATION_ID))
                db.execute('PRAGMA user_version=1')
                for sql in _SCHEMA.values():
                    db.execute(sql)
                self._creation_id = secrets.token_hex(32)
                db.executemany('INSERT INTO journal_meta VALUES (?,?)',
                               [('creation_id', self._creation_id), ('generation', self._generation)])
        else:
            try:
                with self._transaction() as db:
                    self._validate_all(db, require_owner=False)
                    db.execute("UPDATE commands SET state='blocked' WHERE state='pending'")
                    db.execute("UPDATE journal_meta SET value=? WHERE key='generation'", (self._generation,))
                    self._validate_all(db)
            except (sqlite3.Error, ValueError) as exc:
                raise ValueError('journal cannot be safely opened') from exc

    @classmethod
    def create(cls, path, *, simulation=False):
        return cls(path, simulation=simulation, _create=True)

    @classmethod
    def open_existing(cls, path, *, simulation=False, expected_creation_id=None):
        return cls(path, simulation=simulation, _create=False, _expected_creation_id=expected_creation_id)

    @property
    def creation_id(self):
        """Identity to preserve independently from this experimental DB."""
        return self._creation_id

    def _check_file(self):
        if os.path.islink(self.path) or not os.path.isfile(self.path):
            raise JournalConflict('journal file is missing or replaced')
        info = os.stat(self.path)
        if (info.st_dev, info.st_ino) != self._file_identity:
            raise JournalConflict('journal file was replaced')

    def _connect(self):
        try:
            self._check_file()
            db = sqlite3.connect(Path(self.path).as_uri() + '?mode=rw', uri=True,
                                 timeout=5, isolation_level=None)
            try:
                self._check_file()
                db.execute('PRAGMA foreign_keys=ON')
                return db
            except BaseException:
                db.close()
                raise
        except (sqlite3.Error, OSError) as exc:
            raise JournalConflict('journal file unavailable') from exc

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _validate_all(self, db, *, require_owner=True):
        try:
            if (db.execute('PRAGMA application_id').fetchone() != (_APPLICATION_ID,)
                    or db.execute('PRAGMA user_version').fetchone() != (1,)
                    or db.execute('PRAGMA quick_check').fetchall() != [('ok',)]
                    or db.execute('PRAGMA foreign_key_check').fetchone() is not None):
                raise ValueError('database integrity differs')
            objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if set(objects) != {('table', name, sql) for name, sql in _SCHEMA.items()}:
                raise ValueError('database schema differs')
            meta = dict(db.execute('SELECT key,value FROM journal_meta'))
            if (set(meta) != {'creation_id', 'generation'}
                    or any(re.fullmatch('[0-9a-f]{64}', meta[key]) is None for key in meta)
                    or (self._creation_id is not None and meta['creation_id'] != self._creation_id)
                    or (require_owner and meta['generation'] != self._generation)):
                raise ValueError('journal identity or owner differs')
            if db.execute('SELECT robot_id FROM commands GROUP BY robot_id '
                          'HAVING COUNT(DISTINCT order_id) > 1 LIMIT 1').fetchone():
                raise ValueError('robot is associated with multiple task IDs')
            previous = {}
            for robot, order_id, seq, session, map_id, payload, digest, state, generation in db.execute(
                    'SELECT robot_id,order_id,seq,session_id,map_id,payload,digest,state,generation '
                    'FROM commands ORDER BY robot_id,order_id,seq'):
                _topic(robot)
                _text(session, 'session')
                _sequence(seq)
                if not isinstance(payload, bytes):
                    raise ValueError('invalid payload bytes')
                order = _decode(payload)
                validate_field_robot_order(order)
                if (_payload(order) != payload or hashlib.sha256(payload).hexdigest() != digest
                        or order['order_id'] != order_id or order['seq'] != seq or order['map_id'] != map_id
                        or state not in ('pending', 'uncertain', 'blocked', 'accepted', 'rejected')
                        or re.fullmatch('[0-9a-f]{64}', generation) is None):
                    raise ValueError('stored command differs')
                key = (robot, order_id)
                prev = previous.get(key)
                if seq != (1 if prev is None else prev[0] + 1):
                    raise ValueError('stored command sequence gap')
                if prev is not None and (prev[1] != session or prev[2] != map_id or prev[3] != 'accepted'
                                         or prev[4]['nodes'] != order['nodes']
                                         or prev[4]['action_at_end'] != order['action_at_end']):
                    raise ValueError('stored order sequence differs')
                previous[key] = (seq, session, map_id, state, order)
                response = db.execute('SELECT event_id,payload FROM responses WHERE robot_id=? AND order_id=? AND seq=?',
                                      (robot, order_id, seq)).fetchone()
                if state in ('accepted', 'rejected'):
                    if response is None:
                        raise ValueError('stored response is missing')
                    event = _decode(response[1])
                    if (set(event) != _RESPONSE_KEYS or event['order_id'] != order_id or event['seq'] != seq
                            or event['robot_session'] != session or event['map_id'] != map_id
                            or event['order_digest'] != digest or event['outcome'] != state
                            or event['event_id'] != response[0] or type(event['seq']) is not int
                            or _canonical(event) != response[1]):
                        raise ValueError('stored response differs')
                    _text(event['event_id'], 'event id')
                elif response is not None:
                    raise ValueError('unexpected response record')
            return meta
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise JournalConflict('journal failed integrity or owner checks') from exc

    @staticmethod
    def _check_identity(robot_id, order_id, seq):
        _topic(robot_id)
        _text(order_id, 'order id')
        _sequence(seq)

    def get(self, robot_id, order_id, seq):
        self._check_identity(robot_id, order_id, seq)
        with self._transaction() as db:
            self._validate_all(db)
            row = db.execute('SELECT session_id,map_id,payload,digest,state FROM commands '
                             'WHERE robot_id=? AND order_id=? AND seq=?',
                             (robot_id, order_id, seq)).fetchone()
            if row is None:
                return None
            return {'robot_id': robot_id, 'order_id': order_id, 'seq': seq, 'session_id': row[0],
                    'map_id': row[1], 'payload': row[2], 'digest': row[3], 'state': row[4]}

    def stage(self, robot_id, session_id, order):
        _topic(robot_id)
        _text(session_id, 'session')
        snapshot = copy.deepcopy(order)
        validate_field_robot_order(snapshot)
        _text(snapshot['order_id'], 'order id')
        _sequence(snapshot['seq'])
        payload = _payload(snapshot)
        digest = hashlib.sha256(payload).hexdigest()
        identity = (robot_id, snapshot['order_id'], snapshot['seq'])
        with self._transaction() as db:
            self._validate_all(db)
            row = db.execute('SELECT session_id,map_id,payload FROM commands '
                             'WHERE robot_id=? AND order_id=? AND seq=?', identity).fetchone()
            if row is not None:
                if row != (session_id, snapshot['map_id'], payload):
                    raise JournalConflict('command identity already has different content')
            else:
                if db.execute('SELECT 1 FROM commands WHERE robot_id=? AND order_id<>? LIMIT 1',
                              (robot_id, snapshot['order_id'])).fetchone():
                    raise JournalConflict('robot already has another task in this journal')
                # Work from a previous generation never permits another send.
                if db.execute('SELECT 1 FROM commands WHERE generation != ? LIMIT 1',
                              (self._generation,)).fetchone():
                    raise JournalConflict('prior generation requires explicit recovery')
                prev = db.execute('SELECT seq,session_id,map_id,state,payload FROM commands '
                                  'WHERE robot_id=? AND order_id=? ORDER BY seq DESC LIMIT 1', identity[:2]).fetchone()
                if prev is None:
                    if snapshot['seq'] != 1:
                        raise JournalConflict('first command must use sequence 1')
                else:
                    old = _decode(prev[4])
                    if (snapshot['seq'] != prev[0] + 1 or prev[1] != session_id or prev[2] != snapshot['map_id']
                            or prev[3] != 'accepted' or old['nodes'] != snapshot['nodes']
                            or old['action_at_end'] != snapshot['action_at_end']):
                        raise JournalConflict('next command differs from accepted history')
                try:
                    db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?)',
                               identity + (session_id, snapshot['map_id'], payload, digest, 'pending', self._generation))
                except sqlite3.IntegrityError as exc:
                    raise JournalConflict('command identity already exists') from exc
            self._validate_all(db)
        return self.get(*identity)

    def mark_attempted(self, robot_id, order_id, seq, *, expected_payload):
        """Persist the attempt only for these exact immutable wire bytes.

        Returns the stored bytes; this method does not publish or authorize.
        """
        self._check_identity(robot_id, order_id, seq)
        if type(expected_payload) is not bytes:
            raise ValueError('exact immutable expected payload bytes are required')
        with self._transaction() as db:
            self._validate_all(db)
            updated = db.execute("UPDATE commands SET state='uncertain' WHERE robot_id=? AND order_id=? "
                                 "AND seq=? AND state='pending' AND generation=? AND payload=?",
                                 (robot_id, order_id, seq, self._generation, expected_payload))
            if updated.rowcount != 1:
                raise JournalConflict('command, payload or generation is not eligible for its first attempt')
            self._validate_all(db)
            (payload,) = db.execute('SELECT payload FROM commands WHERE robot_id=? AND order_id=? AND seq=?',
                                    (robot_id, order_id, seq)).fetchone()
            return payload

    def record_response(self, robot_id, response, *, retained=False):
        _topic(robot_id)
        if retained is not False:
            raise JournalConflict('retained or ambiguous response is not accepted')
        event = copy.deepcopy(response)
        if not isinstance(event, dict) or set(event) != _RESPONSE_KEYS:
            raise ValueError('invalid proposed response')
        _text(event['event_id'], 'event id')
        _text(event['robot_session'], 'session')
        self._check_identity(robot_id, event['order_id'], event['seq'])
        if event['outcome'] not in ('accepted', 'rejected'):
            raise ValueError('invalid proposed outcome')
        payload = _canonical(event)
        with self._transaction() as db:
            self._validate_all(db)
            prior = db.execute('SELECT payload FROM responses WHERE robot_id=? AND event_id=?',
                               (robot_id, event['event_id'])).fetchone()
            if prior is not None:
                if prior[0] != payload:
                    raise JournalConflict('event ID is bound to different content')
                return event['outcome']
            row = db.execute('SELECT session_id,map_id,digest,state,generation FROM commands '
                             'WHERE robot_id=? AND order_id=? AND seq=?',
                             (robot_id, event['order_id'], event['seq'])).fetchone()
            if (row is None or row != (event['robot_session'], event['map_id'], event['order_digest'],
                                       'uncertain', self._generation)):
                raise JournalConflict('response differs from current attempted command')
            try:
                db.execute('INSERT INTO responses VALUES (?,?,?,?,?)',
                           (robot_id, event['event_id'], event['order_id'], event['seq'], payload))
                db.execute('UPDATE commands SET state=? WHERE robot_id=? AND order_id=? AND seq=?',
                           (event['outcome'], robot_id, event['order_id'], event['seq']))
            except sqlite3.IntegrityError as exc:
                raise JournalConflict('response conflicts with recorded history') from exc
            self._validate_all(db)
        return event['outcome']
