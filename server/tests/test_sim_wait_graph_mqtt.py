"""Two virtual robots in real loopback MQTT circular contention.

The reported graph is a bounded observation, never a physical-safety claim or
permission to replan, publish, release occupancy or infer an initial wait.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server.segments import plan_segment
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_runtime import SimRuntime
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


def cycle_map():
    points = {'S1': [0, 0], 'W1': [100, 0], 'W2': [200, 0],
              'D1': [300, 0], 'S2': [200, 100], 'D2': [100, -100],
              'X': [100, 200], 'Y': [300, 200]}
    pairs = [('S1', 'W1'), ('W1', 'W2'), ('W2', 'D1'), ('S2', 'W2'), ('W1', 'D2'),
             ('W1', 'X'), ('X', 'Y'), ('Y', 'D1')]
    return {'map_id': 'wait-test', 'version': 1, 'unit': 'mm',
            'nodes': [{'id': name, 'pos': value} for name, value in points.items()],
            'edges': [{'id': 'E' + str(i), 'from': a, 'to': b, 'shape': [points[a], points[b]],
                       'bidirectional': True, 'lanes': 1} for i, (a, b) in enumerate(pairs)],
            'zones': [{'id': 'wait-' + name, 'type': 'waiting', 'node': name}
                      for name in ('W1', 'W2', 'D1', 'D2')]}


def read_tables(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute('BEGIN')
        names = ('tasks', 'allocation_jobs', 'allocation_occupancy', 'allocation_outbox',
                 'progress_orders', 'progress_events', 'sim_event_inbox', 'recovery_challenges')
        return {name: db.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall() for name in names}


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class SimWaitGraphMqttTest(unittest.TestCase):
    def test_cycle_unclassified_disconnect_and_concurrent_queries_never_change_resources(self):
        data, port = cycle_map(), free_port()
        env = isolated_environment(os.environ)
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='sim-wait-graph-') as folder, ExitStack() as cleanup:
            folder = Path(folder)
            log = cleanup.enter_context((folder / 'broker.log').open('w+', encoding='utf-8'))
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c',
                BROKER, str(port), str(folder / 'ready')], env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(folder / 'ready'))
            with patch.dict(os.environ, isolated_environment(os.environ), clear=True):
                path = folder / 'simulation.sqlite3'
                runtime = SimRuntime(path, data, {'sim-r1': {'T1': 'boot-1'},
                                     'sim-r2': {'T2': 'boot-2'}}, port=port, client_id='wait-graph')
                runtime.start()
                cleanup.callback(runtime.stop)
                errors = []
                runtime.receiver.on_error = errors.append
                robots = {}
                for rid, start in [('sim-r1', 'S1'), ('sim-r2', 'S2')]:
                    inbox = SimOrderInbox(folder / (rid + '.sqlite3'), rid, 'wait-test@v1')
                    inbox.initialize()
                    robot = SimMqttOrderRobot(SimOrderExecutor(inbox, data, start), port,
                        authorization_guard=SimOrderAuthorizer(runtime, robot_id=rid).guard)
                    robot.start()
                    cleanup.callback(robot.stop)
                    robots[rid] = robot

                def wait(predicate, message):
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if predicate(): return
                        time.sleep(.02)
                    self.fail('timed out: ' + message)

                for task, robot_id, node, goal in [('T1', 'sim-r1', 'S1', 'D1'),
                                                    ('T2', 'sim-r2', 'S2', 'D2')]:
                    runtime.create_task(task)
                    allocation = runtime.prepare(task, goal, [{'robot_id': robot_id, 'node': node,
                                                  'available': True}], {'type': 'none'})
                    self.assertEqual(allocation['nodes'][1], 'W1' if task == 'T1' else 'W2')
                    runtime.publish(task)
                    wait(lambda: not robots[robot_id]._queue.empty(), task + ' command')

                payloads = {}
                for task, rid, session in [('T1', 'sim-r1', 'boot-1'), ('T2', 'sim-r2', 'boot-2')]:
                    state = robots[rid].tick(10)
                    self.assertEqual(state['status'], 'stopped')
                    receipt, order = state['receipt'], state['order']
                    challenge = runtime.begin(task, session)['challenge']
                    runtime.complete(task, session, challenge, rid, task, receipt['seq'],
                                     'wait-test@v1', state['current_node'], receipt['digest'], True)
                    path_nodes = order['nodes']
                    first = set(plan_segment(data, path_nodes).resources)
                    following = set(plan_segment(data, path_nodes[1:]).resources)
                    payloads[task] = {'type': 'rear_clear', 'event_id': 'event-' + task,
                        'robot_session': session, 'order_id': task, 'seq': order['seq'],
                        'map_id': 'wait-test@v1', 'stopped_at': state['current_node'],
                        'cleared_resources': [list(x) for x in sorted(first - following)]}

                robots['sim-r1'].publish_event(payloads['T1'])
                wait(lambda: ('sim-r1', 'event-T1') in runtime.receiver._occupied_events, 'first occupied event')
                self.assertTrue(hasattr(runtime, 'wait_graph'), 'read-only wait graph not implemented')
                first = runtime.wait_graph()
                self.assertEqual(first['status'], 'observed')
                self.assertEqual(first['cycle_candidates'], [])
                self.assertEqual(first['waiting'][0]['task_id'], 'T1')
                self.assertEqual({b['owner_task_id'] for b in first['waiting'][0]['blockers']}, {'T2'})

                robots['sim-r2'].publish_event(payloads['T2'])
                wait(lambda: len(runtime.receiver._occupied_events) == 2, 'two occupied events')
                before = read_tables(path)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    observations = list(pool.map(lambda _: runtime.wait_graph(), range(8)))
                for graph in observations:
                    self.assertEqual(graph['status'], 'cycle_candidate')
                    self.assertEqual(graph['cycle_candidates'], [['T1', 'T2']])
                    self.assertEqual(graph['initial_allocation_waits'], 'not_observed')
                    self.assertEqual(graph['control_effect'], 'none')
                self.assertEqual(read_tables(path), before)

                proposal = runtime.reroute_candidate('T1')
                self.assertEqual(proposal['status'], 'unknown')
                self.assertEqual(proposal['reason'], 'direction_checkpoint_unavailable')
                self.assertEqual(proposal['control_effect'], 'none')
                self.assertEqual(read_tables(path), before)

                # Corrupt evidence and an expired recovery proof must suppress
                # the entire candidate, without modifying persistent state.
                with closing(sqlite3.connect(path)) as db, db:
                    digest = db.execute("SELECT digest FROM sim_event_inbox WHERE event_id='event-T1'").fetchone()[0]
                    db.execute("UPDATE sim_event_inbox SET digest='corrupt' WHERE event_id='event-T1'")
                corrupted = read_tables(path)
                graph = runtime.wait_graph()
                self.assertEqual((graph['status'], graph['reason'], graph['cycle_candidates']),
                                 ('unknown', 'evidence_unavailable', []))
                self.assertEqual(runtime.reroute_candidate('T1')['status'], 'unknown')
                self.assertEqual(read_tables(path), corrupted)
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("UPDATE sim_event_inbox SET digest=? WHERE event_id='event-T1'", (digest,))
                    expiry = db.execute("SELECT expires FROM recovery_challenges WHERE task_id='T1'").fetchone()[0]
                    db.execute("UPDATE recovery_challenges SET expires=0 WHERE task_id='T1'")
                expired = read_tables(path)
                graph = runtime.wait_graph()
                self.assertEqual((graph['status'], graph['reason'], graph['cycle_candidates']),
                                 ('unknown', 'evidence_unavailable', []))
                self.assertEqual(read_tables(path), expired)
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("UPDATE recovery_challenges SET expires=? WHERE task_id='T1'", (expiry,))
                self.assertEqual(runtime.wait_graph()['status'], 'cycle_candidate')

                # A stored but never classified event is not evidence of a wait.
                unknown = copy.deepcopy(payloads['T1'])
                unknown['event_id'] = 'unclassified'
                runtime.inbox.store('sim-r1', unknown)
                before = read_tables(path)
                graph = runtime.wait_graph()
                self.assertEqual(graph['status'], 'unknown')
                self.assertEqual(graph['reason'], 'unclassified_pending')
                self.assertEqual(runtime.reroute_candidate('T1')['status'], 'unknown')
                self.assertEqual(graph['cycle_candidates'], [])
                self.assertEqual(read_tables(path), before)

                # Restore the test-injected row so that disconnect races are
                # independently exercised against classified waits.
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("DELETE FROM sim_event_inbox WHERE event_id='unclassified'")
                self.assertEqual(runtime.wait_graph()['status'], 'cycle_candidate')

                retained = runtime.store.list_occupancy()
                start = threading.Event()
                def observe_during_disconnect(_):
                    start.wait()
                    return runtime.wait_graph()
                with ThreadPoolExecutor(max_workers=8) as pool:
                    futures = [pool.submit(observe_during_disconnect, n) for n in range(8)]
                    start.set()
                    stop(broker)
                    raced = [future.result(timeout=10) for future in futures]
                for graph in raced:
                    self.assertIn(graph['status'], ('cycle_candidate', 'unknown'))
                    self.assertEqual(graph['control_effect'], 'none')
                wait(lambda: not runtime.status()['ready'], 'broker disconnect')
                graph = runtime.wait_graph()
                self.assertEqual(graph['status'], 'unknown')
                self.assertEqual(graph['reason'], 'receiver_unavailable')
                self.assertEqual(runtime.reroute_candidate('T1')['status'], 'unknown')
                self.assertEqual(graph['cycle_candidates'], [])
                self.assertEqual(runtime.store.list_occupancy(), retained)


if __name__ == '__main__':
    unittest.main()
