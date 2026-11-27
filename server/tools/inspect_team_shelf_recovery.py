"""Inspect a stopped simulated team-shelf state without opening its DBs for writing.

The output is historical, unverified information only: no world, position,
physical inventory, or permission to move can be derived from it. Stop the
simulation before inspection. The tool refuses live WAL or rollback journals
instead of replaying them or creating SQLite sidecars.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import sys


class InspectionUnavailable(ValueError):
    """The requested state cannot be inspected safely."""


_MAX_BYTES = 32 * 1024 * 1024
_MAX_ORDERS = 500
_PRODUCTS = frozenset(('milk', 'tissue', 'egg', 'juice'))
_ORDER_STATES = frozenset(('queued', 'dispatching', 'active', 'completed', 'failed', 'unknown'))
_REQUEST = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)
_IDENTITY = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)


def _sidecars_absent(path):
    # Both a live transaction and an unprocessed crash journal can make the
    # main DB stale. Do not replay them or let SQLite create a shared-memory
    # file merely to display unverified observations.
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise InspectionUnavailable('simulation must be stopped and journals resolved')


def _snapshot(path):
    _sidecars_absent(path)
    if path.is_symlink() or not path.is_file():
        raise InspectionUnavailable('state database is unavailable')
    try:
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if not 0 < before.st_size <= _MAX_BYTES:
                raise InspectionUnavailable('state database size is unavailable')
            first = stream.read(_MAX_BYTES + 1)
            stream.seek(0)
            second = stream.read(_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = path.stat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if (first != second or len(first) != before.st_size or len(first) > _MAX_BYTES
                or identity(before) != identity(after) or identity(after) != identity(current)):
            raise InspectionUnavailable('state database changed during inspection')
        _sidecars_absent(path)
        return first, identity(current)
    except OSError as exc:
        raise InspectionUnavailable('state database is unavailable') from exc


def _memory_database(data):
    try:
        db = sqlite3.connect(':memory:', isolation_level=None)
        db.deserialize(data)
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise InspectionUnavailable('state database integrity is unavailable')
        return db
    except (sqlite3.Error, ValueError) as exc:
        try:
            db.close()
        except UnboundLocalError:
            pass
        raise InspectionUnavailable('state database is invalid') from exc


def _identity(value):
    return value if isinstance(value, str) and _IDENTITY.fullmatch(value) else None


def _tables(db, names):
    for name in names:
        if db.execute('SELECT type FROM sqlite_master WHERE name=?', (name,)).fetchone() != ('table',):
            raise InspectionUnavailable('state database tables are unavailable')


def inspect_state(state_dir):
    """Return bounded historical summaries; never report an actionable state."""
    folder = Path(state_dir)
    if not folder.is_absolute() or not folder.is_dir():
        raise InspectionUnavailable('an existing absolute state directory is required')
    orders_path, slots_path = folder / 'orders.sqlite3', folder / 'team-slots.sqlite3'
    orders_bytes, orders_identity = _snapshot(orders_path)
    slots_bytes, slots_identity = _snapshot(slots_path)
    try:
        with closing(_memory_database(orders_bytes)) as orders_db, closing(_memory_database(slots_bytes)) as slots_db:
            if (orders_db.execute('PRAGMA application_id').fetchone() != (0x41475653,)
                    or orders_db.execute('PRAGMA user_version').fetchone() != (2,)
                    or slots_db.execute('PRAGMA application_id').fetchone() != (1095194691,)
                    or slots_db.execute('PRAGMA user_version').fetchone() != (3,)
                    or orders_db.execute('PRAGMA foreign_key_check').fetchone() is not None
                    or slots_db.execute('PRAGMA foreign_key_check').fetchone() is not None):
                raise InspectionUnavailable('state database schema is unavailable')
            _tables(orders_db, ('metadata', 'orders'))
            _tables(slots_db, ('identity', 'slots', 'claims'))
            metadata = orders_db.execute('SELECT pickup_id FROM metadata WHERE singleton=1').fetchone()
            identity = slots_db.execute('SELECT creation_id FROM identity WHERE singleton=1').fetchone()
            if metadata is None or identity is None:
                raise InspectionUnavailable('state database binding is unavailable')
            saved_id, slot_id = _identity(metadata[0]), _identity(identity[0])
            binding = ('unavailable' if saved_id is None or slot_id is None else
                       'matched' if saved_id == slot_id else 'mismatch')
            rows = orders_db.execute('SELECT request_id,product_id,state FROM orders '
                                     'ORDER BY sequence DESC LIMIT ?', (_MAX_ORDERS + 1,)).fetchall()
            truncated = len(rows) > _MAX_ORDERS
            orders = []
            for request_id, product, recorded_state in rows[:_MAX_ORDERS]:
                if (not isinstance(request_id, str) or _REQUEST.fullmatch(request_id) is None
                        or product not in _PRODUCTS or recorded_state not in _ORDER_STATES):
                    raise InspectionUnavailable('order record is invalid')
                orders.append({'request_id': request_id, 'product_id': product, 'status': 'unknown',
                               'recorded_state': recorded_state})
            actual_slots = slots_db.execute('SELECT slot_id,shelf_id,slot_index FROM slots ORDER BY slot_id LIMIT 25').fetchall()
            expected_slots = [(i, 'N%02d' % ((i - 1) // 4 + 1), (i - 1) % 4) for i in range(1, 25)]
            if actual_slots != expected_slots:
                raise InspectionUnavailable('slot layout is invalid')
            claim_rows = slots_db.execute('SELECT slot_id FROM claims LIMIT 25').fetchall()
            claims = {row[0] for row in claim_rows}
            if len(claim_rows) > 24 or len(claims) != len(claim_rows) or not claims <= set(range(1, 25)):
                raise InspectionUnavailable('slot claim is invalid')
            slots = [{'slot_id': i, 'shelf_id': shelf, 'slot_index': index, 'state': 'unknown',
                      'claim_recorded': True if i in claims else None} for i, shelf, index in expected_slots]
    except sqlite3.Error as exc:
        raise InspectionUnavailable('state database is invalid') from exc
    # No output is returned if either file changed while the other was read.
    for path, old in ((orders_path, orders_identity), (slots_path, slots_identity)):
        try:
            now = path.stat()
            if (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) != old:
                raise InspectionUnavailable('state database changed during inspection')
            _sidecars_absent(path)
        except OSError as exc:
            raise InspectionUnavailable('state database is unavailable') from exc
    return {'scope': 'stopped_team_shelf_record_inspection', 'source': 'historical_unverified',
            'motion_authorized': False, 'inventory_fresh': False, 'binding': binding,
            'orders_truncated': truncated, 'orders': orders, 'slots': slots}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True,
                        help='absolute path of the stopped, dedicated team-shelf state directory')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(inspect_state(args.state_dir), ensure_ascii=False, indent=2))
    except InspectionUnavailable:
        # Do not print DB contents, credentials, raw SQLite messages or paths.
        print('Inspection unavailable: stop the simulation and check the preserved state.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
