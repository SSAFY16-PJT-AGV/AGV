# -*- coding: utf-8 -*-
"""Experimental authenticated, hold-only reconciliation (Python 3.6+)."""
import copy
import hashlib
import hmac
import json
import math
import re
import secrets
import time

_COMMON = frozenset(('v', 'kind', 'nonce', 'server_epoch', 'robot_id', 'robot_session',
                     'order_id', 'seq', 'order_digest', 'map_id', 'release_index',
                     'stopped_at', 'event_id', 'event_digest', 'mac'))
_EVENT = frozenset(('type', 'event_id', 'robot_session', 'order_id', 'seq',
                    'map_id', 'stopped_at', 'cleared_resources'))
_HEX = re.compile(r'^[0-9a-f]{64}$')
_PREFIX = b'AGV-RECONCILE-V1\0'
MAX_MESSAGE = 16384


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(',', ':'), allow_nan=False).encode('ascii')


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate key')
        out[key] = value
    return out


def decode_message(payload):
    if not isinstance(payload, bytes) or len(payload) > MAX_MESSAGE:
        raise ValueError('invalid message')
    def invalid(_value):
        raise ValueError('nonfinite number')
    value = json.loads(payload.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError('object required')
    return value


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip() and '\x00' not in value


def _hex(value):
    return isinstance(value, str) and _HEX.match(value) is not None


class ReconcileRobot(object):
    """Authenticate and compare snapshots. No operation here authorizes motion."""
    def __init__(self, robot_id, key, clock=time.monotonic, random_nonce=None, ttl=10.0, limit=1024):
        if not _text(robot_id) or not isinstance(key, bytes) or len(key) < 32:
            raise ValueError('robot ID and at least 32 bytes of key are required')
        if isinstance(ttl, bool) or not isinstance(ttl, (float, int)) or not math.isfinite(ttl) or not 0 < ttl <= 30:
            raise ValueError('invalid timeout')
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError('invalid nonce limit')
        self.robot_id, self._key, self._clock = robot_id, key, clock
        self._random = random_nonce or (lambda: secrets.token_hex(32))
        self._ttl, self._limit = float(ttl), limit
        self._seen = {}

    def _signed(self, value):
        out = copy.deepcopy(value)
        out['mac'] = hmac.new(self._key, _PREFIX + canonical(out), hashlib.sha256).hexdigest()
        return out

    def _check(self, value, kind, extras):
        try:
            if not isinstance(value, dict) or set(value) != _COMMON | set(extras):
                return False
            if type(value['v']) is not int or value['v'] != 1 or value['kind'] != kind:
                return False
            if any(not _hex(value[k]) for k in ('nonce', 'server_epoch', 'order_digest', 'mac')):
                return False
            if value['robot_id'] != self.robot_id or not all(_text(value[k]) for k in
                ('robot_session', 'order_id', 'map_id', 'stopped_at')):
                return False
            if type(value['seq']) is not int or value['seq'] < 1 or type(value['release_index']) is not int or value['release_index'] < 1:
                return False
            eid, digest = value['event_id'], value['event_digest']
            if not ((eid is None and digest is None and kind == 'probe') or (_text(eid) and _hex(digest))):
                return False
            body = {k: v for k, v in value.items() if k != 'mac'}
            expected = hmac.new(self._key, _PREFIX + canonical(body), hashlib.sha256).hexdigest()
            return hmac.compare_digest(value['mac'], expected)
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            return False

    def _local_event(self, query, local):
        if not isinstance(local, dict) or local.get('safe_hold') is not True:
            return None
        keys = ('robot_session', 'map_id', 'order_id', 'seq', 'order_digest', 'release_index', 'stopped_at')
        if any(type(local.get(k)) is not type(query[k]) or local.get(k) != query[k] for k in keys):
            return None
        event = local.get('event')
        if not isinstance(event, dict) or set(event) != _EVENT or event['type'] != 'rear_clear':
            return None
        if any(type(event.get(k)) is not type(query[k]) or event.get(k) != query[k]
               for k in ('robot_session', 'map_id', 'order_id', 'seq', 'stopped_at')):
            return None
        if not _text(event['event_id']):
            return None
        try:
            digest = hashlib.sha256(canonical(event)).hexdigest()
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            return None
        if query['event_id'] is not None and (query['event_id'] != event['event_id'] or query['event_digest'] != digest):
            return None
        return copy.deepcopy(event), digest, (local.get('error_reason'), local.get('book_finished'),
                                             local.get('disconnect_pending'))

    def probe(self, request, local, generation):
        if not self._check(request, 'probe', ()) or type(generation) is not int or generation < 1:
            return None
        nonce = request['nonce']
        existing = self._seen.get(nonce)
        now = self._clock()
        if existing is not None:
            if (existing['request'] != request or existing['generation'] != generation
                    or now < existing['issued'] or now >= existing['deadline']
                    or self._local_event(request, local) != existing['evidence']):
                return None
            return copy.deepcopy(existing['snapshot'])
        if len(self._seen) >= self._limit:
            return None
        # Consume the signed nonce even if the robot cannot provide evidence.
        # A delayed request must not become valid merely because state changes.
        evidence = self._local_event(request, local)
        self._seen[nonce] = {'request': copy.deepcopy(request), 'issued': now, 'deadline': now + self._ttl,
                             'generation': generation, 'evidence': evidence, 'snapshot': None, 'ack': None}
        if evidence is None:
            return None
        event, digest, _status = evidence
        robot_nonce = self._random()
        if not _hex(robot_nonce):
            return None
        body = {k: v for k, v in request.items() if k != 'mac'}
        body.update(kind='snapshot', event_id=event['event_id'], event_digest=digest,
                    connection_generation=generation, robot_nonce=robot_nonce, event=event)
        result = self._signed(body)
        self._seen[nonce]['snapshot'] = result
        return copy.deepcopy(result)

    def commit(self, request, local, generation):
        if not self._check(request, 'commit', ('connection_generation', 'robot_nonce', 'decision')):
            return None
        if (type(generation) is not int or generation < 1 or type(request['connection_generation']) is not int
                or request['connection_generation'] != generation or not _hex(request['robot_nonce'])
                or request['decision'] != 'hold_confirmed'):
            return None
        entry = self._seen.get(request['nonce'])
        if entry is None or entry['snapshot'] is None or entry['generation'] != generation:
            return None
        now = self._clock()
        if now < entry['issued'] or now >= entry['deadline']:
            return None
        expected = {k: v for k, v in entry['snapshot'].items() if k not in ('mac', 'event')}
        expected.update(kind='commit', decision='hold_confirmed')
        if {k: v for k, v in request.items() if k != 'mac'} != expected:
            return None
        if self._local_event(entry['request'], local) != entry['evidence']:
            return None
        if entry['ack'] is None:
            expected['kind'] = 'commit_ack'
            entry['ack'] = self._signed(expected)
        return copy.deepcopy(entry['ack'])
