"""Bounded metrics derived from the existing isolated simulation journal.

Times are SQLite UTC wall-clock observations rounded to seconds, not physical
robot telemetry. Stock changes cannot be reconstructed from this journal.
"""
from datetime import datetime

from server.order_persistence import PersistentOrderStore


_STATES = ('queued', 'dispatching', 'active', 'completed', 'failed', 'unknown')
_MAX_ORDERS = 10000


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
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
        rows = db.execute("SELECT o.state,o.created_at,"
                          "MIN(CASE WHEN e.state='active' THEN e.at END),"
                          "MIN(CASE WHEN e.state='completed' THEN e.at END) "
                          'FROM orders o LEFT JOIN order_events e ON e.request_id=o.request_id '
                          'GROUP BY o.request_id ORDER BY o.sequence').fetchall()
        slots = db.execute('SELECT observed,COUNT(*) FROM shelf_slots GROUP BY observed').fetchall()
        if len(rows) != total:
            raise ValueError('simulation order snapshot differs')
    counts = {state: 0 for state in _STATES}
    waiting, processing, duration, invalid = [], [], [], 0
    for state, created_raw, active_raw, complete_raw in rows:
        if state not in counts:
            raise ValueError('unknown simulated order state')
        counts[state] += 1
        if active_raw is None and state != 'completed':
            continue
        created, active, completed = _time(created_raw), _time(active_raw), _time(complete_raw)
        if (created is None or active is None or active < created
                or (state == 'completed' and (completed is None or completed < active))):
            invalid += 1
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
