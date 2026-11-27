"""Transactional allocation and outbound-order journal; never publishes MQTT.

Caller must provide the entire canonical conflict set and an independently
verified location. This is not an operational authorization or safety layer.
Uncertain records are deliberately not reset or sent automatically.
"""
from contextlib import closing, contextmanager
import copy
import hashlib
import json
import math
import os
import re
import sqlite3


class AllocationConflict(ValueError):
    """Task, robot, resource or outbound order is in conflict."""


class InvalidAllocation(ValueError):
    """Data is invalid or too ambiguous for an allocation."""


def _fingerprint(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise InvalidAllocation('map_fingerprint must be a lowercase SHA-256 hex string')
    return value


def _field_route(context, map_id, nodes, fingerprint):
    """Validate a simulation-only map snapshot and its candidate route."""
    from server.field_map_planner import plan_field_route
    if not isinstance(context, dict) or set(context) != {'map_data', 'incoming_edge', 'goal'}:
        raise InvalidAllocation('field route requires map, incoming edge and goal')
    try:
        _validate_json(context)
        data = context['map_data']
        raw = json.dumps(data, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        if hashlib.sha256(raw.encode('ascii')).hexdigest() != fingerprint:
            raise ValueError('field map fingerprint differs')
        route = plan_field_route(data, map_ref=map_id, incoming_edge=context['incoming_edge'], goal=context['goal'])
        if list(route.nodes) != nodes:
            raise ValueError('field route differs from candidate')
        return raw
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise InvalidAllocation('invalid field route provenance') from exc


def _stored_field_route(db, task_id, map_id, nodes, fingerprint):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_route_provenance'").fetchone()
    if exists is None:
        return None  # Older unique-route simulation databases remain readable.
    row = db.execute('SELECT map_json,incoming_edge,goal FROM field_route_provenance WHERE task_id=?',
                     (task_id,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
        context = {'map_data': data, 'incoming_edge': row[1], 'goal': row[2]}
        if _field_route(context, map_id, nodes, fingerprint) != row[0]:
            raise ValueError('field map is not canonical')
        return {'incoming_edge': row[1], 'goal': row[2]}
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise AllocationConflict('stored field route provenance is invalid') from exc


def _verify_field_global(db, *, field_required=False):
    """Fail closed if any field-simulation allocation has lost its occupancy.

    This runs inside the caller's SQLite write transaction and establishes only
    database consistency; it is not a physical position or clearance proof.
    """
    mode = db.execute("SELECT value FROM allocation_meta WHERE key='field_route_mode'").fetchone()
    field_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_route_provenance'").fetchone()
    if mode is None:
        if field_table and db.execute('SELECT 1 FROM field_route_provenance LIMIT 1').fetchone():
            raise AllocationConflict('field route mode is missing')
        if field_required and db.execute('SELECT 1 FROM allocation_jobs LIMIT 1').fetchone():
            raise AllocationConflict('existing field allocation has no route mode')
        return
    if mode != ('required',) or not field_table:
        raise AllocationConflict('field route mode or provenance table is invalid')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise AllocationConflict('field simulation database has orphan records')
    jobs = db.execute('SELECT j.task_id,t.status,j.map_id,j.nodes_json,j.released_until '
                      'FROM allocation_jobs j JOIN tasks t ON t.task_id=j.task_id ORDER BY j.task_id').fetchall()
    if not jobs:
        raise AllocationConflict('field route mode has no allocation history')
    from server.segment_progress import SegmentProgress, _read_order
    from server.route_occurrence import plan_occurrence_segment
    provenance = db.execute('SELECT map_json FROM field_route_provenance WHERE task_id=?', (jobs[0][0],)).fetchone()
    if provenance is None:
        raise AllocationConflict('field route provenance is missing')
    try:
        data = json.loads(provenance[0])
        progress = SegmentProgress(None, data, simulation_enabled=True, field_simulation=True)
        has_progress = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='progress_orders'").fetchone()
        has_events = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='progress_events'").fetchone()
        for task_id, status, map_id, nodes_json, stopped in jobs:
            robot, path, stop = progress._job(db, task_id) if status == 'ASSIGNED' else (None, None, None)
            if status != 'ASSIGNED':
                from server.sim_reroute_transition import is_prepared
                if is_prepared(db, task_id):
                    raise AllocationConflict('completed task retains an unfinished reroute')
                # The separate handoff workflow can retain completed history,
                # but completed jobs must no longer own active resources.
                if status != 'DONE' or db.execute('SELECT 1 FROM allocation_occupancy WHERE task_id=?',
                                                  (task_id,)).fetchone():
                    raise AllocationConflict('field task status or occupancy is inconsistent')
                nodes = json.loads(nodes_json)
                if _stored_field_route(db, task_id, map_id, nodes, progress._fingerprint) is None:
                    raise AllocationConflict('completed field route provenance is missing')
                continue
            initial = db.execute('SELECT payload,state FROM allocation_outbox WHERE task_id=?', (task_id,)).fetchone()
            if initial is None or initial[1] not in ('pending', 'uncertain', 'acked'):
                raise AllocationConflict('field task initial order is unavailable')
            rows = (db.execute('SELECT seq,payload,state FROM progress_orders WHERE task_id=? ORDER BY seq',
                               (task_id,)).fetchall() if has_progress else [])
            if rows:
                seq, _payload, _state, order = progress._latest(db, task_id, robot, path, stop)
                segment = progress._segment_for_task(db, task_id, path, stop)
            else:
                order = _read_order(initial[0])
                from server.order_publisher import validate_order
                from server.field_robot_contract import validate_field_robot_order
                validate_order(robot, order)
                validate_field_robot_order(order)
                segment = plan_occurrence_segment(data, path, 0)
                if (order.get('order_id') != task_id or order.get('seq') != 1
                        or order.get('map_id') != map_id or order.get('nodes') != path
                        or order.get('released_until') != segment.released_until or stop != segment.released_until):
                    raise AllocationConflict('initial field order is inconsistent')
                if len(set(path)) != len(path):
                    index = db.execute('SELECT current_index FROM occurrence_allocations WHERE task_id=?',
                                       (task_id,)).fetchone()
                    if index != (segment.end_index,):
                        raise AllocationConflict('initial field visit index is inconsistent')
                seq = 1
            finished = (db.execute("SELECT 1 FROM progress_events WHERE task_id=? AND seq=? AND result='finished'",
                                   (task_id, seq)).fetchone() if has_events else None)
            if finished and (stop != path[-1] or order['released_until'] != path[-1]):
                raise AllocationConflict('finished field task has not reached its destination')
            expected = {('node', path[-1])} if finished else set(segment.resources)
            from server.sim_reroute_transition import is_prepared, verify_prepared
            if finished and is_prepared(db, task_id):
                raise AllocationConflict('finished task retains an unfinished reroute')
            old_expected = set(expected)
            expected |= verify_prepared(db, progress, task_id, robot, path, stop, seq,
                                        initial[0] if not rows else _payload, old_expected)
            from server.sim_reroute_finish import finished_resources
            completed = finished_resources(db, progress, task_id, robot, path, old_expected)
            if completed is not None:
                expected = completed
            held = set(db.execute('SELECT kind,resource_id FROM allocation_occupancy WHERE task_id=?',
                                  (task_id,)).fetchall())
            if held != expected:
                raise AllocationConflict('field task occupancy differs from its verified segment')
    except AllocationConflict:
        raise
    except (ValueError, TypeError, KeyError, IndexError, RecursionError, sqlite3.Error) as exc:
        raise AllocationConflict('field simulation database is inconsistent') from exc


def _id(value, name, topic_component=False):
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidAllocation(name + ' must be a non-empty string')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise InvalidAllocation(name + ' must be UTF-8 encodable') from exc
    if '\x00' in value or (topic_component and any(c in value for c in '/+#')):
        raise InvalidAllocation(name + ' contains disallowed characters')
    return value


def _validate_json(value):
    pending = [(value, 0, False)]
    active = set()
    while pending:
        item, depth, leaving = pending.pop()
        if leaving:
            active.remove(id(item))
            continue
        if depth > 32:
            raise InvalidAllocation('JSON nesting exceeds the temporary limit')
        if item is None or isinstance(item, (bool, int)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise InvalidAllocation('JSON numbers must be finite')
        elif isinstance(item, str):
            try:
                item.encode('utf-8')
            except UnicodeEncodeError as exc:
                raise InvalidAllocation('JSON strings must be UTF-8 encodable') from exc
        elif isinstance(item, list):
            if id(item) in active:
                raise InvalidAllocation('cyclic JSON data is invalid')
            active.add(id(item)); pending.append((item, depth, True))
            pending.extend((v, depth + 1, False) for v in item)
        elif isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise InvalidAllocation('JSON keys must be strings')
            if id(item) in active:
                raise InvalidAllocation('cyclic JSON data is invalid')
            active.add(id(item)); pending.append((item, depth, True))
            pending.extend((v, depth + 1, False) for v in item.keys())
            pending.extend((v, depth + 1, False) for v in item.values())
        else:
            raise InvalidAllocation('value must be JSON-compatible')


def _input(task_id, robot_id, map_id, nodes, released_until, resources, action_at_end, *, release_index=None):
    _id(task_id, 'task_id'); _id(robot_id, 'robot_id', True); _id(map_id, 'map_id')
    if '@' not in map_id:
        raise InvalidAllocation('map_id must include a version')
    map_name, version = map_id.rsplit('@', 1)
    if (not map_name.strip() or not version.startswith('v') or not version[1:]
            or not version[1:].isascii() or not version[1:].isdecimal()):
        raise InvalidAllocation('map_id must use map-name@v<nonnegative integer>')
    if not isinstance(nodes, list) or len(nodes) < 2:
        raise InvalidAllocation('nodes must contain at least a start and next node')
    for node in nodes:
        _id(node, 'node')
    repeated = len(nodes) != len(set(nodes))
    if repeated:
        if not robot_id.startswith('sim-') or not robot_id[4:].strip():
            raise InvalidAllocation('repeated routes are only supported for isolated simulation')
        if type(release_index) is not int or not 1 <= release_index < len(nodes):
            raise InvalidAllocation('repeated route requires an explicit release index')
    elif release_index is not None:
        raise InvalidAllocation('release index is only accepted for repeated routes')
    _id(released_until, 'released_until')
    if released_until not in nodes:
        raise InvalidAllocation('released_until must be a route node')
    end = release_index if repeated else nodes.index(released_until)
    if repeated and (nodes[end] != released_until or end != nodes.index(released_until, 1)):
        raise InvalidAllocation('initial release must be the first matching route occurrence')
    if end < 1:
        raise InvalidAllocation('released_until must include at least one edge')
    if not isinstance(resources, (list, tuple, set, frozenset)) or not resources:
        raise InvalidAllocation('resources must be a nonempty collection')
    checked = set()
    for resource in resources:
        if not isinstance(resource, tuple) or len(resource) != 2 or resource[0] not in ('node', 'edge', 'zone'):
            raise InvalidAllocation('invalid resource')
        checked.add((resource[0], _id(resource[1], 'resource id')))
    prefix = nodes[:end + 1]
    required = {('node', node) for node in prefix}
    required.update(('edge', json.dumps(sorted([a, b]), ensure_ascii=True, separators=(',', ':')))
                    for a, b in zip(prefix, prefix[1:]))
    if not required.issubset(checked):
        raise InvalidAllocation('all released nodes and canonical edges must be reserved')
    if not isinstance(action_at_end, dict):
        raise InvalidAllocation('action_at_end must be an object')
    _validate_json(action_at_end)
    try:
        payload = json.dumps({'order_id': task_id, 'seq': 1, 'map_id': map_id,
                              'nodes': nodes, 'released_until': released_until,
                              'action_at_end': action_at_end}, ensure_ascii=False,
                             allow_nan=False, separators=(',', ':'))
    except (ValueError, OverflowError, RecursionError) as exc:
        raise InvalidAllocation('order cannot be serialized') from exc
    if repeated:
        try:
            from server.field_robot_contract import validate_field_robot_order
            validate_field_robot_order(json.loads(payload))
        except (ValueError, TypeError, RecursionError) as exc:
            raise InvalidAllocation('repeated simulation order violates team order limits') from exc
    return checked, payload


class AllocationStore:
    def __init__(self, path, *, field_required=False):
        path = os.fspath(path)
        if not isinstance(path, str) or not path or path == ':memory:':
            raise ValueError('a file-backed database path is required')
        if type(field_required) is not bool:
            raise ValueError('field_required must be boolean')
        self.path = path
        self._field_required = field_required

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute('PRAGMA foreign_keys = ON')
        return connection

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def initialize(self):
        with self._write() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks '
                       '(task_id TEXT PRIMARY KEY NOT NULL, status TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS allocation_jobs '
                       '(task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(task_id), '
                       'robot_id TEXT NOT NULL UNIQUE, map_id TEXT NOT NULL, nodes_json TEXT NOT NULL, '
                       'released_until TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS allocation_occupancy '
                       '(kind TEXT NOT NULL, resource_id TEXT NOT NULL, task_id TEXT NOT NULL '
                       'REFERENCES allocation_jobs(task_id), PRIMARY KEY(kind, resource_id))')
            db.execute('CREATE TABLE IF NOT EXISTS allocation_outbox '
                       '(task_id TEXT PRIMARY KEY NOT NULL REFERENCES allocation_jobs(task_id), '
                       'payload TEXT NOT NULL, state TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS allocation_meta '
                       '(key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS occurrence_allocations '
                       '(task_id TEXT PRIMARY KEY NOT NULL REFERENCES allocation_jobs(task_id), '
                       'current_index INTEGER NOT NULL CHECK(current_index > 0))')
            db.execute('CREATE TABLE IF NOT EXISTS field_route_provenance '
                       '(task_id TEXT PRIMARY KEY NOT NULL REFERENCES allocation_jobs(task_id), '
                       'map_json TEXT NOT NULL, incoming_edge TEXT NOT NULL, goal TEXT NOT NULL)')
            maps = [row[0] for row in db.execute('SELECT DISTINCT map_id FROM allocation_jobs')]
            if len(maps) > 1:
                raise AllocationConflict('existing allocations use multiple maps; manual reconciliation required')
            active = db.execute("SELECT value FROM allocation_meta WHERE key='active_map'").fetchone()
            fingerprint = db.execute("SELECT value FROM allocation_meta WHERE key='map_fingerprint'").fetchone()
            if (maps and (active is None or fingerprint is None)) or ((active is None) != (fingerprint is None)):
                raise AllocationConflict('map fingerprint missing; manual reconciliation required')
            if fingerprint is not None:
                try:
                    _fingerprint(fingerprint[0])
                except InvalidAllocation as exc:
                    raise AllocationConflict('invalid saved map fingerprint') from exc
            if active is not None and maps and active[0] != maps[0]:
                raise AllocationConflict('active map differs from existing allocation; manual reconciliation required')

    def create_task(self, task_id):
        _id(task_id, 'task_id')
        try:
            with self._write() as db:
                db.execute('INSERT INTO tasks(task_id, status) VALUES (?, ?)', (task_id, 'PENDING'))
        except sqlite3.IntegrityError as exc:
            raise AllocationConflict('task already exists') from exc

    def allocate(self, task_id, robot_id, map_id, nodes, released_until, resources, action_at_end, *, map_fingerprint,
                 release_index=None, field_context=None):
        _fingerprint(map_fingerprint)
        try:
            nodes_copy, resources_copy, action_copy = copy.deepcopy((nodes, resources, action_at_end))
        except (RecursionError, TypeError, ValueError) as exc:
            raise InvalidAllocation('allocation input cannot be copied') from exc
        checked, payload = _input(task_id, robot_id, map_id, nodes_copy, released_until, resources_copy, action_copy,
                                  release_index=release_index)
        if self._field_required and field_context is None:
            raise AllocationConflict('field route provenance is required for this store')
        field_json = None
        if field_context is not None:
            if not robot_id.startswith('sim-') or not robot_id[4:].strip():
                raise InvalidAllocation('field route is limited to isolated simulation')
            from server.field_robot_contract import validate_field_robot_order
            validate_field_robot_order(json.loads(payload))
            field_context = copy.deepcopy(field_context)
            field_json = _field_route(field_context, map_id, nodes_copy, map_fingerprint)
        nodes_json = json.dumps(nodes_copy, ensure_ascii=False)
        try:
            with self._write() as db:
                _verify_field_global(db, field_required=self._field_required)
                row = db.execute('SELECT status FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                if row is None or row[0] != 'PENDING':
                    raise AllocationConflict('task is missing or not pending')
                active = db.execute("SELECT value FROM allocation_meta WHERE key='active_map'").fetchone()
                fingerprint = db.execute("SELECT value FROM allocation_meta WHERE key='map_fingerprint'").fetchone()
                field_mode = db.execute("SELECT value FROM allocation_meta WHERE key='field_route_mode'").fetchone()
                has_jobs = db.execute('SELECT 1 FROM allocation_jobs LIMIT 1').fetchone() is not None
                if active is None and fingerprint is None and not has_jobs:
                    db.execute('INSERT INTO allocation_meta(key, value) VALUES (?, ?)', ('active_map', map_id))
                    db.execute('INSERT INTO allocation_meta(key, value) VALUES (?, ?)', ('map_fingerprint', map_fingerprint))
                elif (active is None or fingerprint is None or active[0] != map_id
                      or fingerprint[0] != map_fingerprint):
                    raise AllocationConflict('map or fingerprint differs from the persistent active map')
                if field_json is not None:
                    if field_mode is None and not has_jobs:
                        db.execute("INSERT INTO allocation_meta VALUES ('field_route_mode','required')")
                    elif field_mode != ('required',):
                        raise AllocationConflict('cannot mix field and legacy route provenance')
                elif field_mode is not None:
                    raise AllocationConflict('field route provenance is required for this map')
                db.execute('INSERT INTO allocation_jobs VALUES (?, ?, ?, ?, ?)',
                           (task_id, robot_id, map_id, nodes_json, released_until))
                if release_index is not None:
                    db.execute('INSERT INTO occurrence_allocations VALUES (?, ?)', (task_id, release_index))
                if field_json is not None:
                    db.execute('INSERT INTO field_route_provenance VALUES (?, ?, ?, ?)',
                               (task_id, field_json, field_context['incoming_edge'], field_context['goal']))
                db.executemany('INSERT INTO allocation_occupancy VALUES (?, ?, ?)',
                               [(kind, key, task_id) for kind, key in sorted(checked)])
                db.execute('INSERT INTO allocation_outbox VALUES (?, ?, ?)', (task_id, payload, 'pending'))
                db.execute('UPDATE tasks SET status=? WHERE task_id=?', ('ASSIGNED', task_id))
                _verify_field_global(db, field_required=self._field_required)
        except sqlite3.IntegrityError as exc:
            raise AllocationConflict('robot or resource already allocated') from exc
        return self.get_allocation(task_id)

    def get_allocation(self, task_id):
        _id(task_id, 'task_id')
        with closing(self._connect()) as db:
            row = db.execute("SELECT robot_id, map_id, nodes_json, released_until, "
                             "(SELECT value FROM allocation_meta WHERE key='map_fingerprint') "
                             'FROM allocation_jobs WHERE task_id=?',
                             (task_id,)).fetchone()
            if row is None:
                return None
            result = {'task_id': task_id, 'robot_id': row[0], 'map_id': row[1],
                      'nodes': json.loads(row[2]), 'released_until': row[3], 'map_fingerprint': row[4]}
            field = _stored_field_route(db, task_id, row[1], result['nodes'], row[4])
            if field is not None:
                result['field_route'] = field
            return result

    def list_occupancy(self):
        with closing(self._connect()) as db:
            rows = db.execute('SELECT o.kind, o.resource_id, j.robot_id FROM allocation_occupancy o '
                              'JOIN allocation_jobs j ON j.task_id=o.task_id').fetchall()
        return {(kind, resource_id): robot_id for kind, resource_id, robot_id in rows}

    def get_order(self, task_id):
        _id(task_id, 'task_id')
        with closing(self._connect()) as db:
            row = db.execute('SELECT payload, state FROM allocation_outbox WHERE task_id=?', (task_id,)).fetchone()
        return None if row is None else {'payload': row[0], 'state': row[1]}

    def _transition_verified(self, task_id, expected_payload, expected_allocation,
                             expected_resources, expected_state, target_state):
        """Verify the intended order and occupancy within the same write transaction."""
        _id(task_id, 'task_id')
        try:
            allocation = copy.deepcopy(expected_allocation)
            expected = set(copy.deepcopy(expected_resources))
        except (RecursionError, TypeError, ValueError) as exc:
            raise AllocationConflict('invalid expected allocation or resources') from exc
        if not isinstance(expected_payload, str) or not isinstance(allocation, dict):
            raise AllocationConflict('invalid expected allocation or payload')
        with self._write() as db:
            if self._field_required and callable(getattr(self, '_verify_field_fleet', None)):
                self._verify_field_fleet(db)
            _verify_field_global(db, field_required=self._field_required)
            row = db.execute(
                'SELECT t.status,j.robot_id,j.map_id,j.nodes_json,j.released_until,o.payload,o.state '
                'FROM tasks t JOIN allocation_jobs j ON j.task_id=t.task_id '
                'JOIN allocation_outbox o ON o.task_id=t.task_id WHERE t.task_id=?', (task_id,)
            ).fetchone()
            if row is None or row[0] != 'ASSIGNED' or row[6] != expected_state or row[5] != expected_payload:
                raise AllocationConflict('task or order changed before transition')
            try:
                nodes = json.loads(row[3])
                order = json.loads(row[5])
                actual_allocation = {'task_id': task_id, 'robot_id': row[1], 'map_id': row[2],
                                     'nodes': nodes, 'released_until': row[4],
                                     'map_fingerprint': (db.execute("SELECT value FROM allocation_meta WHERE key='map_fingerprint'").fetchone() or (None,))[0]}
                field = _stored_field_route(db, task_id, row[2], nodes, actual_allocation['map_fingerprint'])
                if field is not None:
                    actual_allocation['field_route'] = field
                if (not isinstance(order, dict) or set(order) != {'order_id', 'seq', 'map_id',
                        'nodes', 'released_until', 'action_at_end'} or order['order_id'] != task_id
                        or type(order['seq']) is not int or order['seq'] != 1
                        or order['map_id'] != row[2] or order['nodes'] != nodes
                        or order['released_until'] != row[4]):
                    raise AllocationConflict('order and allocation disagree')
                actual_resources = set(db.execute(
                    'SELECT kind,resource_id FROM allocation_occupancy WHERE task_id=?', (task_id,)
                ).fetchall())
                if actual_allocation != allocation or actual_resources != expected:
                    raise AllocationConflict('allocation or resource occupancy changed')
                table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='occurrence_allocations'").fetchone()
                occurrence = (db.execute('SELECT current_index FROM occurrence_allocations WHERE task_id=?',
                                         (task_id,)).fetchone() if table else None)
                index = None if occurrence is None else occurrence[0]
                _, canonical_payload = _input(task_id, row[1], row[2], nodes, row[4],
                                              actual_resources, order['action_at_end'], release_index=index)
                active = db.execute("SELECT value FROM allocation_meta WHERE key='active_map'").fetchone()
                _fingerprint(actual_allocation['map_fingerprint'])
                if active is None or active[0] != row[2] or canonical_payload != row[5]:
                    raise AllocationConflict('order or active map is inconsistent')
            except (ValueError, TypeError, RecursionError, KeyError) as exc:
                if isinstance(exc, AllocationConflict):
                    raise
                raise AllocationConflict('invalid persisted order or allocation') from exc
            updated = db.execute('UPDATE allocation_outbox SET state=? WHERE task_id=? AND state=?',
                                 (target_state, task_id, expected_state))
            if updated.rowcount != 1:
                raise AllocationConflict('outbox state changed before transition')
            _verify_field_global(db, field_required=self._field_required)
            if self._field_required and callable(getattr(self, '_verify_field_fleet', None)):
                self._verify_field_fleet(db)

    def mark_attempted(self, task_id, expected_payload, expected_allocation, expected_resources):
        """Atomically verify intended order and occupancy before marking uncertain."""
        self._transition_verified(task_id, expected_payload, expected_allocation,
                                  expected_resources, 'pending', 'uncertain')

    def mark_broker_acked(self, task_id, expected_payload, expected_allocation, expected_resources):
        """Atomically reverify the same order and occupancy before recording PUBACK."""
        self._transition_verified(task_id, expected_payload, expected_allocation,
                                  expected_resources, 'uncertain', 'acked')
