"""Actual virtual workflows automatically resolve one shared-segment wait."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from server.order_pickup_workflow import PickupWorkflow
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_reroute_automatic import SimRerouteAutoSession
from server.sim_runtime import SimRuntime
from server.tests.test_sim_reroute_mqtt import cycle_map
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class AutomaticRerouteWorkflowTest(unittest.TestCase):
    def test_two_real_virtual_workflows_finish_after_automatic_reroute(self):
        self._exercise('success')

    def test_lost_execute_outcome_stops_without_republishing_or_releasing_holds(self):
        self._exercise('lost_execute')

    def test_lost_clearance_outcome_stops_without_republishing_or_releasing_holds(self):
        self._exercise('lost_event')

    def test_stop_waits_for_inflight_prepare_and_prevents_later_publish(self):
        self._exercise('stop_race')

    def _exercise(self, mode):
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
        with tempfile.TemporaryDirectory(prefix='auto-reroute-') as folder, ExitStack() as cleanup:
            folder = Path(folder)
            log = cleanup.enter_context((folder / 'broker.log').open('w+', encoding='utf-8'))
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', BROKER,
                str(port), str(folder / 'ready')], env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, str(folder / 'ready'))
            with patch.dict(os.environ, isolated_environment(os.environ), clear=True):
                runtime = SimRuntime(folder / 'sim.sqlite3', data,
                    {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                    port=port, client_id='auto-reroute', field_simulation=True, reroute_transitions=True)
                runtime.start(); cleanup.callback(runtime.stop)
                robots = {}
                for rid, start, edge in [('sim-r1', 'S1', 'IN1'), ('sim-r2', 'S2', 'IN2~r')]:
                    inbox = SimOrderInbox(folder / (rid + '.sqlite3'), rid, 'wait-test@v1'); inbox.initialize()
                    robot = SimMqttOrderRobot(SimOrderExecutor(inbox, data, start, experimental_field_map=True,
                        field_incoming_edge=edge), port, authorization_guard=SimOrderAuthorizer(runtime, robot_id=rid).guard)
                    robot.start(); cleanup.callback(robot.stop); robots[rid] = robot
                runtime.bind_reroute_simulators(robots)
                reroute = SimRerouteAutoSession(runtime, robots['sim-r1'], 'boot-1', key=bytes(range(32)),
                                                simulation_enabled=True).start()
                cleanup.callback(reroute.stop)
                first = PickupWorkflow(runtime, robots['sim-r1'], data, {'p': {'pickup_node': 'D1'}},
                    simulation_enabled=True, task_id='T1', session='boot-1', robot_id='sim-r1',
                    action_at_end={'type': 'none'}, wait_for_reservation=True, reroute_session=reroute)
                second = PickupWorkflow(runtime, robots['sim-r2'], data, {'p': {'pickup_node': 'D2'}},
                    simulation_enabled=True, task_id='T2', session='boot-2', robot_id='sim-r2',
                    action_at_end={'type': 'none'}, wait_for_reservation=True)
                attempts = []
                if mode == 'lost_execute':
                    from server.sim_reroute_automatic import publish_execution
                    def lost_execute(*args):
                        attempts.append('execute')
                        publish_execution(*args)
                        raise TimeoutError('injected lost execute outcome')
                    cleanup.enter_context(patch('server.sim_reroute_automatic.publish_execution', side_effect=lost_execute))
                elif mode == 'lost_event':
                    publish_event = robots['sim-r1'].publish_event
                    def lost_event(event):
                        if event['event_id'].startswith('reroute-'):
                            attempts.append('event')
                            publish_event(event)
                            raise TimeoutError('injected lost clearance outcome')
                        return publish_event(event)
                    cleanup.enter_context(patch.object(robots['sim-r1'], 'publish_event', side_effect=lost_event))
                elif mode == 'stop_race':
                    from server.sim_reroute_automatic import publish_proposal
                    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
                    actual_prepare = runtime.prepare_reroute
                    def blocked_prepare(task):
                        result = actual_prepare(task)
                        entered.set()
                        if not release.wait(10):
                            raise RuntimeError('test did not release prepare')
                        return result
                    def check_publish(*args):
                        attempts.append(('proposal', stopped.is_set()))
                        return publish_proposal(*args)
                    cleanup.enter_context(patch.object(runtime, 'prepare_reroute', side_effect=blocked_prepare))
                    cleanup.enter_context(patch('server.sim_reroute_automatic.publish_proposal', side_effect=check_publish))
                self.assertNotEqual(first.submit('p')['status'], 'unknown')
                self.assertNotEqual(second.submit('p')['status'], 'unknown')
                if mode == 'stop_race':
                    finished = threading.Event()
                    def drive():
                        while not finished.is_set():
                            first.tick(.5); second.tick(.5)
                            time.sleep(.02)
                    worker = threading.Thread(target=drive)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(15), 'prepare did not enter barrier')
                        def shutdown():
                            reroute.stop()
                            stopped.set()
                        closer = threading.Thread(target=shutdown)
                        closer.start()
                        self.assertFalse(stopped.wait(.2), 'stop returned before prepare finished')
                        release.set()
                        closer.join(10)
                        self.assertFalse(closer.is_alive(), 'stop and prepare deadlocked')
                        self.assertTrue(stopped.is_set())
                        self.assertEqual(attempts, [('proposal', False)])
                        with self.assertRaises(RuntimeError):
                            reroute.step(.1)
                        with self.assertRaises(RuntimeError):
                            reroute.begin('T1')
                        self.assertEqual(attempts, [('proposal', False)])
                        self.assertTrue(any(owner == 'sim-r1' and item[0] == 'edge'
                                            for item, owner in runtime.store.list_occupancy().items()))
                    finally:
                        release.set()
                        finished.set()
                        worker.join(10)
                    self.assertFalse(worker.is_alive())
                    return
                deadline = time.monotonic() + 30
                states = None
                while time.monotonic() < deadline:
                    states = first.tick(.5), second.tick(.5)
                    if states[0]['status'] == 'unknown' and mode != 'success':
                        break
                    if any(item['status'] == 'unknown' for item in states):
                        self.fail('workflow became unknown: {}, phase={}'.format(states, reroute._phase))
                    if all(item['status'] == 'pickup_reached' for item in states):
                        break
                    time.sleep(.02)
                if mode != 'success':
                    self.assertEqual(states[0]['status'], 'unknown')
                    self.assertEqual(attempts, ['execute' if mode == 'lost_execute' else 'event'])
                    held = runtime.store.list_occupancy()
                    self.assertTrue(any(owner == 'sim-r1' and item[0] == 'edge' for item, owner in held.items()))
                    for _ in range(3):
                        self.assertEqual(first.tick(.5)['status'], 'unknown')
                    self.assertEqual(runtime.store.list_occupancy(), held)
                    self.assertEqual(len(attempts), 1)
                    reroute.stop()
                    runtime.stop()
                    reopened = SimRuntime(folder / 'sim.sqlite3', data,
                        {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                        port=port, client_id='auto-reroute-restart', field_simulation=True)
                    reopened.start(); cleanup.callback(reopened.stop)
                    self.assertEqual(reopened.store.list_occupancy(), held)
                    with self.assertRaises(RuntimeError):
                        reopened.prepare_reroute('T1')
                    return
                self.assertEqual([item['status'] for item in states], ['pickup_reached', 'pickup_reached'])
                self.assertEqual([item['current_node'] for item in states], ['D1', 'D2'])
                self.assertEqual(reroute._phase, 'finished')
                self.assertEqual(runtime.inbox.get('sim-r1', states[0]['finished_evidence']['event_id'])['state'], 'applied')
                self.assertEqual(runtime.inbox.get('sim-r2', states[1]['finished_evidence']['event_id'])['result']['status'], 'finished')


if __name__ == '__main__':
    unittest.main()
