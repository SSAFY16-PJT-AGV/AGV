"""One virtual reroute may hand off only using the live completed generation."""
import unittest
import time
import threading
import sqlite3
from contextlib import closing
from unittest.mock import patch

from server.field_map_planner import plan_field_route
from server.allocation_store import AllocationConflict, _verify_field_global
from server.segments import plan_segment
from server.sim_handoff_store import SimHandoffStore, SimHandoffCoordinatorStore, _reroute_handoff_fence
from server.sim_reroute_finish import finish_reroute
from server.sim_runtime import SimRuntime
from server.tests import test_sim_reroute_finish_wire as fixture
from server.tests import test_sim_reroute_reservation as reservation
from server import sim_reroute_activate_wire
from server import sim_reroute_execute_wire


class Done(Exception):
    pass


class RerouteHandoffWireTest(unittest.TestCase):
    def test_guarded_handoff_checks_action_and_transfers_stop_atomically(self):
        original_prepare = SimRuntime.prepare_field
        original_init = SimRuntime.__init__
        original_exercise = fixture.RerouteReservationTest._exercise
        original_map = reservation.cycle_map

        def map_with_delivery():
            result = original_map()
            result['nodes'].append({'id': 'K', 'pos': [300, -100]})
            result['edges'].append({'id': 'E8', 'from': 'D1', 'to': 'K',
                'shape': [[300, 0], [300, -100]], 'bidirectional': True, 'lanes': 1})
            result['zones'].append({'id': 'wait-K', 'type': 'waiting', 'node': 'K'})
            return result

        def initialize(runtime, path, map_data, sessions, **kwargs):
            sessions = {robot: dict(tasks) for robot, tasks in sessions.items()}
            sessions['sim-r1']['T3'] = 'boot-1'
            return original_init(runtime, path, map_data, sessions, **kwargs)

        def prepare(runtime, task, destination, candidates, action, **kwargs):
            if task == 'T1':
                action = {'type': 'load'}
            return original_prepare(runtime, task, destination, candidates, action, **kwargs)

        def exercise(test, handoff, after_prepare=None):
            if handoff:
                return original_exercise(test, True, after_prepare=after_prepare)

        def finish_and_handoff(runtime, task, event, guard):
            result = finish_reroute(runtime, task, event, guard)
            if not isinstance(runtime.store, SimHandoffStore):
                return result
            runtime.create_task('T3')
            robot = guard.robot_receiver.robot
            _, incoming = robot.executor.field_stop_direction()
            route = plan_field_route(runtime.progress._map, map_ref='wait-test@v1',
                                     incoming_edge=incoming, goal='K')
            nodes = list(route.nodes)
            segment = plan_segment(runtime.progress._map, nodes)
            kwargs = dict(task_id='T3', robot_id='sim-r1', map_id='wait-test@v1', nodes=nodes,
                released_until=segment.released_until, resources=segment.resources,
                action_at_end={'type': 'unload'}, map_fingerprint=runtime.progress._fingerprint,
                source_task_id='T1', robot_session='boot-1', seq=2, stopped_at='D1',
                event_id=event, action_ack={'order_id': 'T1', 'action_id': 'test-action',
                    'agv_id': 'AGV_01', 'action': 'grab', 'applied': True}, expected_twin_id='AGV_01',
                gate=runtime.gate, progress=runtime.progress, receiver=runtime.receiver,
                simulation_enabled=True, field_context={'map_data': runtime.progress._map,
                    'incoming_edge': incoming, 'goal': 'K'}, reroute_guard=guard)
            bad = dict(kwargs, action_ack=dict(kwargs['action_ack'], action='place'))
            before = runtime.store.list_occupancy()
            with self.assertRaises(ValueError):
                runtime.store.handoff_allocate(**bad)
            with self.assertRaises(ValueError):
                runtime.store.handoff_allocate(**dict(kwargs, event_id='old-parent-event'))
            with self.assertRaises(ValueError):
                runtime.store.handoff_allocate(**dict(kwargs, reroute_guard=None))
            guard._execution_receiver.endpoint._ready.clear()
            try:
                with self.assertRaises(ValueError):
                    runtime.store.handoff_allocate(**kwargs)
            finally:
                guard._execution_receiver.endpoint._ready.set()
            self.assertEqual(runtime.store.list_occupancy(), before)
            endpoint_entered, probe_entered = threading.Event(), threading.Event()
            fence_errors = []
            original_binding = sim_reroute_activate_wire._check_binding
            def observe_probe(*args):
                result = original_binding(*args)
                if threading.current_thread().name == 'handoff-probe':
                    probe_entered.set()
                return result
            def duplicate_worker():
                try:
                    with guard._execution_receiver.endpoint._connection_fence:
                        endpoint_entered.set()
                        if not probe_entered.wait(5):
                            raise RuntimeError('handoff did not enter outer fence')
                        with robot._lock, runtime._lock:
                            pass
                except Exception as exc:
                    fence_errors.append(exc)
            def probe_handoff():
                try:
                    with _reroute_handoff_fence(guard, 'T1', runtime.store, runtime.progress,
                                                 runtime.receiver, runtime.gate):
                        pass
                except Exception as exc:
                    fence_errors.append(exc)
            duplicate = threading.Thread(target=duplicate_worker, daemon=True)
            probe = threading.Thread(target=probe_handoff, name='handoff-probe', daemon=True)
            with patch.object(sim_reroute_activate_wire, '_check_binding', observe_probe):
                duplicate.start()
                self.assertTrue(endpoint_entered.wait(5))
                probe.start()
                duplicate.join(8)
                probe.join(8)
            self.assertFalse(duplicate.is_alive() or probe.is_alive(), 'duplicate and handoff deadlocked')
            self.assertEqual(fence_errors, [])
            adapter = SimHandoffCoordinatorStore(runtime.store, target_task_id='T3', source_task_id='T1',
                robot_id='sim-r1', robot_session='boot-1', seq=2, stopped_at='D1', event_id=event,
                action_ack=kwargs['action_ack'], expected_twin_id='AGV_01', gate=runtime.gate,
                progress=runtime.progress, receiver=runtime.receiver, simulation_enabled=True, reroute_guard=guard)
            adapter._context['reroute_guard'] = object()
            try:
                with self.assertRaises(ValueError):
                    runtime.prepare_field('T3', 'K', [{'robot_id': 'sim-r1', 'node': 'D1',
                        'incoming_edge': incoming, 'available': True}], {'type': 'unload'}, handoff_store=adapter)
            finally:
                adapter._context['reroute_guard'] = guard
            worker_entered, prepare_entered = threading.Event(), threading.Event()
            auth_entered, disconnect_attempted, disconnect_done = threading.Event(), threading.Event(), threading.Event()
            errors, results = [], []
            original_binding = sim_reroute_activate_wire._check_binding
            original_execution = sim_reroute_execute_wire._execution
            def observe_execution(*args, **kwargs):
                result = original_execution(*args, **kwargs)
                if threading.current_thread().name == 'handoff-prepare':
                    auth_entered.set()
                    if not disconnect_attempted.wait(5):
                        raise RuntimeError('disconnect did not race the handoff')
                    if disconnect_done.is_set():
                        raise RuntimeError('disconnect crossed the handoff fence')
                return result
            def observe_binding(*args):
                result = original_binding(*args)
                if threading.current_thread().name == 'handoff-prepare':
                    prepare_entered.set()
                return result
            def proposal_worker():
                try:
                    with guard.robot_receiver._lifecycle_lock:
                        worker_entered.set()
                        if not prepare_entered.wait(5):
                            raise RuntimeError('handoff did not enter outer fence')
                        with robot._lock, runtime._lock:
                            pass
                except Exception as exc:
                    errors.append(exc)
            def prepare_handoff():
                try:
                    results.append(runtime.prepare_field('T3', 'K', [{'robot_id': 'sim-r1', 'node': 'D1',
                        'incoming_edge': incoming, 'available': True}], {'type': 'unload'}, handoff_store=adapter))
                except Exception as exc:
                    errors.append(exc)
            def disconnect_endpoint():
                try:
                    if not auth_entered.wait(8):
                        raise RuntimeError('handoff did not reach authorization')
                    disconnect_attempted.set()
                    with guard._execution_receiver.endpoint._connection_fence:
                        guard._execution_receiver.endpoint._ready.clear()
                        disconnect_done.set()
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=proposal_worker, daemon=True)
            starter = threading.Thread(target=prepare_handoff, name='handoff-prepare', daemon=True)
            disconnector = threading.Thread(target=disconnect_endpoint, daemon=True)
            with patch.object(sim_reroute_activate_wire, '_check_binding', observe_binding), \
                    patch.object(sim_reroute_execute_wire, '_execution', observe_execution):
                worker.start()
                self.assertTrue(worker_entered.wait(5))
                disconnector.start()
                starter.start()
                worker.join(8)
                starter.join(8)
                disconnector.join(8)
            guard._execution_receiver.endpoint._ready.set()
            self.assertTrue(disconnect_done.is_set())
            self.assertFalse(worker.is_alive() or starter.is_alive() or disconnector.is_alive(),
                             'proposal, disconnect and handoff deadlocked')
            self.assertEqual(errors, [])
            allocation = results[0]
            self.assertEqual(allocation['task_id'], 'T3')
            self.assertEqual(runtime.store.list_occupancy()[('node', 'D1')], 'sim-r1')
            with closing(sqlite3.connect(runtime.path, isolation_level=None)) as db:
                corruptions = (
                    "UPDATE sim_handoff_transfers SET ack_json='{}' WHERE source_task_id='T1'",
                    "UPDATE sim_handoff_transfers SET ack_json=replace(ack_json,'AGV_01','AGV_FAKE') WHERE source_task_id='T1'",
                    "UPDATE sim_handoff_transfers SET action_id='wrong' WHERE source_task_id='T1'",
                    "UPDATE sim_reroute_handoff_proofs SET expected_twin_id='AGV_FAKE' WHERE source_task_id='T1'",
                    "UPDATE sim_reroute_handoff_proofs SET action_id='wrong' WHERE source_task_id='T1'",
                    "UPDATE sim_reroute_handoff_proofs SET ack_digest='wrong' WHERE source_task_id='T1'",
                    "UPDATE sim_handoff_transfers SET event_id='wrong' WHERE source_task_id='T1'",
                    "UPDATE sim_handoff_transfers SET robot_session='wrong' WHERE source_task_id='T1'",
                    "UPDATE sim_event_inbox SET result='{}' WHERE robot_id='sim-r1' AND event_id='reroute-T1'",
                    "UPDATE field_route_provenance SET incoming_edge='wrong' WHERE task_id='T3'",
                )
                for sql in corruptions:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute(sql)
                    with self.assertRaises(AllocationConflict, msg=sql):
                        _verify_field_global(db, field_required=True)
                    db.execute('ROLLBACK')
            runtime.publish('T3')
            deadline = time.monotonic() + 10
            while robot._queue.empty() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(robot._queue.empty())
            delivered = robot.tick(10)
            self.assertEqual((delivered['status'], delivered['current_node']), ('stopped', 'K'))
            self.assertEqual(delivered['receipt']['order_id'], 'T3')
            held = runtime.store.list_occupancy()
            path, port, map_data = runtime.path, runtime.port, runtime.map_data
            creation = runtime.store.creation_id
            runtime.stop()
            reopened = SimHandoffStore.open_existing(path, expected_creation_id=creation,
                expected_idle_enabled=True, simulation_enabled=True)
            restarted = SimRuntime(path, map_data, {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                port=port, client_id='reroute-handoff-restart', field_simulation=True,
                reroute_transitions=True, simulation_store=reopened,
                field_initial_positions={'sim-r1': 'S1', 'sim-r2': 'S2'})
            try:
                with self.assertRaisesRegex(RuntimeError, 'reroute handoff'):
                    restarted.start()
                self.assertEqual(reopened.list_occupancy(), held)
                with self.assertRaises((ValueError, RuntimeError)):
                    finish_reroute(restarted, 'T1', event, guard)
            finally:
                restarted.stop()
            raise Done()

        with patch.object(SimRuntime, '__init__', initialize), patch.object(SimRuntime, 'prepare_field', prepare), \
                patch.object(reservation, 'cycle_map', map_with_delivery), \
                patch.object(fixture.RerouteReservationTest, '_exercise', exercise), \
                patch.object(fixture, 'finish_reroute', side_effect=finish_and_handoff):
            with self.assertRaises(Done):
                fixture.RerouteFinishWireTest(
                    methodName='test_new_clearance_releases_union_and_unblocks_second_robot'
                ).test_new_clearance_releases_union_and_unblocks_second_robot()
