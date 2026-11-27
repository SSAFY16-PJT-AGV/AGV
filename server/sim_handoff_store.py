"""Fresh-database-only simulated leg handoff; never a physical drive permit.

The caller must provide a separately isolated broker and an independently
validated simulated Unreal acknowledgement. Neither this module, a token,
nor a simulated event proves a physical robot's state or message identity.
Existing allocation databases are never opened or migrated by create_new().
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from contextlib import closing, contextmanager

from server.allocation_store import (AllocationStore, AllocationConflict, InvalidAllocation, _fingerprint,
                                     _id, _input, _field_route, _verify_field_global)
from server.order_publisher import _topic, validate_order
from server.segments import plan_segment
from server.route_occurrence import plan_occurrence_segment
from server.field_robot_contract import validate_field_robot_order
from server.field_map_planner import plan_field_route
from server.segment_progress import _resources
from sim.event_inbox import _event
from sim.event_receiver import SimEventReceiver


_CONSTRUCTION_KEY = object()
_HANDOFF_FENCE_LOCAL = threading.local()
_SCHEMA_HASHES = {
    False: 'f7bb0641f03d58fbcd478b1a4c0117b33bd1971b7704732bb7c6e786ac011b22',
    True: '77c77335f721d5cd94bee5bbb60b5707bcf0e81f42f6a546bcda9755bf1a82ca',
}


class SimResourceBusy(AllocationConflict):
    """A verified segment is occupied in the isolated simulation transaction."""


@contextmanager
def _reroute_handoff_fence(guard, source_task, store, progress, receiver, gate):
    if guard is None:
        yield None
        return
    from server.sim_reroute_execute_wire import SimRerouteExecutionGuard
    from server.sim_reroute_activate_wire import _check_binding
    if type(guard) is not SimRerouteExecutionGuard:
        raise ValueError('exact live simulated reroute guard required')
    identity = (guard, source_task, store, progress, receiver, gate)
    active = getattr(_HANDOFF_FENCE_LOCAL, 'active', None)
    if active is not None:
        if (active[0][1] != source_task
                or any(active[0][i] is not identity[i] for i in (0, 2, 3, 4, 5))):
            raise ValueError('different simulated reroute handoff fence is active')
        yield active[1]
        return
    runtime = guard.runtime
    if runtime.store is not store or runtime.progress is not progress or runtime.receiver is not receiver or runtime.gate is not gate:
        raise ValueError('reroute handoff must use the bound runtime')
    with runtime._runtime_read_operation():
        robot = _check_binding(runtime, source_task, guard.receipt_inbox, guard.robot_receiver)
        endpoint = guard._execution_receiver
        if endpoint is None:
            raise ValueError('live simulated execution receiver required')
        with guard.robot_receiver._lifecycle_lock, guard.robot_receiver._connection_fence, \
                guard.activation_receiver.endpoint._connection_fence, endpoint.endpoint._connection_fence, \
                robot._lock, runtime._lock:
            runtime._ready()
            if runtime.receiver is not receiver or not receiver.ready:
                raise ValueError('simulated event receiver changed')
            with receiver._dispatch_lock, runtime.inbox._lock, guard._fresh_stop_proof.motion_fence(robot):
                _HANDOFF_FENCE_LOCAL.active = (identity, robot)
                try:
                    yield robot
                finally:
                    _HANDOFF_FENCE_LOCAL.active = None


class SimHandoffStore(AllocationStore):
    """Store for a new, private simulation DB with durable per-robot ownership."""

    def __init__(self, path, *, _key=None):
        if _key is not _CONSTRUCTION_KEY:
            raise ValueError('use create_new for a fresh isolated simulation database')
        super().__init__(path)

    @classmethod
    def create_new(cls, path, *, simulation_enabled=False, idle_positions=None):
        if simulation_enabled is not True:
            raise ValueError('explicit isolated simulation is required')
        if idle_positions is not None:
            if not isinstance(idle_positions, dict) or not idle_positions:
                raise ValueError('nonempty simulated idle positions required')
            idle_positions = copy.deepcopy(idle_positions)
            for robot, node in idle_positions.items():
                _topic(robot)
                _id(node, 'idle node')
            if len(set(idle_positions.values())) != len(idle_positions):
                raise ValueError('idle robots cannot occupy the same node')
        name = os.fspath(path)
        if not isinstance(name, str) or not name or name == ':memory:':
            raise ValueError('a new file-backed database path is required')
        descriptor = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        store = cls(name, _key=_CONSTRUCTION_KEY)
        store._idle_enabled = idle_positions is not None
        store._field_expected_robots = frozenset(idle_positions) if idle_positions is not None else None
        store._field_initial_positions = copy.deepcopy(idle_positions)
        store._reopened = False
        store._created_path = os.path.normcase(os.path.realpath(name))
        store._creation_id = secrets.token_hex(32)
        created_stat = os.stat(name)
        store._creation_file = (created_stat.st_dev, created_stat.st_ino)
        with store._write() as db:
            # Keep the exact five legacy columns, but use a separate table and
            # trigger for the unique *active* robot rather than unique history.
            db.execute('CREATE TABLE tasks (task_id TEXT PRIMARY KEY NOT NULL, status TEXT NOT NULL)')
            db.execute('CREATE TABLE sim_store_identity (value TEXT NOT NULL)')
            db.execute('INSERT INTO sim_store_identity VALUES (?)', (store._creation_id,))
            db.execute('CREATE TABLE allocation_jobs (task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(task_id), '
                       'robot_id TEXT NOT NULL, map_id TEXT NOT NULL, nodes_json TEXT NOT NULL, released_until TEXT NOT NULL)')
            db.execute('CREATE TABLE sim_active_robots (robot_id TEXT PRIMARY KEY NOT NULL, '
                       'task_id TEXT UNIQUE NOT NULL REFERENCES allocation_jobs(task_id))')
            db.execute('CREATE TABLE sim_handoff_transfers (source_task_id TEXT PRIMARY KEY NOT NULL REFERENCES allocation_jobs(task_id), '
                       'target_task_id TEXT UNIQUE NOT NULL REFERENCES tasks(task_id), robot_id TEXT NOT NULL, '
                       'robot_session TEXT NOT NULL, seq INTEGER NOT NULL, stopped_at TEXT NOT NULL, event_id TEXT NOT NULL, '
                       'action_id TEXT UNIQUE NOT NULL, ack_json TEXT NOT NULL)')
            db.execute('CREATE TRIGGER sim_robot_only BEFORE INSERT ON allocation_jobs '
                       "WHEN substr(NEW.robot_id,1,4)!='sim-' OR length(trim(substr(NEW.robot_id,5)))=0 "
                       "OR instr(NEW.robot_id,'/')>0 OR instr(NEW.robot_id,'+')>0 OR instr(NEW.robot_id,'#')>0 BEGIN "
                       "SELECT RAISE(ABORT,'simulation robot ID required'); END")
            db.execute('CREATE TRIGGER sim_activate_robot AFTER INSERT ON allocation_jobs BEGIN '
                       'INSERT INTO sim_active_robots(robot_id,task_id) VALUES(NEW.robot_id,NEW.task_id); END')
            db.execute('CREATE TRIGGER sim_preserve_job BEFORE DELETE ON allocation_jobs BEGIN '
                       "SELECT RAISE(ABORT,'simulation allocation history is immutable'); END")
            db.execute('CREATE TRIGGER sim_preserve_robot BEFORE UPDATE OF robot_id,task_id ON allocation_jobs BEGIN '
                       "SELECT RAISE(ABORT,'simulation allocation identity is immutable'); END")
            db.execute('CREATE TRIGGER sim_preserve_active_update BEFORE UPDATE ON sim_active_robots BEGIN '
                       "SELECT RAISE(ABORT,'active ownership cannot be changed directly'); END")
            db.execute('CREATE TRIGGER sim_preserve_active_delete BEFORE DELETE ON sim_active_robots '
                       'WHEN NOT EXISTS (SELECT 1 FROM sim_handoff_transfers h WHERE h.source_task_id=OLD.task_id '
                       'AND h.robot_id=OLD.robot_id) BEGIN '
                       "SELECT RAISE(ABORT,'active ownership requires a recorded handoff'); END")
        # Adds the unmodified legacy occupancy, outbox and map metadata tables.
        super(SimHandoffStore, store).initialize()
        if store._idle_enabled:
            with store._write() as db:
                db.execute('CREATE TABLE sim_idle_robots (robot_id TEXT PRIMARY KEY NOT NULL, '
                           'node TEXT UNIQUE NOT NULL)')
                db.executemany('INSERT INTO sim_idle_robots VALUES (?,?)', idle_positions.items())
                db.execute('CREATE TRIGGER sim_idle_blocks_occupancy BEFORE INSERT ON allocation_occupancy '
                           "WHEN NEW.kind='node' AND EXISTS (SELECT 1 FROM sim_idle_robots WHERE node=NEW.resource_id) "
                           "BEGIN SELECT RAISE(ABORT,'idle simulated robot occupies node'); END")
                db.execute('CREATE TRIGGER sim_idle_blocks_occupancy_update BEFORE UPDATE OF kind,resource_id ON allocation_occupancy '
                           "WHEN NEW.kind='node' AND EXISTS (SELECT 1 FROM sim_idle_robots WHERE node=NEW.resource_id) "
                           "BEGIN SELECT RAISE(ABORT,'idle simulated robot occupies node'); END")
        return store

    @property
    def creation_id(self):
        """Persist separately from this DB; never derive a restart identity from it."""
        return self._creation_id

    @property
    def reopened(self):
        return self._reopened

    @classmethod
    def open_existing(cls, path, *, expected_creation_id, expected_idle_enabled,
                      simulation_enabled=False):
        """Bind an existing isolated DB without creating, resetting, or migrating it.

        A separately persisted creation identity is mandatory. Reopening is not
        authorization to publish, move, release reservations, or assume a pose.
        """
        if simulation_enabled is not True or type(expected_idle_enabled) is not bool:
            raise ValueError('explicit isolated simulation and idle mode required')
        if not isinstance(expected_creation_id, str) or not re.fullmatch('[0-9a-f]{64}', expected_creation_id):
            raise ValueError('independent simulation creation identity required')
        name = os.fspath(path)
        if not isinstance(name, str) or not name or name == ':memory:':
            raise ValueError('existing file-backed simulation database required')
        try:
            if os.path.islink(name) or not os.path.isfile(name):
                raise ValueError('existing regular simulation database required')
            stat = os.stat(name)
        except OSError as exc:
            raise ValueError('existing simulation database unavailable') from exc
        store = cls(name, _key=_CONSTRUCTION_KEY)
        store._idle_enabled = expected_idle_enabled
        store._field_expected_robots = None
        store._field_initial_positions = None
        store._reopened = True
        store._created_path = os.path.normcase(os.path.realpath(name))
        store._creation_id = expected_creation_id
        store._creation_file = (stat.st_dev, stat.st_ino)
        store.validate_bound(name, check_schema=True)
        return store

    def bind_field_robots(self, robot_ids, initial_positions=None):
        """Bind the expected isolated fleet from configuration, never from DB rows."""
        if not self._idle_enabled or not isinstance(robot_ids, (set, frozenset, tuple, list)) or not robot_ids:
            raise ValueError('field simulation requires an explicit idle fleet')
        expected = frozenset(robot_ids)
        if len(expected) != len(robot_ids):
            raise ValueError('field simulation robot identities must be unique')
        for robot in expected:
            _topic(robot)
            if not robot.startswith('sim-') or not robot[4:].strip():
                raise ValueError('field fleet is restricted to simulated robots')
        if self._field_expected_robots is not None and self._field_expected_robots != expected:
            raise ValueError('field simulation fleet differs from its creation configuration')
        if initial_positions is not None:
            if not isinstance(initial_positions, dict) or set(initial_positions) != expected:
                raise ValueError('field simulation start positions differ from configured fleet')
            for node in initial_positions.values():
                _id(node, 'field initial node')
            if len(set(initial_positions.values())) != len(initial_positions):
                raise ValueError('field initial nodes must be distinct')
            if (self._field_initial_positions is not None
                    and self._field_initial_positions != initial_positions):
                raise ValueError('field simulation start positions differ from creation configuration')
            self._field_initial_positions = copy.deepcopy(initial_positions)
        if self._field_initial_positions is None:
            raise ValueError('field restart requires independently configured initial positions')
        self._field_expected_robots = expected

    def _verify_field_fleet(self, db):
        """Require every configured virtual AGV to be idle XOR active."""
        expected = self._field_expected_robots
        if not self._idle_enabled or not expected:
            raise AllocationConflict('configured field fleet is unavailable')
        idle = db.execute('SELECT robot_id,node FROM sim_idle_robots').fetchall()
        active = db.execute('SELECT a.robot_id,a.task_id,j.robot_id,t.status '
                            'FROM sim_active_robots a LEFT JOIN allocation_jobs j ON j.task_id=a.task_id '
                            'LEFT JOIN tasks t ON t.task_id=a.task_id').fetchall()
        idle_ids = {row[0] for row in idle}
        active_ids = {row[0] for row in active}
        idle_nodes = {row[1] for row in idle}
        active_nodes = {row[0] for row in db.execute(
            "SELECT resource_id FROM allocation_occupancy WHERE kind='node'").fetchall()}
        if (len(idle_ids) != len(idle) or len(active_ids) != len(active) or idle_ids & active_ids
                or idle_ids | active_ids != expected
                or idle_nodes & active_nodes
                or self._field_initial_positions is None
                or any(self._field_initial_positions.get(robot) != node for robot, node in idle)
                or any(robot != job_robot or status != 'ASSIGNED' for robot, _task, job_robot, status in active)):
            raise AllocationConflict('configured field robot ownership is inconsistent')

    def validate_bound(self, path, *, check_schema=False):
        """Fail closed if the exclusively created simulation DB was replaced."""
        if (os.path.normcase(os.path.realpath(os.fspath(path))) != self._created_path
                or os.path.normcase(os.path.realpath(self.path)) != self._created_path):
            raise ValueError('simulation store path differs from its fresh database')
        try:
            info = os.stat(self.path)
            if (info.st_dev, info.st_ino) != self._creation_file:
                raise ValueError('simulation store file was replaced')
            uri = Path(self.path).resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True)) as db:
                if self._reopened and check_schema:
                    if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                        raise ValueError('simulation database integrity check failed')
                    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                        raise ValueError('simulation database foreign keys are invalid')
                    schema = db.execute("SELECT type,name,sql FROM sqlite_master WHERE "
                        "(type='trigger' AND name LIKE 'sim_%') OR (type='table' AND name IN "
                        "('tasks','sim_store_identity','allocation_jobs','sim_active_robots',"
                        "'sim_handoff_transfers','allocation_occupancy','allocation_outbox',"
                        "'allocation_meta','sim_idle_robots')) ORDER BY type,name").fetchall()
                    digest = hashlib.sha256(json.dumps(schema, separators=(',', ':')).encode()).hexdigest()
                    if digest != _SCHEMA_HASHES[self._idle_enabled]:
                        raise ValueError('simulation database schema or protection changed')
                values = db.execute('SELECT value FROM sim_store_identity').fetchall()
                trigger = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name='sim_activate_robot'").fetchone()
                if self._idle_enabled:
                    triggers = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name IN "
                                          "('sim_idle_blocks_occupancy','sim_idle_blocks_occupancy_update')").fetchall()
                    if len(triggers) != 2:
                        raise ValueError('simulation idle protection is invalid')
            if values != [(self._creation_id,)] or trigger != ('sim_activate_robot',):
                raise ValueError('simulation database identity is invalid')
            info = os.stat(self.path)
            if (info.st_dev, info.st_ino) != self._creation_file:
                raise ValueError('simulation store file was replaced')
        except (OSError, sqlite3.Error) as exc:
            raise ValueError('fresh simulation database is unavailable') from exc

    def allocate(self, task_id, robot_id, map_id, nodes, released_until, resources, action_at_end, *, map_fingerprint,
                 release_index=None, field_context=None):
        _topic(robot_id)
        if self._idle_enabled:
            self.validate_bound(self.path)
            _fingerprint(map_fingerprint)
            nodes, resources, action_at_end = copy.deepcopy((nodes, resources, action_at_end))
            checked, payload = _input(task_id, robot_id, map_id, nodes, released_until, resources, action_at_end,
                                      release_index=release_index)
            if self._field_required and field_context is None:
                raise AllocationConflict('field route provenance is required')
            field_json = None
            if field_context is not None:
                field_context = copy.deepcopy(field_context)
                field_json = _field_route(field_context, map_id, nodes, map_fingerprint)
                validate_field_robot_order(json.loads(payload))
                self._field_required = True
                segment = plan_occurrence_segment(field_context['map_data'], nodes, 0)
                if (segment.released_until != released_until or set(segment.resources) != checked
                        or (release_index is not None and release_index != segment.end_index)):
                    raise InvalidAllocation('field first segment differs from route')
            try:
                with self._write() as db:
                    if field_json is not None or self._field_required:
                        self._verify_field_fleet(db)
                    _verify_field_global(db, field_required=self._field_required)
                    if db.execute('SELECT status FROM tasks WHERE task_id=?', (task_id,)).fetchone() != ('PENDING',):
                        raise AllocationConflict('task is missing or not pending')
                    idle = db.execute('SELECT node FROM sim_idle_robots WHERE robot_id=?', (robot_id,)).fetchone()
                    if idle != (nodes[0],):
                        raise AllocationConflict('robot must own the first node as its idle position')
                    active = db.execute("SELECT value FROM allocation_meta WHERE key='active_map'").fetchone()
                    fingerprint = db.execute("SELECT value FROM allocation_meta WHERE key='map_fingerprint'").fetchone()
                    has_jobs = db.execute('SELECT 1 FROM allocation_jobs LIMIT 1').fetchone() is not None
                    if active is None and fingerprint is None and not has_jobs:
                        db.execute('INSERT INTO allocation_meta(key,value) VALUES (?,?)', ('active_map', map_id))
                        db.execute('INSERT INTO allocation_meta(key,value) VALUES (?,?)', ('map_fingerprint', map_fingerprint))
                    elif active != (map_id,) or fingerprint != (map_fingerprint,):
                        raise AllocationConflict('map or fingerprint differs from the persistent active map')
                    mode = db.execute("SELECT value FROM allocation_meta WHERE key='field_route_mode'").fetchone()
                    if field_json is not None:
                        if mode is None and not has_jobs:
                            db.execute("INSERT INTO allocation_meta VALUES ('field_route_mode','required')")
                        elif mode != ('required',):
                            raise AllocationConflict('cannot mix field and legacy route provenance')
                    elif mode is not None:
                        raise AllocationConflict('field route provenance is required')
                    if db.execute('SELECT 1 FROM sim_active_robots WHERE robot_id=?', (robot_id,)).fetchone():
                        raise AllocationConflict('robot is already active')
                    self._check_resource_availability(db, checked, ignore_idle_robot=robot_id)
                    if db.execute('DELETE FROM sim_idle_robots WHERE robot_id=? AND node=?',
                                  (robot_id, nodes[0])).rowcount != 1:
                        raise AllocationConflict('idle ownership changed')
                    db.execute('INSERT INTO allocation_jobs VALUES (?,?,?,?,?)',
                               (task_id, robot_id, map_id, json.dumps(nodes, ensure_ascii=False), released_until))
                    if release_index is not None:
                        db.execute('INSERT INTO occurrence_allocations VALUES (?,?)', (task_id, release_index))
                    if field_json is not None:
                        db.execute('INSERT INTO field_route_provenance VALUES (?,?,?,?)',
                                   (task_id, field_json, field_context['incoming_edge'], field_context['goal']))
                    db.executemany('INSERT INTO allocation_occupancy VALUES (?,?,?)',
                                   [(kind, key, task_id) for kind, key in sorted(checked)])
                    db.execute('INSERT INTO allocation_outbox VALUES (?,?,?)', (task_id, payload, 'pending'))
                    db.execute("UPDATE tasks SET status='ASSIGNED' WHERE task_id=?", (task_id,))
                    _verify_field_global(db, field_required=self._field_required)
                    if field_json is not None or self._field_required:
                        self._verify_field_fleet(db)
            except sqlite3.IntegrityError as exc:
                raise AllocationConflict('robot or resource already allocated') from exc
            return self.get_allocation(task_id)
        return super().allocate(task_id, robot_id, map_id, nodes, released_until, resources,
                                action_at_end, map_fingerprint=map_fingerprint,
                                release_index=release_index, field_context=field_context)

    def _check_resource_availability(self, db, resources, *, ignore_task=None, ignore_idle_robot=None):
        """Check under the caller's write transaction; only known occupancy is retryable."""
        for kind, resource in resources:
            owner = db.execute('SELECT task_id FROM allocation_occupancy WHERE kind=? AND resource_id=?',
                               (kind, resource)).fetchone()
            if owner is not None and owner[0] != ignore_task:
                raise SimResourceBusy('simulation segment resource is occupied')
            if self._idle_enabled and kind == 'node':
                idle = db.execute('SELECT robot_id FROM sim_idle_robots WHERE node=?', (resource,)).fetchone()
                if idle is not None and idle[0] != ignore_idle_robot:
                    raise SimResourceBusy('simulation idle node is occupied')

    def list_occupancy(self):
        if not self._idle_enabled:
            return super().list_occupancy()
        self.validate_bound(self.path)
        with closing(self._connect()) as db:
            db.execute('BEGIN')
            rows = db.execute('SELECT o.kind,o.resource_id,j.robot_id FROM allocation_occupancy o '
                              'JOIN allocation_jobs j ON j.task_id=o.task_id').fetchall()
            rows += [('node', node, robot) for robot, node in db.execute('SELECT robot_id,node FROM sim_idle_robots')]
        return {(kind, resource_id): robot for kind, resource_id, robot in rows}

    def handoff_allocate(self, *, task_id, robot_id, map_id, nodes, released_until,
                         resources, action_at_end, map_fingerprint, source_task_id,
                         robot_session, seq, stopped_at, event_id, action_ack,
                         expected_twin_id, gate, progress, receiver, simulation_enabled=False,
                         release_index=None, field_context=None, reroute_guard=None):
        """Atomically transfer the source stop into an entirely reserved new leg.

        action_ack is a caller-verified Unreal simulation acknowledgement. This
        routine verifies its correlation, not its origin or physical truth.
        The existing receiver is serialized inside the operation.
        """
        if simulation_enabled is not True:
            raise ValueError('explicit isolated simulation is required')
        self.validate_bound(self.path)
        for name, value in (('source_task_id', source_task_id), ('robot_session', robot_session),
                            ('stopped_at', stopped_at), ('event_id', event_id),
                            ('expected_twin_id', expected_twin_id)):
            _id(value, name)
        if source_task_id == task_id or type(seq) is not int or seq < 1:
            raise ValueError('invalid source task or sequence')
        _topic(robot_id)
        _fingerprint(map_fingerprint)
        nodes, resources, action_at_end, action_ack = copy.deepcopy((nodes, resources, action_at_end, action_ack))
        checked, payload = _input(task_id, robot_id, map_id, nodes, released_until, resources, action_at_end,
                                  release_index=release_index)
        validate_order(robot_id, json.loads(payload))
        field_json = None
        if field_context is not None:
            field_context = copy.deepcopy(field_context)
            field_json = _field_route(field_context, map_id, nodes, map_fingerprint)
            validate_field_robot_order(json.loads(payload))
        if progress._field_required != (field_context is not None):
            raise ValueError('handoff and progress field modes differ')
        ack_keys = {'order_id', 'action_id', 'agv_id', 'action', 'applied'}
        if (not isinstance(action_ack, dict) or set(action_ack) != ack_keys
                or action_ack['order_id'] != source_task_id or action_ack['agv_id'] != expected_twin_id
                or action_ack['action'] not in ('grab', 'place') or action_ack['applied'] is not True):
            raise ValueError('verified simulated action acknowledgement required')
        _id(action_ack['action_id'], 'action_id')
        if (progress._gate is not gate or progress._enabled is not True
                or os.path.normcase(os.path.realpath(progress.store.path)) != os.path.normcase(os.path.realpath(self.path))
                or progress._map_id != map_id or progress._fingerprint != map_fingerprint):
            raise ValueError('progress, recovery and map must refer to this simulation')
        if (not isinstance(receiver, SimEventReceiver) or receiver.progress is not progress
                or receiver.gate is not gate
                or os.path.normcase(os.path.realpath(receiver.inbox.path)) != os.path.normcase(os.path.realpath(self.path))
                or receiver.robot_sessions.get(robot_id, {}).get(source_task_id) != robot_session
                or receiver.robot_sessions.get(robot_id, {}).get(task_id) != robot_session):
            raise ValueError('receiver must bind both legs to the same simulated robot session')
        segment = (plan_occurrence_segment(progress._map, nodes, 0) if field_json is not None
                   else plan_segment(progress._map, nodes))
        if (segment.released_until != released_until or set(segment.resources) != checked or nodes[0] != stopped_at
                or (release_index is not None and release_index != segment.end_index)):
            raise ValueError('target leg must reserve its exact first segment from the stopped node')
        try:
            with _reroute_handoff_fence(reroute_guard, source_task_id, self, progress, receiver, gate) as reroute_robot, \
                    receiver._dispatch_lock, gate.transaction() as db:
                if not receiver.ready:
                    raise RuntimeError('simulation receiver is not ready')
                progress._check_database(db)
                if progress._field_required:
                    self._verify_field_fleet(db)
                _verify_field_global(db, field_required=progress._field_required)
                prior_robot, prior_path, prior_stop = progress._job(db, source_task_id, robot_id)
                if reroute_guard is None:
                    gate.require_reconciled(db, source_task_id, robot_session,
                                            expected_seq=seq, expected_stopped_at=stopped_at)
                if field_context is not None and reroute_guard is None:
                    row = db.execute('SELECT incoming_edge FROM field_route_provenance WHERE task_id=?',
                                     (source_task_id,)).fetchone()
                    if row is None:
                        raise AllocationConflict('source direction provenance is missing')
                    source_route = plan_field_route(progress._map, map_ref=map_id,
                                                    incoming_edge=row[0], goal=prior_path[-1])
                    if (tuple(prior_path) != source_route.nodes or not source_route.edge_ids
                            or field_context['incoming_edge'] != source_route.edge_ids[-1]):
                        raise AllocationConflict('handoff incoming direction differs from finished route')
                latest_seq, _prior_payload, prior_state, _order = progress._latest(
                    db, source_task_id, prior_robot, prior_path, prior_stop)
                if reroute_guard is None:
                    if (prior_robot != robot_id or prior_stop != stopped_at or prior_path[-1] != stopped_at
                            or latest_seq != seq or prior_state != 'acked'):
                        raise AllocationConflict('source leg is not finished at its acknowledged destination')
                else:
                    from server.sim_reroute_execute_wire import _execution
                    _execution_payload, generation = _execution(db, reroute_guard, source_task_id,
                                                                 reroute_robot, state='finished')
                    from server.sim_reroute_multi_progress import validate_steps
                    steps = validate_steps(db, progress._map, generation)
                    if (field_context is None or prior_robot != robot_id or prior_state != 'acked'
                            or generation['robot_session'] != robot_session
                            or generation['parent_seq'] != latest_seq or seq != latest_seq + 1 + len(steps)
                            or generation['nodes'][-1] != stopped_at
                            or not generation['edge_ids'] or field_context['incoming_edge'] != generation['edge_ids'][-1]):
                        raise AllocationConflict('completed reroute does not match the next leg')
                    finish = db.execute('SELECT robot_id,event_id FROM sim_reroute_finishes WHERE task_id=?',
                                        (source_task_id,)).fetchone()
                    if finish != (robot_id, event_id):
                        raise AllocationConflict('completed reroute event differs')
                required_action = {'load': 'grab', 'unload': 'place'}.get(_order['action_at_end'].get('type'))
                if required_action is None or action_ack['action'] != required_action:
                    raise AllocationConflict('source leg and acknowledged action do not match')
                if progress._occupancy(db, source_task_id) != {('node', stopped_at)}:
                    raise AllocationConflict('source must retain exactly its final node')
                if db.execute('SELECT task_id FROM sim_active_robots WHERE robot_id=?', (robot_id,)).fetchone() != (source_task_id,):
                    raise AllocationConflict('source is not the active owner')
                if db.execute('SELECT status FROM tasks WHERE task_id=?', (task_id,)).fetchone() != ('PENDING',):
                    raise AllocationConflict('target task must be pending')
                row = db.execute('SELECT i.payload,i.digest,i.robot_session,i.task_id,i.seq,i.state,i.result, '
                                 'p.digest,p.task_id,p.seq,p.next_seq,p.result '
                                 'FROM sim_event_inbox i JOIN progress_events p '
                                 'ON p.robot_id=i.robot_id AND p.event_id=i.event_id '
                                 'WHERE i.robot_id=? AND i.event_id=?', (robot_id, event_id)).fetchone()
                if reroute_guard is not None:
                    # _execution has already revalidated the distinct final
                    # event, both generation records and its held destination.
                    row = None
                if row is None and reroute_guard is None:
                    raise AllocationConflict('applied final event not found')
                if row is not None:
                    event, canonical, digest = _event(row[0].encode('ascii'))
                    result = json.loads(row[6]) if row[6] else None
                    expected_cleared = set(progress._segment_for_task(db, source_task_id, prior_path, stopped_at).resources) - {('node', stopped_at)}
                    if (canonical != row[0] or digest != row[1] or digest != row[7]
                        or row[2:6] != (robot_session, source_task_id, seq, 'applied')
                        or row[8:12] != (source_task_id, seq, None, 'finished')
                        or event['event_id'] != event_id or event['robot_session'] != robot_session
                        or event['order_id'] != source_task_id or event['seq'] != seq
                        or event['map_id'] != map_id or event['stopped_at'] != stopped_at
                        or _resources(event['cleared_resources']) != expected_cleared
                        or not isinstance(result, dict) or result.get('status') != 'finished'
                            or result.get('task_id') != source_task_id or result.get('next_seq') is not None):
                        raise AllocationConflict('finished event and simulated proof do not match')
                if db.execute('SELECT 1 FROM sim_handoff_transfers WHERE source_task_id=? OR target_task_id=? OR action_id=?',
                              (source_task_id, task_id, action_ack['action_id'])).fetchone():
                    raise AllocationConflict('handoff or action was already used')
                self._check_resource_availability(db, checked, ignore_task=source_task_id)
                db.execute('INSERT INTO sim_handoff_transfers VALUES (?,?,?,?,?,?,?,?,?)',
                           (source_task_id, task_id, robot_id, robot_session, seq, stopped_at,
                            event_id, action_ack['action_id'], json.dumps(action_ack, sort_keys=True)))
                if reroute_guard is not None:
                    canonical_ack = json.dumps(action_ack, sort_keys=True, ensure_ascii=True,
                                               allow_nan=False, separators=(',', ':')).encode('ascii')
                    db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_handoff_proofs ('
                               'source_task_id TEXT PRIMARY KEY NOT NULL REFERENCES allocation_jobs(task_id), '
                               'expected_twin_id TEXT NOT NULL, action_id TEXT NOT NULL, ack_digest TEXT NOT NULL)')
                    db.execute('INSERT INTO sim_reroute_handoff_proofs VALUES (?,?,?,?)',
                               (source_task_id, expected_twin_id, action_ack['action_id'],
                                hashlib.sha256(canonical_ack).hexdigest()))
                if db.execute('DELETE FROM sim_active_robots WHERE robot_id=? AND task_id=?',
                              (robot_id, source_task_id)).rowcount != 1:
                    raise AllocationConflict('active ownership changed')
                db.execute('INSERT INTO allocation_jobs VALUES (?,?,?,?,?)',
                           (task_id, robot_id, map_id, json.dumps(nodes, ensure_ascii=False), released_until))
                if release_index is not None:
                    db.execute('INSERT INTO occurrence_allocations VALUES (?,?)', (task_id, release_index))
                if field_json is not None:
                    db.execute('INSERT INTO field_route_provenance VALUES (?,?,?,?)',
                               (task_id, field_json, field_context['incoming_edge'], field_context['goal']))
                if db.execute("UPDATE allocation_occupancy SET task_id=? WHERE task_id=? AND kind='node' AND resource_id=?",
                              (task_id, source_task_id, stopped_at)).rowcount != 1:
                    raise AllocationConflict('source stopped node changed')
                db.executemany('INSERT INTO allocation_occupancy VALUES (?,?,?)',
                               [(kind, resource, task_id) for kind, resource in sorted(checked - {('node', stopped_at)})])
                db.execute('INSERT INTO allocation_outbox VALUES (?,?,?)', (task_id, payload, 'pending'))
                if db.execute("UPDATE tasks SET status='ASSIGNED' WHERE task_id=? AND status='PENDING'", (task_id,)).rowcount != 1:
                    raise AllocationConflict('target task changed')
                if db.execute("UPDATE tasks SET status='DONE' WHERE task_id=? AND status='ASSIGNED'", (source_task_id,)).rowcount != 1:
                    raise AllocationConflict('source task changed')
                db.execute("UPDATE recovery_challenges SET status='revoked' WHERE task_id=?", (source_task_id,))
                _verify_field_global(db, field_required=progress._field_required)
                if progress._field_required:
                    self._verify_field_fleet(db)
        except sqlite3.IntegrityError as exc:
            raise AllocationConflict('robot, action or segment is already allocated') from exc
        return self.get_allocation(task_id)


class SimHandoffCoordinatorStore:
    """One-shot adapter for Coordinator; only its first allocation is a handoff."""

    def __init__(self, store, *, target_task_id, source_task_id, robot_id, robot_session,
                 seq, stopped_at, event_id, action_ack, expected_twin_id, gate, progress,
                 receiver, simulation_enabled=False, reroute_guard=None):
        if simulation_enabled is not True or not isinstance(store, SimHandoffStore):
            raise ValueError('fresh isolated simulation store and explicit enablement required')
        _id(target_task_id, 'target_task_id')
        _topic(robot_id)
        self.path = store.path
        self._store = store
        self._target = target_task_id
        self._robot = robot_id
        self._context = {'source_task_id': source_task_id, 'robot_session': robot_session,
                         'seq': seq, 'stopped_at': stopped_at, 'event_id': event_id,
                         'action_ack': copy.deepcopy(action_ack), 'expected_twin_id': expected_twin_id,
                         'gate': gate, 'progress': progress, 'receiver': receiver,
                         'simulation_enabled': True, 'reroute_guard': reroute_guard}
        self._lock = threading.Lock()
        self._used = False

    def allocate(self, task_id, robot_id, map_id, nodes, released_until, resources, action_at_end, *, map_fingerprint,
                 release_index=None, field_context=None):
        with self._lock:
            if self._used:
                raise AllocationConflict('simulation handoff adapter has already been used')
            self._used = True
        if task_id != self._target or robot_id != self._robot:
            raise AllocationConflict('handoff target or robot does not match')
        try:
            return self._store.handoff_allocate(task_id=task_id, robot_id=robot_id, map_id=map_id,
                        nodes=nodes, released_until=released_until, resources=resources,
                        action_at_end=action_at_end, map_fingerprint=map_fingerprint,
                        release_index=release_index, field_context=field_context, **self._context)
        except SimResourceBusy:
            # This exception is raised inside the write transaction before
            # mutation and only after ownership and proof checks. Its rollback
            # is definite; every other failure consumes this adapter.
            with self._lock:
                self._used = False
            raise

    def get_allocation(self, task_id):
        return self._store.get_allocation(task_id)

    def get_order(self, task_id):
        return self._store.get_order(task_id)

    def mark_attempted(self, *args, **kwargs):
        return self._store.mark_attempted(*args, **kwargs)

    def mark_broker_acked(self, *args, **kwargs):
        return self._store.mark_broker_acked(*args, **kwargs)
