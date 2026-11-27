"""Reserve a whole virtual reroute before moving across several segments."""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from server.allocation_store import AllocationConflict, _verify_field_global
from server.segments import plan_segment
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_runtime import SimRuntime
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


def resources(data, nodes):
    result, offset = set(), 0
    while offset < len(nodes) - 1:
        segment = plan_segment(data, nodes[offset:])
        result.update(segment.resources)
        offset = nodes.index(segment.released_until, offset + 1)
    return result


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class RerouteMultiReservationTest(unittest.TestCase):
    def test_v3_reserves_every_detour_segment_atomically(self):
        data = json.loads((Path(__file__).resolve().parents[1] / 'examples' /
                           'mat_ssafy_v3_simulated_waits.json').read_text(encoding='utf-8'))
        port = free_port(); env = isolated_environment(os.environ); env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='auto-reroute-multi-') as folder, ExitStack() as cleanup:
            folder = Path(folder); log = cleanup.enter_context((folder / 'broker.log').open('w+', encoding='utf-8'))
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', BROKER,
                str(port), str(folder / 'ready')], env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker); wait_for_owned_broker(self, broker, str(folder / 'ready'))
            with patch.dict(os.environ, isolated_environment(os.environ), clear=True):
                runtime = SimRuntime(folder / 'sim.sqlite3', data,
                    {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                    port=port, client_id='multi-v3', field_simulation=True, reroute_transitions=True)
                runtime.start(); cleanup.callback(runtime.stop)
                robots = {}
                for rid, start, edge in [('sim-r1', 'N01', 'E1~r'), ('sim-r2', 'D02', 'E_D02')]:
                    inbox = SimOrderInbox(folder / (rid + '.sqlite3'), rid, 'mat_ssafy_v3@v0'); inbox.initialize()
                    robot = SimMqttOrderRobot(SimOrderExecutor(inbox, data, start, experimental_field_map=True,
                        field_incoming_edge=edge), port, authorization_guard=SimOrderAuthorizer(runtime, robot_id=rid).guard)
                    robot.start(); cleanup.callback(robot.stop); robots[rid] = robot
                runtime.bind_reroute_simulators(robots)
                def wait(predicate):
                    until = time.monotonic() + 10
                    while time.monotonic() < until:
                        if predicate(): return
                        time.sleep(.02)
                    self.fail('timed out waiting for isolated MQTT')
                for task, rid, start, edge, goal in [('T1', 'sim-r1', 'N01', 'E1~r', 'N03'),
                                                     ('T2', 'sim-r2', 'D02', 'E_D02', 'N05')]:
                    runtime.create_task(task)
                    runtime.prepare_field(task, goal, [{'robot_id': rid, 'node': start, 'incoming_edge': edge,
                                                       'available': True}], {'type': 'none'})
                    runtime.publish(task); wait(lambda: not robots[rid]._queue.empty())
                for task, rid, session in [('T1', 'sim-r1', 'boot-1'), ('T2', 'sim-r2', 'boot-2')]:
                    state = robots[rid].tick(20); order, receipt = state['order'], state['receipt']
                    challenge = runtime.begin(task, session)['challenge']
                    runtime.complete(task, session, challenge, rid, task, receipt['seq'], 'mat_ssafy_v3@v0',
                                     state['current_node'], receipt['digest'], True)
                    first = set(plan_segment(data, order['nodes']).resources)
                    index = order['nodes'].index(state['current_node'])
                    following = set(plan_segment(data, order['nodes'][index:]).resources)
                    event = {'type': 'rear_clear', 'event_id': 'event-' + task, 'robot_session': session,
                             'order_id': task, 'seq': order['seq'], 'map_id': 'mat_ssafy_v3@v0',
                             'stopped_at': state['current_node'],
                             'cleared_resources': [list(item) for item in sorted(first - following)]}
                    robots[rid].publish_event(event)
                wait(lambda: len(runtime.receiver._occupied_events) == 2)
                old = runtime.store.list_occupancy()
                candidate = runtime.reroute_candidate('T1')
                self.assertEqual(candidate['nodes'], ['N07', 'N01', 'J02', 'J07', 'N03'])
                from server.sim_reroute_transition import reserve
                def fail_after_reserve(*args):
                    reserve(*args)
                    raise RuntimeError('failure after whole-route reservation')
                with patch('server.sim_reroute_transition.reserve', side_effect=fail_after_reserve):
                    self.assertEqual(runtime.prepare_reroute('T1')['status'], 'unknown')
                self.assertEqual(runtime.store.list_occupancy(), old)
                with closing(sqlite3.connect(runtime.path)) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_generations').fetchone(), (0,))
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_reroute_multiseg_routes').fetchone(), (0,))
                result = runtime.prepare_reroute('T1')
                self.assertEqual(result['state'], 'prepared')
                with closing(sqlite3.connect(runtime.path)) as db:
                    held = set(db.execute("SELECT kind,resource_id FROM allocation_occupancy WHERE task_id='T1'"))
                self.assertTrue(resources(data, candidate['nodes']) <= held)
                self.assertTrue({item for item, owner in old.items() if owner == 'sim-r1'} <= held)
                with closing(sqlite3.connect(runtime.path, isolation_level=None)) as db:
                    db.execute('BEGIN IMMEDIATE')
                    target = sorted(resources(data, candidate['nodes']) - set(plan_segment(data, candidate['nodes']).resources))[0]
                    db.execute('DELETE FROM allocation_occupancy WHERE task_id=? AND kind=? AND resource_id=?', ('T1', *target))
                    with self.assertRaises(AllocationConflict): _verify_field_global(db, field_required=True)
                    db.execute('ROLLBACK')
                saved = runtime.store.list_occupancy()
                runtime.stop()
                restarted = SimRuntime(folder / 'sim.sqlite3', data,
                    {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                    port=port, client_id='multi-v3-restarted', field_simulation=True)
                restarted.start(); cleanup.callback(restarted.stop)
                self.assertEqual(restarted.store.list_occupancy(), saved)
                with self.assertRaises(RuntimeError):
                    restarted.prepare_reroute('T1')


if __name__ == '__main__':
    unittest.main()
