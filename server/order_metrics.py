"""Bounded metrics derived from the existing isolated simulation journal.

Times are SQLite UTC wall-clock observations rounded to seconds, not physical
robot telemetry. Stock changes cannot be reconstructed from this journal.
"""
from datetime import datetime

from server.order_persistence import PersistentOrderStore


_STATES = ('queued', 'dispatching', 'active', 'completed', 'failed', 'unknown')
_NEXT_STATES = {
    None: ('queued',),
    'queued': ('dispatching', 'failed'),
    'dispatching': ('active', 'failed', 'unknown'),
    'active': ('active', 'completed', 'failed', 'unknown'),
    'completed': (), 'failed': (), 'unknown': (),
}
_MAX_ORDERS = 10000
_MAX_EVENTS = 250000


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        return result if result.strftime('%Y-%m-%d %H:%M:%S') == value else None
    except ValueError:
        return None


def _mean(samples):
    return {'samples': len(samples), 'average': round(sum(samples) / len(samples), 2) if samples else None}


def order_metrics(store):
    """Read one DB snapshot; never change orders, slots or authorization."""
    if type(store) is not PersistentOrderStore:
        raise ValueError('isolated persistent simulation journal required')
    with store._read() as db:
        total = db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
        if type(total) is not int or total < 0 or total > _MAX_ORDERS:
            raise ValueError('simulation order history exceeds metric limit')
        rows = db.execute('SELECT request_id,state,created_at FROM orders ORDER BY sequence').fetchall()
        slots = db.execute('SELECT observed,COUNT(*) FROM shelf_slots GROUP BY observed').fetchall()
        if len(rows) != total:
            raise ValueError('simulation order snapshot differs')
        # Event sequence is causal insertion order; MIN(at) is not equivalent
        # when a wall clock steps backward. Reject such samples altogether.
        events = {}
        for request_id, state, raw_created in rows:
            if request_id in events or state not in _STATES:
                raise ValueError('invalid simulated order metadata')
            created = _time(raw_created)
            events[request_id] = {'previous': created, 'last_state': None,
                                  'active': None, 'completed': None,
                                  'invalid': created is None, 'count': 0}
        cursor = db.execute('SELECT request_id,state,at FROM order_events ORDER BY sequence')
        for count, (request_id, event_state, raw_time) in enumerate(cursor, 1):
            if count > _MAX_EVENTS:
                raise ValueError('simulation event history exceeds metric limit')
            item = events.get(request_id)
            if item is None or event_state not in _STATES:
                raise ValueError('invalid simulated order event')
            observed = _time(raw_time)
            if (observed is None or item['previous'] is None or observed < item['previous']):
                item['invalid'] = True
            if event_state not in _NEXT_STATES[item['last_state']]:
                item['invalid'] = True
            item['last_state'] = event_state
            if observed is not None:
                item['previous'] = observed
            item['count'] += 1
            if event_state == 'active' and item['active'] is None:
                item['active'] = observed
            if event_state == 'completed' and item['completed'] is None:
                item['completed'] = observed
    counts = {state: 0 for state in _STATES}
    waiting, processing, duration, invalid = [], [], [], 0
    for request_id, state, created_raw in rows:
        if state not in counts:
            raise ValueError('unknown simulated order state')
        counts[state] += 1
        item = events[request_id]
        created, active, completed = _time(created_raw), item['active'], item['completed']
        if (item['invalid'] or not item['count'] or item['last_state'] != state
                or (state in ('active', 'completed') and active is None)
                or (state == 'completed' and completed is None)):
            invalid += 1
            continue
        if active is None:
            continue
        waiting.append((active - created).total_seconds())
        if state == 'completed':
            processing.append((completed - active).total_seconds())
            duration.append((completed - created).total_seconds())
    inventory = {'empty': 0, 'occupied': 0, 'unknown': 0, 'freshness': 'unknown', 'change': None}
    for observed, count in slots:
        if observed not in (None, 0, 1) or type(count) is not int or count < 0:
            raise ValueError('invalid simulated shelf observation')
        inventory[{None: 'unknown', 0: 'empty', 1: 'occupied'}[observed]] += count
    return {'source': 'simulation_journal', 'orders_by_state': counts,
            'waiting_seconds': _mean(waiting), 'processing_seconds': _mean(processing),
            'total_seconds': _mean(duration), 'invalid_timestamps': invalid,
            'timestamp_precision_seconds': 1, 'inventory': inventory}
