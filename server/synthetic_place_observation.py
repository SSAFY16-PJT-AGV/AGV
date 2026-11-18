"""One-point synthetic delivery observation; never an action authorization.

The grab ledger and the live allocation DB are observed in separate transactions.
No result here remains valid for a later write, publish, or physical operation.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3

from server.allocation_store import _stored_field_route, _verify_field_global
from server.field_map_planner import plan_field_route
from server.order_persistence import _text
from server.order_product_control import ProductControlEngine
from server.segment_progress import _resources
from server.sim_handoff_store import SimHandoffStore
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_runtime import SimRuntime
from server.synthetic_action_transaction import SyntheticActionSession, _assignment_json
from sim.event_inbox import _event
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import _decode


def _grab_observation(session, request_id):
    if not session._lock.acquire(blocking=False):
        raise RuntimeError('synthetic session is busy')
    try:
        if session._closed or session._blocked:
            raise RuntimeError('synthetic session is unavailable')
        pair = session._pair
        pair._validate_live()
        orders = (pair._path / 'orders.sqlite3').resolve().as_uri() + '?mode=ro'
        pickup = (pair._path / 'pickup-inventory.sqlite3').resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(orders, uri=True, timeout=0, isolation_level=None)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('ATTACH DATABASE ? AS pickup', (pickup,))
            db.execute('BEGIN')
            session._verify(db)
            row = db.execute('SELECT task_id,action_id,agv_id,state FROM main.synthetic_action_attempt '
                             'WHERE request_id=? AND action=?', (request_id, 'grab')).fetchone()
            saved = db.execute("SELECT product_id,result_json FROM main.orders WHERE request_id=? AND state='active'",
                               (request_id,)).fetchone()
            if row is None or row[3] != 'applied' or saved is None:
                raise ValueError('applied synthetic grab and active order required')
            assignment = _assignment_json(saved[1])
            products = {item['id']: item for item in json.loads(pair._catalog_json)}
            product = products.get(saved[0])
            if not isinstance(assignment, dict) or not isinstance(product, dict):
                raise ValueError('invalid synthetic order assignment')
            observed = (row[0], row[1], row[2], assignment.get('assigned_robot'),
                        assignment.get('shelf_id'), assignment.get('slot_index'),
                        assignment.get('reservation_id'), assignment.get('pickup_reservation_id'),
                        product.get('product_type'), product.get('pickup_node'), product.get('destination_node'))
            session._verify(db)
        pair._validate_live()
        return observed
    except sqlite3.Error as exc:
        raise ValueError('synthetic observation database unavailable') from exc
    finally:
        session._lock.release()


def _final_event(db, progress, robot, task, session, event_id, seq, route, stop):
    row = db.execute('SELECT i.payload,i.digest,i.robot_session,i.task_id,i.seq,i.state,i.result, '
                     'p.digest,p.task_id,p.seq,p.next_seq,p.result FROM sim_event_inbox i '
                     'JOIN progress_events p ON p.robot_id=i.robot_id AND p.event_id=i.event_id '
                     'WHERE i.robot_id=? AND i.event_id=?', (robot, event_id)).fetchone()
    if row is None:
        raise ValueError('applied synthetic final event missing')
    event, canonical, digest = _event(row[0].encode('ascii'))
    result = _assignment_json(row[6]) if row[6] else None
    cleared = set(progress._segment_for_task(db, task, route, stop).resources) - {('node', stop)}
    if (canonical != row[0] or digest != row[1] or row[1] != row[7]
            or row[2:6] != (session, task, seq, 'applied')
            or row[8:12] != (task, seq, None, 'finished')
            or event['event_id'] != event_id or event['robot_session'] != session
            or event['order_id'] != task or event['seq'] != seq
            or event['map_id'] != progress._map_id or event['stopped_at'] != stop
            or _resources(event['cleared_resources']) != cleared
            or not isinstance(result, dict) or result.get('status') != 'finished'
            or result.get('task_id') != task or result.get('next_seq') is not None):
        raise ValueError('synthetic final event differs from allocation')


def observe_waiting_place(engine, session, *, request_id, simulation=False):
    """Inspect live synthetic state; result expires immediately and permits nothing."""
    if simulation is not True or type(engine) is not ProductControlEngine or type(session) is not SyntheticActionSession:
        raise ValueError('explicit in-process synthetic engine and session required')
    _text(request_id, 'request ID', 128)
    runtime, robot = engine.runtime, engine.robot
    if (type(runtime) is not SimRuntime or type(robot) is not SimMqttOrderRobot
            or type(getattr(robot, 'executor', None)) is not SimOrderExecutor):
        raise ValueError('live isolated simulation runtime and robot required')
    executor, inbox = robot.executor, robot.executor._inbox
    # Keep the DB owner alive without preventing stop from fencing the runtime.
    with runtime._runtime_read_operation():
        with engine.lock:
            grab = _grab_observation(session, request_id)
            with robot._lock:
                # Pinning preserves ownership; do not hold the runtime lock
                # through SQLite I/O or prevent stop() from fencing first.
                with runtime._lock:
                    runtime._ready()
                    store, receiver, gate, progress = runtime.store, runtime.receiver, runtime.gate, runtime.progress
                    if (type(store) is not SimHandoffStore or runtime._simulation_store is not store
                            or progress.store is not store or progress._gate is not gate
                            or receiver.progress is not progress or receiver.gate is not gate
                            or session._pair._map_id != progress._map_id
                            or runtime._tasks.get(engine._pickup_task) != (engine.ROBOT, engine.SESSION)
                            or runtime._tasks.get(engine._delivery_task) != (engine.ROBOT, engine.SESSION)):
                        raise ValueError('synthetic runtime, store, mapping, or map differs')
                guard = robot._authorization_guard
                authorizer = getattr(guard, '__self__', None)
                if (type(authorizer) is not SimOrderAuthorizer or authorizer.runtime is not runtime
                        or authorizer.robot_id != engine.ROBOT
                        or getattr(guard, '__func__', None) is not SimOrderAuthorizer.guard
                        or not robot.ready or inbox.robot_id != engine.ROBOT):
                    raise ValueError('original authorized synthetic robot is unavailable')
                store.validate_bound(store.path)
                with receiver._dispatch_lock, gate.serialized(), executor._lock, inbox._lock:
                    runtime._ready()
                    current = engine.snapshot()
                    pending = current.get('pending_action')
                    if (engine._status != 'waiting' or engine._phase != 'waiting_place'
                            or engine._current_leg != 'delivery' or not isinstance(pending, dict)
                            or set(pending) != {'order_id', 'action_id', 'agv_id', 'action', 'shelf_id', 'slot_index'}
                            or pending['order_id'] != engine._delivery_task or pending['action'] != 'place'
                            or pending['agv_id'] != engine.TWIN or type(pending['action_id']) is not str
                            or re.fullmatch(r'[0-9a-f]{32}', pending['action_id']) is None
                            or re.fullmatch(r'[0-9a-f]{32}', grab[1]) is None
                            or pending['shelf_id'] != engine._shelf
                            or type(pending['slot_index']) is not int or pending['slot_index'] != engine._slot
                            or grab[0] != engine._pickup_task or grab[2] != engine.TWIN
                            or grab[3:6] != (engine.ROBOT, engine._shelf, engine._slot)
                            or grab[8:11] != (engine._product, engine._pickup, engine._destination)):
                        raise ValueError('live synthetic place or saved assignment differs')
                    evidence = engine._validate_finished(engine._delivery_evidence,
                                                         engine._delivery_task, engine._destination)
                    with gate.transaction() as db:
                        progress._check_database(db)
                        _verify_field_global(db, field_required=progress._field_required)
                        if progress._field_required:
                            store._verify_field_fleet(db)
                        target = engine._delivery_task
                        gate.require_reconciled(db, target, engine.SESSION,
                                                expected_seq=evidence['seq'], expected_stopped_at=engine._destination)
                        target_robot, route, stopped = progress._job(db, target, engine.ROBOT)
                        seq, payload, state, order = progress._latest(db, target, target_robot, route, stopped)
                        parsed, canonical, order_digest = _decode(payload.encode('utf-8'), engine.ROBOT, progress._map_id)
                        if (parsed != order or seq != evidence['seq'] or order_digest != evidence['digest']
                                or state != 'acked' or stopped != route[-1] or stopped != engine._destination
                                or order['released_until'] != stopped or order['action_at_end'].get('type') != 'unload'
                                or progress._occupancy(db, target) != {('node', stopped)}
                                or db.execute('SELECT task_id FROM sim_active_robots WHERE robot_id=?',
                                              (engine.ROBOT,)).fetchone() != (target,)):
                            raise ValueError('synthetic delivery is not the active terminal stop')
                        state_now = executor.snapshot()
                        if state_now.get('visit_index') != len(route) - 1 or state_now.get('released_index') != len(route) - 1:
                            raise ValueError('synthetic executor visit differs from final route occurrence')
                        _final_event(db, progress, engine.ROBOT, target, engine.SESSION,
                                     evidence['event_id'], seq, route, stopped)
                        transfer = db.execute('SELECT robot_id,robot_session,seq,stopped_at,event_id,action_id,ack_json '
                                              'FROM sim_handoff_transfers WHERE source_task_id=? AND target_task_id=?',
                                              (engine._pickup_task, target)).fetchone()
                        if (transfer is None or transfer[0:2] != (engine.ROBOT, engine.SESSION)
                                or transfer[5] != grab[1]):
                            raise ValueError('synthetic pickup to delivery handoff differs')
                        ack = _assignment_json(transfer[6])
                        if ack != {'order_id': engine._pickup_task, 'action_id': grab[1], 'agv_id': engine.TWIN,
                                   'action': 'grab', 'applied': True}:
                            raise ValueError('handoff grab claim differs from synthetic record')
                        source = db.execute('SELECT t.status,j.robot_id,j.map_id,j.nodes_json,j.released_until '
                                            'FROM tasks t JOIN allocation_jobs j ON j.task_id=t.task_id '
                                            'WHERE t.task_id=?', (engine._pickup_task,)).fetchone()
                        if (source is None or source[:3] != ('DONE', engine.ROBOT, progress._map_id)
                                or db.execute('SELECT 1 FROM allocation_occupancy WHERE task_id=?',
                                              (engine._pickup_task,)).fetchone()):
                            raise ValueError('finished pickup allocation differs')
                        source_route = json.loads(source[3])
                        if (not isinstance(source_route, list) or len(source_route) < 2
                                or source_route[-1] != source[4] or source[4] != transfer[3]
                                or route[0] != source[4]):
                            raise ValueError('pickup and delivery route continuity differs')
                        source_seq, _, source_state, source_order = progress._latest(
                            db, engine._pickup_task, engine.ROBOT, source_route, source[4])
                        if (source_seq != transfer[2] or source_state != 'acked'
                                or source_order['action_at_end'].get('type') != 'load'):
                            raise ValueError('pickup order history differs from handoff')
                        if progress._field_required:
                            prior = _stored_field_route(db, engine._pickup_task, progress._map_id,
                                                        source_route, progress._fingerprint)
                            next_route = _stored_field_route(db, target, progress._map_id, route, progress._fingerprint)
                            if prior is None or next_route is None:
                                raise ValueError('field route provenance missing')
                            computed = plan_field_route(progress._map, map_ref=progress._map_id,
                                                        incoming_edge=prior['incoming_edge'], goal=source_route[-1])
                            if (tuple(source_route) != computed.nodes or not computed.edge_ids
                                    or next_route['incoming_edge'] != computed.edge_ids[-1]):
                                raise ValueError('field route handoff direction differs')
                        _final_event(db, progress, engine.ROBOT, engine._pickup_task, engine.SESSION,
                                     transfer[4], source_seq, source_route, source[4])
                        uri = Path(inbox.path).resolve().as_uri() + '?mode=ro'
                        with closing(sqlite3.connect(uri, uri=True)) as robot_db:
                            identity = robot_db.execute('SELECT robot_id,map_id FROM sim_inbox_identity WHERE singleton=1').fetchone()
                            latest = robot_db.execute('SELECT order_id,seq,digest,payload,state FROM sim_inbox_orders '
                                                      'ORDER BY rowid DESC LIMIT 1').fetchone()
                        if identity != (engine.ROBOT, progress._map_id) or latest != (target, seq, order_digest, canonical, 'completed'):
                            raise ValueError('synthetic robot inbox differs from delivery')
                    runtime._ready()
                    if runtime.receiver is not receiver or not robot.ready:
                        raise RuntimeError('synthetic runtime or robot changed')
                store.validate_bound(store.path)
            if _grab_observation(session, request_id) != grab:
                raise ValueError('synthetic grab observation changed')
            store.validate_bound(store.path)
            with robot._lock, runtime._lock:
                runtime._ready()
                if runtime.receiver is not receiver or runtime.store is not store or not robot.ready:
                    raise RuntimeError('synthetic runtime or robot changed')
            return {'scope': 'isolated_simulation', 'kind': 'immediately_expiring_observation',
                    'control_effect': 'none', 'snapshot_consistency': 'separate_databases',
                    'causality': 'not_established', 'valid_for_later_action': False,
                    'pickup_task': engine._pickup_task,
                    'delivery_task': engine._delivery_task, 'action_id': pending['action_id']}
