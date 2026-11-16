"""Fresh-database-only simulated leg handoff; never a physical drive permit.

The caller must provide a separately isolated broker and an independently
validated simulated Unreal acknowledgement. Neither this module, a token,
nor a simulated event proves a physical robot's state or message identity.
Existing allocation databases are never opened or migrated by create_new().
"""
import copy
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from contextlib import closing

from server.allocation_store import AllocationStore, AllocationConflict, InvalidAllocation, _fingerprint, _id, _input
from server.order_publisher import _topic, validate_order
from server.segments import plan_segment
from server.segment_progress import _resources
from sim.event_inbox import _event
from sim.event_receiver import SimEventReceiver


_CONSTRUCTION_KEY = object()


class SimHandoffStore(AllocationStore):
    """Store for a new, private simulation DB with durable per-robot ownership."""

    def __init__(self, path, *, _key=None):
        if _key is not _CONSTRUCTION_KEY:
            raise ValueError('use create_new for a fresh isolated simulation database')
        super().__init__(path)

    @classmethod
    def create_new(cls, path, *, simulation_enabled=False):
        if simulation_enabled is not True:
            raise ValueError('explicit isolated simulation is required')
        name = os.fspath(path)
        if not isinstance(name, str) or not name or name == ':memory:':
            raise ValueError('a new file-backed database path is required')
        descriptor = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        store = cls(name, _key=_CONSTRUCTION_KEY)
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
        return store

    def validate_bound(self, path):
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
                values = db.execute('SELECT value FROM sim_store_identity').fetchall()
                trigger = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name='sim_activate_robot'").fetchone()
            if values != [(self._creation_id,)] or trigger != ('sim_activate_robot',):
                raise ValueError('simulation database identity is invalid')
        except (OSError, sqlite3.Error) as exc:
            raise ValueError('fresh simulation database is unavailable') from exc

    def allocate(self, task_id, robot_id, map_id, nodes, released_until, resources, action_at_end, *, map_fingerprint):
        _topic(robot_id)
        return super().allocate(task_id, robot_id, map_id, nodes, released_until, resources,
                                action_at_end, map_fingerprint=map_fingerprint)

    def handoff_allocate(self, *, task_id, robot_id, map_id, nodes, released_until,
                         resources, action_at_end, map_fingerprint, source_task_id,
                         robot_session, seq, stopped_at, event_id, action_ack,
                         expected_twin_id, gate, progress, receiver, simulation_enabled=False):
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
        checked, payload = _input(task_id, robot_id, map_id, nodes, released_until, resources, action_at_end)
        validate_order(robot_id, json.loads(payload))
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
        segment = plan_segment(progress._map, nodes)
        if segment.released_until != released_until or set(segment.resources) != checked or nodes[0] != stopped_at:
            raise ValueError('target leg must reserve its exact first segment from the stopped node')
        try:
            with receiver._dispatch_lock, gate.transaction() as db:
                if not receiver.ready:
                    raise RuntimeError('simulation receiver is not ready')
                progress._check_database(db)
                gate.require_reconciled(db, source_task_id, robot_session,
                                        expected_seq=seq, expected_stopped_at=stopped_at)
                prior_robot, prior_path, prior_stop = progress._job(db, source_task_id, robot_id)
                latest_seq, _prior_payload, prior_state, _order = progress._latest(
                    db, source_task_id, prior_robot, prior_path, prior_stop)
                if (prior_robot != robot_id or prior_stop != stopped_at or prior_path[-1] != stopped_at
                        or latest_seq != seq or prior_state != 'acked'):
                    raise AllocationConflict('source leg is not finished at its acknowledged destination')
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
                if row is None:
                    raise AllocationConflict('applied final event not found')
                event, canonical, digest = _event(row[0].encode('ascii'))
                result = json.loads(row[6]) if row[6] else None
                expected_cleared = set(progress._segment(prior_path, stopped_at).resources) - {('node', stopped_at)}
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
                db.execute('INSERT INTO sim_handoff_transfers VALUES (?,?,?,?,?,?,?,?,?)',
                           (source_task_id, task_id, robot_id, robot_session, seq, stopped_at,
                            event_id, action_ack['action_id'], json.dumps(action_ack, sort_keys=True)))
                if db.execute('DELETE FROM sim_active_robots WHERE robot_id=? AND task_id=?',
                              (robot_id, source_task_id)).rowcount != 1:
                    raise AllocationConflict('active ownership changed')
                db.execute('INSERT INTO allocation_jobs VALUES (?,?,?,?,?)',
                           (task_id, robot_id, map_id, json.dumps(nodes, ensure_ascii=False), released_until))
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
        except sqlite3.IntegrityError as exc:
            raise AllocationConflict('robot, action or segment is already allocated') from exc
        return self.get_allocation(task_id)


class SimHandoffCoordinatorStore:
    """One-shot adapter for Coordinator; only its first allocation is a handoff."""

    def __init__(self, store, *, target_task_id, source_task_id, robot_id, robot_session,
                 seq, stopped_at, event_id, action_ack, expected_twin_id, gate, progress,
                 receiver, simulation_enabled=False):
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
                         'simulation_enabled': True}
        self._lock = threading.Lock()
        self._used = False

    def allocate(self, task_id, robot_id, map_id, nodes, released_until, resources, action_at_end, *, map_fingerprint):
        with self._lock:
            if self._used:
                raise AllocationConflict('simulation handoff adapter has already been used')
            self._used = True
        if task_id != self._target or robot_id != self._robot:
            raise AllocationConflict('handoff target or robot does not match')
        return self._store.handoff_allocate(task_id=task_id, robot_id=robot_id, map_id=map_id,
                    nodes=nodes, released_until=released_until, resources=resources,
                    action_at_end=action_at_end, map_fingerprint=map_fingerprint, **self._context)

    def get_allocation(self, task_id):
        return self._store.get_allocation(task_id)

    def get_order(self, task_id):
        return self._store.get_order(task_id)

    def mark_attempted(self, *args, **kwargs):
        return self._store.mark_attempted(*args, **kwargs)

    def mark_broker_acked(self, *args, **kwargs):
        return self._store.mark_broker_acked(*args, **kwargs)
