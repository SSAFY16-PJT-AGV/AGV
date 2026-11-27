"""Reserve old and proposed simulated segments atomically during a replan."""
from contextlib import ExitStack, closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from server.segments import plan_segment
from server.allocation_store import AllocationConflict, _verify_field_global
from server.field_map_planner import plan_field_route
from server.sim_handoff_store import SimHandoffStore
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_runtime import SimRuntime
from server.tests.test_sim_reroute_mqtt import cycle_map, read_tables
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class RerouteReservationTest(unittest.TestCase):
    def test_prepare_claims_both_segments_and_rejects_duplicate_or_lost_connection(self):
        for handoff in (False, True):
            with self.subTest(handoff_store=handoff):
                self._exercise(handoff)

    def _exercise(self, handoff, after_prepare=None):
        data, port = cycle_map(), free_port()
        data['schema'] = 'agv-map-v1'
        for edge in (data['edges'][3], data['edges'][4]):
            edge['from'], edge['to'] = edge['to'], edge['from']
            edge['shape'].reverse()
        data['nodes'].extend([{'id': 'P1', 'pos': [-100, 0]}, {'id': 'P2', 'pos': [200, 200]}])
        data['edges'].extend([
            {'id': 'IN1', 'from': 'P1', 'to': 'S1', 'shape': [[-100, 0], [0, 0]], 'bidirectional': True, 'lanes': 1},
            {'id': 'IN2', 'from': 'S2', 'to': 'P2', 'shape': [[200, 100], [200, 200]], 'bidirectional': True, 'lanes': 1}])
        env = isolated_environment(os.environ)
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='sim-reroute-hold-') as folder, ExitStack() as cleanup:
            folder = Path(folder)
            log = cleanup.enter_context((folder / 'broker.log').open('w+', encoding='utf-8'))
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', BROKER,
                str(port), str(folder / 'ready')], env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(folder / 'ready'))
            with patch.dict(os.environ, isolated_environment(os.environ), clear=True):
                path = folder / 'sim.sqlite3'
                positions = {'sim-r1': 'S1', 'sim-r2': 'S2'}
                store = (SimHandoffStore.create_new(path, simulation_enabled=True, idle_positions=positions)
                         if handoff else None)
                runtime = SimRuntime(path, data, {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                                     port=port, client_id='reroute-reserve', field_simulation=True,
                                     reroute_transitions=True, simulation_store=store,
                                     field_initial_positions=positions if handoff else None)
                runtime.start()
                cleanup.callback(runtime.stop)
                robots = {}
                for rid, start, edge in [('sim-r1', 'S1', 'IN1'), ('sim-r2', 'S2', 'IN2~r')]:
                    inbox = SimOrderInbox(folder / (rid + '.sqlite3'), rid, 'wait-test@v1')
                    inbox.initialize()
                    robot = SimMqttOrderRobot(SimOrderExecutor(inbox, data, start, experimental_field_map=True,
                        field_incoming_edge=edge), port, authorization_guard=SimOrderAuthorizer(runtime, robot_id=rid).guard)
                    robot.start()
                    cleanup.callback(robot.stop)
                    robots[rid] = robot
                runtime.bind_reroute_simulators(robots)

                def wait(predicate):
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if predicate():
                            return
                        time.sleep(.02)
                    self.fail('timed out waiting for simulation')

                for task, rid, node, edge, goal in [('T1', 'sim-r1', 'S1', 'IN1', 'D1'),
                                                    ('T2', 'sim-r2', 'S2', 'IN2~r', 'D2')]:
                    runtime.create_task(task)
                    runtime.prepare_field(task, goal, [{'robot_id': rid, 'node': node,
                        'incoming_edge': edge, 'available': True}], {'type': 'none'})
                    runtime.publish(task)
                    wait(lambda: not robots[rid]._queue.empty())
                for task, rid, session in [('T1', 'sim-r1', 'boot-1'), ('T2', 'sim-r2', 'boot-2')]:
                    state = robots[rid].tick(10)
                    order, receipt = state['order'], state['receipt']
                    challenge = runtime.begin(task, session)['challenge']
                    runtime.complete(task, session, challenge, rid, task, receipt['seq'],
                                     'wait-test@v1', state['current_node'], receipt['digest'], True)
                    first = set(plan_segment(data, order['nodes']).resources)
                    following = set(plan_segment(data, order['nodes'][1:]).resources)
                    event = {'type': 'rear_clear', 'event_id': 'event-' + task, 'robot_session': session,
                        'order_id': task, 'seq': order['seq'], 'map_id': 'wait-test@v1',
                        'stopped_at': state['current_node'],
                        'cleared_resources': [list(x) for x in sorted(first - following)]}
                    robots[rid].publish_event(event)
                wait(lambda: len(runtime.receiver._occupied_events) == 2)
                previous = runtime.store.list_occupancy()
                self.assertEqual(runtime.reroute_candidate('T1')['nodes'], ['W1', 'X', 'Y', 'D1'])
                # Fail after the new occupancy rows and journal row were
                # inserted: the gate transaction must roll both back.
                from server.sim_reroute_transition import reserve
                def fail_after_insert(*args):
                    reserve(*args)
                    raise RuntimeError('injected failure after reservation')
                before = read_tables(path)
                with patch('server.sim_reroute_transition.reserve', side_effect=fail_after_insert):
                    self.assertEqual(runtime.prepare_reroute('T1')['status'], 'unknown')
                self.assertEqual(read_tables(path), before)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_generations').fetchone(), (0,))
                self.assertEqual(runtime.prepare_reroute('T1')['state'], 'prepared')
                with closing(sqlite3.connect(path)) as db:
                    held = set(db.execute("SELECT kind,resource_id FROM allocation_occupancy WHERE task_id='T1'"))
                    transition = db.execute("SELECT state FROM sim_reroute_generations WHERE task_id='T1'").fetchone()
                self.assertEqual(transition, ('prepared',))
                old = {resource for resource, owner in previous.items() if owner == 'sim-r1'}
                self.assertTrue(old <= held)
                self.assertTrue(set(plan_segment(data, ['W1', 'X', 'Y', 'D1']).resources) <= held)
                with closing(sqlite3.connect(path, isolation_level=None)) as db:
                    db.execute('BEGIN IMMEDIATE')
                    # Corruption of either the old or new hold blocks all
                    # other field allocations, and rollback restores it.
                    extra = sorted(held - old)[0]
                    db.execute('DELETE FROM allocation_occupancy WHERE task_id=? AND kind=? AND resource_id=?',
                               ('T1', *extra))
                    with self.assertRaises(AllocationConflict):
                        _verify_field_global(db, field_required=True)
                    db.execute('ROLLBACK')
                if not handoff:
                    runtime.store.create_task('T3')
                    route = plan_field_route(data, map_ref='wait-test@v1', incoming_edge='E5', goal='D1')
                    segment = plan_segment(data, list(route.nodes))
                    with self.assertRaises(AllocationConflict):
                        runtime.store.allocate('T3', 'sim-r3', 'wait-test@v1', list(route.nodes),
                            segment.released_until, segment.resources, {'type': 'none'},
                            map_fingerprint=runtime.progress._fingerprint,
                            field_context={'map_data': data, 'incoming_edge': 'E5', 'goal': 'D1'})
                    self.assertIsNone(runtime.store.get_allocation('T3'))
                self.assertEqual(robots['sim-r1'].executor.snapshot()['status'], 'stopped')
                self.assertEqual(runtime.prepare_reroute('T1')['status'], 'unknown')
                self.assertEqual(runtime.receiver.retry_occupied_event('sim-r1', 'event-T1')['status'], 'occupied')
                import json
                payload = json.dumps(robots['sim-r1'].executor.snapshot()['order']).encode('utf-8')
                with self.assertRaises(ValueError):
                    with SimOrderAuthorizer(runtime, robot_id='sim-r1').guard(payload):
                        pass
                if after_prepare is not None:
                    after_prepare(runtime, robots)
                saved = runtime.store.list_occupancy()
                runtime.stop()
                reopened = (SimHandoffStore.open_existing(path, expected_creation_id=store.creation_id,
                    expected_idle_enabled=True, simulation_enabled=True) if handoff else None)
                restarted = SimRuntime(path, data, {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                    port=port, client_id='reroute-reserve-2', field_simulation=True,
                    simulation_store=reopened, field_initial_positions=positions if handoff else None)
                restarted.start()
                cleanup.callback(restarted.stop)
                self.assertEqual(restarted.store.list_occupancy(), saved)
                with self.assertRaises(RuntimeError):
                    restarted.prepare_reroute('T1')
                with self.assertRaises(ValueError):
                    with SimOrderAuthorizer(restarted, robot_id='sim-r1').guard(payload):
                        pass
                stop(broker)
                wait(lambda: not restarted.status()['ready'])
                self.assertEqual(restarted.store.list_occupancy(), saved)


if __name__ == '__main__':
    unittest.main()
