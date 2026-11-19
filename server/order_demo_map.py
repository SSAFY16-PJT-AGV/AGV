"""Read-only map and occupancy for the isolated simulated web demo."""
from contextlib import closing
import copy
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from server.sim_handoff_store import SimHandoffStore
from server.sim_runtime import SimRuntime
from server.allocation_store import _verify_field_global
from server.order_publisher import validate_order
from server.segment_progress import SegmentProgress, _canonical, _read_order
from server.segments import plan_segment

_MAX_NODES = 2048
_MAX_EDGES = 4096
_MAX_POINTS = 20000
_MAX_RESOURCES = 1000


def _position(point):
    if (not isinstance(point, (list, tuple)) or len(point) != 2
            or any(type(v) not in (int, float) or not math.isfinite(v)
                   or abs(v) > 100000000 for v in point)):
        raise ValueError('invalid simulated map position')
    return [point[0], point[1]]


def _map(data):
    if not isinstance(data, dict):
        raise ValueError('invalid simulated map')
    name, version = data.get('map_id'), data.get('version')
    if (type(name) is not str or not name or len(name) > 128
            or type(version) is not int or not 0 <= version <= 1000000):
        raise ValueError('invalid simulated map identity')
    nodes, edges, zones = data.get('nodes'), data.get('edges'), data.get('zones', [])
    if (not isinstance(nodes, list) or not 1 <= len(nodes) <= _MAX_NODES
            or not isinstance(edges, list) or len(edges) > _MAX_EDGES
            or not isinstance(zones, list) or len(zones) > _MAX_RESOURCES):
        raise ValueError('simulated map exceeds bounds')
    node_rows, positions = [], {}
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError('invalid simulated node')
        node_id = node.get('id')
        if type(node_id) is not str or not node_id or len(node_id) > 128 or node_id in positions:
            raise ValueError('invalid simulated node identity')
        positions[node_id] = _position(node.get('pos'))
        node_rows.append({'id': node_id, 'pos': positions[node_id]})
    edge_rows, edge_keys, points = [], set(), 0
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError('invalid simulated edge')
        start, end, shape = edge.get('from'), edge.get('to'), edge.get('shape')
        if (type(start) is not str or type(end) is not str or start not in positions
                or end not in positions or start == end or not isinstance(shape, list)
                or len(shape) < 2):
            raise ValueError('invalid simulated edge geometry')
        rendered = [_position(point) for point in shape]
        points += len(rendered)
        if points > _MAX_POINTS or rendered[0] != positions[start] or rendered[-1] != positions[end]:
            raise ValueError('invalid simulated edge endpoints')
        edge_keys.add(tuple(sorted((start, end))))
        edge_rows.append({'from': start, 'to': end, 'shape': rendered})
    zone_ids = set()
    for zone in zones:
        if not isinstance(zone, dict) or type(zone.get('id')) is not str or not zone['id'] or len(zone['id']) > 128:
            raise ValueError('invalid simulated zone')
        if zone['id'] in zone_ids:
            raise ValueError('duplicate simulated zone')
        zone_ids.add(zone['id'])
    return {'map_id': '{}@v{}'.format(name, version), 'nodes': node_rows, 'edges': edge_rows}, positions, edge_keys, zone_ids


def _verify_occupancy(db, runtime, store, progress, robots, task_owners):
    # Each configured simulated robot must have exactly one active task or
    # idle node. A missing DB row is unknown, never an unoccupied resource.
    if not store._idle_enabled:
        raise ValueError('idle simulation occupancy is unavailable')
    progress._check_database(db)
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('simulation database has orphan records')
    _verify_field_global(db, field_required=runtime._field_simulation)
    if runtime._field_simulation:
        store._verify_field_fleet(db)
    idle = db.execute('SELECT robot_id,node FROM sim_idle_robots').fetchall()
    active = db.execute('SELECT robot_id,task_id FROM sim_active_robots').fetchall()
    idle_ids, active_ids = {row[0] for row in idle}, {row[0] for row in active}
    if (len(idle_ids) != len(idle) or len(active_ids) != len(active)
            or idle_ids & active_ids or idle_ids | active_ids != robots
            or len({node for _,node in idle}) != len(idle)):
        raise ValueError('simulated fleet ownership is inconsistent')
    jobs = db.execute('SELECT j.task_id,j.robot_id,t.status FROM allocation_jobs j '
                      'LEFT JOIN tasks t ON t.task_id=j.task_id').fetchall()
    assigned = {(robot, task) for task,robot,status in jobs if status == 'ASSIGNED'}
    if assigned != set(active) or any(status not in ('ASSIGNED','DONE') for _,_,status in jobs):
        raise ValueError('simulated active jobs are inconsistent')
    if db.execute("SELECT 1 FROM allocation_occupancy o JOIN tasks t ON t.task_id=o.task_id "
                  "WHERE t.status!='ASSIGNED' LIMIT 1").fetchone():
        raise ValueError('completed simulated job still owns resources')
    for robot, task in active:
        if task_owners.get(task) != robot:
            raise ValueError('simulated task differs from configured robot')
        owner, path, stop = progress._job(db, task, robot)
        if owner != robot:
            raise ValueError('simulated task owner differs')
        initial = db.execute('SELECT payload,state FROM allocation_outbox WHERE task_id=?', (task,)).fetchone()
        if initial is None:
            raise ValueError('initial simulated order missing')
        if initial[1] == 'pending':
            order = _read_order(initial[0])
            validate_order(robot, order)
            if runtime._field_simulation or len(set(path)) != len(path):
                # Field consistency has already been verified globally; its
                # occurrence-aware segment is still checked below.
                segment = progress._segment_for_task(db, task, path, stop)
            else:
                segment = plan_segment(progress._map, path)
            if (order['order_id'] != task or order['seq'] != 1 or order['map_id'] != progress._map_id
                    or order['nodes'] != path or order['released_until'] != stop
                    or stop != segment.released_until
                    or db.execute('SELECT 1 FROM progress_orders WHERE task_id=? LIMIT 1',(task,)).fetchone()):
                raise ValueError('pending simulated order differs')
            seq = 1
        else:
            seq, _, _, _ = progress._latest(db, task, robot, path, stop)
            segment = progress._segment_for_task(db, task, path, stop)
        finished = db.execute("SELECT 1 FROM progress_events WHERE task_id=? AND seq=? AND result='finished'",
                              (task,seq)).fetchone()
        if finished and stop != path[-1]:
            raise ValueError('finished simulated task is not at final node')
        expected = {('node',path[-1])} if finished else set(segment.resources)
        held = set(db.execute('SELECT kind,resource_id FROM allocation_occupancy WHERE task_id=?',(task,)).fetchall())
        if held != expected:
            raise ValueError('simulated occupancy differs from current segment')


def read_map(runtime):
    """Observe one simulation allocation snapshot; never grants movement."""
    if type(runtime) is not SimRuntime:
        raise ValueError('explicit isolated simulation runtime required')
    with runtime._runtime_read_operation():
        with runtime._lock:
            runtime._ready()
            store = runtime.store
            if type(store) is not SimHandoffStore or store is not runtime._simulation_store:
                raise ValueError('bound simulation store required')
            progress = runtime.progress
            if type(progress) is not SegmentProgress or progress.store is not store:
                raise ValueError('bound simulation progress required')
            map_data = copy.deepcopy(runtime.map_data)
            allowed_robots = frozenset(runtime._sessions)
            task_owners = {task: value[0] for task, value in runtime._tasks.items()}
        if hashlib.sha256(_canonical(map_data)).hexdigest() != progress._fingerprint:
            raise ValueError('simulation map fingerprint differs')
        rendered, nodes, edges, zones = _map(map_data)
        if rendered['map_id'] != progress._map_id:
            raise ValueError('simulation map identity differs')
        store.validate_bound(runtime.path)
        uri = Path(runtime.path).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            _verify_occupancy(db, runtime, store, progress, allowed_robots, task_owners)
            rows = db.execute('SELECT o.kind,o.resource_id,j.robot_id FROM allocation_occupancy o '
                              'LEFT JOIN allocation_jobs j ON j.task_id=o.task_id LIMIT ?',
                              (_MAX_RESOURCES + 1,)).fetchall()
            if store._idle_enabled:
                rows += [('node', node, robot) for robot, node in db.execute(
                    'SELECT robot_id,node FROM sim_idle_robots LIMIT ?', (_MAX_RESOURCES + 1,)).fetchall()]
        if len(rows) > _MAX_RESOURCES:
            raise ValueError('simulated occupancy exceeds bounds')
        seen, occupancy = set(), []
        for kind, resource, robot in rows:
            if (kind not in ('node', 'edge', 'zone') or type(resource) is not str
                    or not resource or len(resource) > 300 or robot not in allowed_robots):
                raise ValueError('invalid simulated occupancy')
            identity = (kind, resource)
            if identity in seen:
                raise ValueError('duplicate simulated occupancy')
            seen.add(identity)
            if kind == 'node' and resource not in nodes:
                raise ValueError('occupancy node is not on map')
            if kind == 'zone' and resource not in zones:
                raise ValueError('occupancy zone is not on map')
            if kind == 'edge':
                pair = json.loads(resource)
                if (not isinstance(pair, list) or len(pair) != 2
                        or any(type(item) is not str for item in pair)
                        or pair != sorted(pair) or tuple(pair) not in edges):
                    raise ValueError('occupancy edge is not on map')
            occupancy.append({'kind': kind, 'resource_id': resource, 'robot_id': robot})
        store.validate_bound(runtime.path)
        with runtime._lock:
            runtime._ready()
            if runtime.store is not store or runtime.progress is not progress:
                raise RuntimeError('simulation store changed')
        return {'scope': 'isolated_simulation', 'coordinate_unit': 'mm',
                'snapshot_consistency': 'separate_from_status', **rendered,
                'occupancy': sorted(occupancy, key=lambda row: (row['kind'], row['resource_id']))}
