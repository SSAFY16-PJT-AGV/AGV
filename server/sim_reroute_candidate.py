"""Read-only, simulation-only route proposal; it never transfers reservations."""
from server.allocation_store import _verify_field_global
from server.field_map_planner import NoFieldRouteError, plan_field_route
from server.segments import plan_segment
from server.sim_handoff_store import SimHandoffStore
from server.sim_wait_graph import _LIMIT, _validated_wait
from server.sim_order_authorization import SimOrderAuthorizer
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor


def _result(status, reason):
    return {'status': status, 'reason': reason, 'scope': 'isolated_simulation',
            'control_effect': 'none', 'nodes': [], 'edge_ids': [], 'incoming_edge': None}


def _incoming(data, previous, stopped):
    matches = []
    node = next(item for item in data['nodes'] if item['id'] == stopped)
    for edge in data['edges']:
        if edge['from'] == previous and edge['to'] == stopped:
            matches.append(edge['id'])
        elif edge['to'] == previous and edge['from'] == stopped and edge.get('bidirectional') is True and edge.get('directed') is not True:
            matches.append(edge['id'] if node.get('kind') == 'spur_end' or node.get('type') == 'destination'
                           else edge['id'] + '~r')
    if len(matches) != 1:
        raise ValueError('incoming directed edge is ambiguous')
    return matches[0]


def _owned_simulator(runtime, robot_id, robot):
    guard = getattr(robot, '_authorization_guard', None)
    authorizer = getattr(guard, '__self__', None)
    return (type(robot) is SimMqttOrderRobot and type(robot.executor) is SimOrderExecutor
            and robot.robot_id == robot_id and robot.port == runtime.port
            and type(authorizer) is SimOrderAuthorizer
            and getattr(guard, '__func__', None) is SimOrderAuthorizer.guard
            and authorizer.runtime is runtime and authorizer.robot_id == robot_id)


def propose_reroute(runtime, task_id, *, prepare=False):
    if type(prepare) is not bool or (prepare and not runtime._reroute_transitions):
        raise ValueError('explicit simulated reroute transition required')
    if not isinstance(task_id, str) or not task_id or len(task_id.encode('utf-8')) > 256:
        return _result('unknown', 'invalid_task')
    with runtime._runtime_read_operation():
        bindings = runtime._reroute_simulators
        robot_id = runtime._tasks.get(task_id, (None,))[0]
        robot = bindings.get(robot_id) if bindings is not None else None
        if robot is None:
            return _result('unknown', 'direction_checkpoint_unavailable')
        # Normal simulated ticks take robot._lock before runtime._lock. Keep
        # that same order while pinning the stopped executor and DB snapshot.
        with robot._lock:
            if (not _owned_simulator(runtime, robot_id, robot) or not robot.ready
                    or robot._event_uncertain or not robot._queue.empty()):
                return _result('unknown', 'direction_checkpoint_unavailable')
            client = robot._client
            return _propose_locked(runtime, task_id, robot, client, bindings, prepare=prepare)


def _propose_locked(runtime, task_id, robot, client, bindings, *, prepare=False):
        with runtime._lock:
            if runtime._reroute_simulators is not bindings:
                return _result('unknown', 'direction_checkpoint_unavailable')
            receiver = runtime.receiver
            if (receiver is None or runtime._reconnecting or runtime._reconnect_cleanup_failed
                    or not receiver.ready):
                return _result('unknown', 'receiver_unavailable')
            with receiver._dispatch_lock:
                if receiver is not runtime.receiver or not receiver.ready:
                    return _result('unknown', 'receiver_unavailable')
                with runtime.inbox._lock:
                    try:
                        with runtime.progress._transaction() as db:
                            progress = runtime.progress
                            _verify_field_global(db, field_required=progress._field_required)
                            if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                                raise ValueError('orphaned reservation data')
                            rows = db.execute('SELECT robot_id,event_id,payload,digest,task_id,robot_session,seq '
                                              "FROM sim_event_inbox WHERE state='pending' "
                                              'ORDER BY robot_id,event_id LIMIT ?', (_LIMIT + 1,)).fetchall()
                            if len(rows) > _LIMIT or len(receiver._occupied_events) > _LIMIT:
                                return _result('unknown', 'observation_limit')
                            if {(row[0], row[1]) for row in rows} != set(receiver._occupied_events):
                                return _result('unknown', 'unclassified_pending')
                            waits = [_validated_wait(runtime, db, *row) for row in rows]
                            wait_tasks = {wait['task_id'] for wait in waits}
                            if len(wait_tasks) != len(waits) or task_id not in wait_tasks:
                                return _result('unknown', 'task_wait_not_observed')
                            # A missing or moving task's reservations cannot be treated as
                            # a complete snapshot. Limit proposals to fully observed waits.
                            active = db.execute("SELECT j.task_id,j.robot_id FROM allocation_jobs j "
                                                "JOIN tasks t ON t.task_id=j.task_id WHERE t.status='ASSIGNED'").fetchall()
                            if {row[0] for row in active} != wait_tasks:
                                return _result('unknown', 'fleet_not_fully_observed')
                            occupancy = db.execute('SELECT task_id,kind,resource_id FROM allocation_occupancy').fetchall()
                            if len(occupancy) > 16384 or any(owner not in wait_tasks for owner, _, _ in occupancy):
                                raise ValueError('unobserved reservation owner')
                            for other_task, other_robot in active:
                                owner, other_path, other_stop = progress._job(db, other_task, other_robot)
                                if len(set(other_path)) != len(other_path):
                                    return _result('unknown', 'repeated_or_oversized_route')
                                current = progress._segment_for_task(db, other_task, other_path, other_stop)
                                if owner != other_robot or progress._occupancy(db, other_task) != set(current.resources):
                                    raise ValueError('other reservation does not match its path')
                            blocked = {(kind, resource) for owner, kind, resource in occupancy if owner != task_id}
                            if type(runtime.store) is SimHandoffStore and runtime.store._idle_enabled:
                                idle = db.execute('SELECT robot_id,node FROM sim_idle_robots').fetchall()
                                active_robots = {row[1] for row in active}
                                if ({row[0] for row in idle} | active_robots != set(runtime._sessions)
                                        or {row[0] for row in idle} & active_robots
                                        or len({row[1] for row in idle}) != len(idle)):
                                    raise ValueError('idle fleet not completely mapped')
                                blocked.update(('node', node) for _, node in idle)
                            wait = next(item for item in waits if item['task_id'] == task_id)
                            _, path, stopped = progress._job(db, task_id, wait['robot_id'])
                            if len(path) > 4096 or len(set(path)) != len(path):
                                return _result('unknown', 'repeated_or_oversized_route')
                            index = path.index(stopped)
                            if index == 0:
                                return _result('unknown', 'incoming_direction_unavailable')
                            incoming = _incoming(progress._map, path[index - 1], stopped)
                            # The executor checkpoint is simulation data and is
                            # never a physical position or orientation proof.
                            virtual_robot = bindings[wait['robot_id']]
                            if (virtual_robot is not robot or virtual_robot._client is not client
                                    or not _owned_simulator(runtime, wait['robot_id'], virtual_robot)
                                    or virtual_robot.executor._field_map != progress._map
                                    or not virtual_robot.ready or virtual_robot._event_uncertain
                                    or not virtual_robot._queue.empty()
                                    or virtual_robot.executor.field_stop_direction() != (stopped, incoming)):
                                return _result('unknown', 'direction_checkpoint_mismatch')
                            candidate = plan_field_route(progress._map, map_ref=progress._map_id,
                                incoming_edge=incoming, goal=path[-1], blocked_resources=blocked)
                            if (len(candidate.nodes) < 2 or len(candidate.nodes) > 256
                                    or len(set(candidate.nodes)) != len(candidate.nodes)):
                                return _result('unknown', 'repeated_or_oversized_candidate')
                            offset = 0
                            while offset < len(candidate.nodes) - 1:
                                segment = plan_segment(progress._map, candidate.nodes[offset:])
                                if set(segment.resources) & blocked:
                                    raise ValueError('candidate intersects a held resource')
                                offset += len(segment.nodes) - 1
                            if (virtual_robot._client is not client or not virtual_robot.ready
                                    or not _owned_simulator(runtime, wait['robot_id'], virtual_robot)
                                    or virtual_robot._event_uncertain or not virtual_robot._queue.empty()
                                    or virtual_robot.executor.field_stop_direction() != (stopped, incoming)
                                    or receiver is not runtime.receiver or not receiver.ready):
                                return _result('unknown', 'direction_checkpoint_mismatch')
                            if prepare:
                                from server.sim_reroute_transition import reserve
                                result = reserve(db, runtime, task_id, wait, path, incoming, candidate,
                                                 blocked, progress._occupancy(db, task_id))
                                _verify_field_global(db, field_required=progress._field_required)
                                return result
                            return {'status': 'candidate', 'reason': None, 'scope': 'isolated_simulation',
                                    'control_effect': 'none', 'nodes': list(candidate.nodes),
                                    'edge_ids': list(candidate.edge_ids), 'incoming_edge': incoming,
                                    'direction_source': 'simulated_route_and_executor_checkpoint',
                                    'map_ref': candidate.map_ref, 'distance_mm': candidate.distance_mm,
                                    'reservation': 'not_reserved'}
                    except NoFieldRouteError:
                        return _result('no_candidate', 'no_route_avoiding_snapshot')
                    except Exception:
                        return _result('unknown', 'evidence_unavailable')
