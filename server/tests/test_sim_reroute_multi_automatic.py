"""Team v3 map copy, explicitly simulated starts and loopback MQTT."""
from contextlib import ExitStack, contextmanager
import json
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
from server.tests.test_twin_broker_integration import BROKER, free_port, stop, wait_for_owned_broker
from server.tools.twin_multi_test import isolated_environment
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class AutomaticMultiRerouteV3Test(unittest.TestCase):
    def test_two_virtual_robots_finish_on_team_map_with_simulated_starts(self):
        self._exercise('success')

    def test_next_command_outcome_loss_preserves_all_reservations_across_restart(self):
        self._exercise('lost_next')

    def test_intermediate_event_outcome_loss_preserves_all_reservations_across_restart(self):
        self._exercise('lost_event')

    def test_qos1_duplicate_before_next_command_is_safe_and_continues(self):
        self._exercise('duplicate')

    def _exercise(self, mode):
        source = Path(__file__).resolve().parents[1] / 'examples' / 'mat_ssafy_v3_simulated_waits.json'
        data = json.loads(source.read_text(encoding='utf-8'))
        port = free_port(); env = isolated_environment(os.environ); env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        with tempfile.TemporaryDirectory(prefix='auto-reroute-v3-') as folder, ExitStack() as cleanup:
            folder = Path(folder)
            log = cleanup.enter_context((folder / 'broker.log').open('w+', encoding='utf-8'))
            broker = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', BROKER,
                str(port), str(folder / 'ready')], env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker); wait_for_owned_broker(self, broker, str(folder / 'ready'))
            with patch.dict(os.environ, isolated_environment(os.environ), clear=True):
                runtime = SimRuntime(folder / 'sim.sqlite3', data,
                    {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                    port=port, client_id='auto-reroute-v3', field_simulation=True, reroute_transitions=True)
                runtime.start(); cleanup.callback(runtime.stop)
                robots = {}
                for rid, start, edge in [('sim-r1', 'N01', 'E1~r'), ('sim-r2', 'D02', 'E_D02')]:
                    inbox = SimOrderInbox(folder / (rid + '.sqlite3'), rid, 'mat_ssafy_v3@v0'); inbox.initialize()
                    robot = SimMqttOrderRobot(SimOrderExecutor(inbox, data, start, experimental_field_map=True,
                        field_incoming_edge=edge), port, authorization_guard=SimOrderAuthorizer(runtime, robot_id=rid).guard)
                    robot.start(); cleanup.callback(robot.stop); robots[rid] = robot
                runtime.bind_reroute_simulators(robots)
                reroute = SimRerouteAutoSession(runtime, robots['sim-r1'], 'boot-1', key=bytes(range(32)),
                                                simulation_enabled=True).start(); cleanup.callback(reroute.stop)
                attempts = []
                if mode == 'lost_next':
                    from server.sim_reroute_automatic import publish_next
                    def lost_next(*args):
                        attempts.append('next')
                        publish_next(*args)
                        raise TimeoutError('injected lost next-command outcome')
                    cleanup.enter_context(patch('server.sim_reroute_automatic.publish_next', side_effect=lost_next))
                elif mode == 'lost_event':
                    publish_event = robots['sim-r1'].publish_event
                    def lost_event(event):
                        if event['event_id'].startswith('reroute-') and event['seq'] == 3:
                            attempts.append('event')
                            publish_event(event)
                            raise TimeoutError('injected lost intermediate event outcome')
                        return publish_event(event)
                    cleanup.enter_context(patch.object(robots['sim-r1'], 'publish_event', side_effect=lost_event))
                elif mode == 'duplicate':
                    from server.sim_reroute_automatic import publish_next
                    from sim.reroute_execution import SimRerouteExecution
                    entered, release = threading.Event(), threading.Event()
                    attacks = []
                    original_accepting = SimRerouteExecution._accepting_next
                    @contextmanager
                    def pause_inflight(execution):
                        with original_accepting(execution):
                            if not entered.is_set():
                                entered.set()
                                if not release.wait(10): raise RuntimeError('test did not release acceptance')
                            yield
                    cleanup.enter_context(patch.object(SimRerouteExecution, '_accepting_next', pause_inflight))
                    def competing_thread():
                        try:
                            if not entered.wait(15):
                                attacks.append('no inflight acceptance'); return
                            before = robots['sim-r1'].executor.snapshot()
                            try:
                                reroute.execution.tick(reroute._payload, 1, guard=reroute.guard)
                                attacks.append('unguarded tick allowed')
                            except (ValueError, RuntimeError):
                                attacks.append('tick rejected')
                            duplicate = json.dumps(before['order'], separators=(',', ':')).encode('utf-8')
                            try:
                                with reroute.guard.guard_duplicate(reroute._payload, duplicate):
                                    attacks.append('other thread duplicate allowed')
                            except (ValueError, RuntimeError):
                                attacks.append('duplicate rejected')
                            pending = robots['sim-r1']._queue.get_nowait()
                            try:
                                try:
                                    reroute.execution.tick(reroute._payload, 1, guard=reroute.guard)
                                    attacks.append('empty queue tick allowed')
                                except (ValueError, RuntimeError):
                                    attacks.append('empty queue tick rejected')
                            finally:
                                robots['sim-r1']._queue.put_nowait(pending)
                            attacks.append('pose unchanged' if robots['sim-r1'].executor.snapshot() == before else 'pose changed')
                        finally:
                            release.set()
                    attacker = threading.Thread(target=competing_thread)
                    attacker.start()
                    cleanup.callback(lambda: (release.set(), attacker.join(10)))
                    original_step = reroute.step
                    def diagnose_step(dt):
                        try:
                            return original_step(dt)
                        except Exception:
                            import traceback
                            traceback.print_exc()
                            raise
                    cleanup.enter_context(patch.object(reroute, 'step', side_effect=diagnose_step))
                    def duplicate_first(*args):
                        result = publish_next(*args)
                        if not attempts:
                            attempts.append('duplicate')
                            deadline = time.monotonic() + 5
                            while time.monotonic() < deadline and robots['sim-r1']._queue.empty(): time.sleep(.01)
                            self.assertFalse(robots['sim-r1']._queue.empty())
                            next_packet = robots['sim-r1']._queue.get_nowait()
                            previous = robots['sim-r1'].executor.snapshot()['order']
                            info = robots['sim-r1']._client.publish(robots['sim-r1'].order_topic,
                                json.dumps(previous, separators=(',', ':')).encode('utf-8'), qos=1, retain=False)
                            info.wait_for_publish(timeout=5); self.assertTrue(info.is_published())
                            deadline = time.monotonic() + 5
                            while time.monotonic() < deadline and robots['sim-r1']._queue.empty(): time.sleep(.01)
                            self.assertFalse(robots['sim-r1']._queue.empty())
                            duplicate_packet = robots['sim-r1']._queue.get_nowait()
                            robots['sim-r1']._queue.put_nowait(duplicate_packet)
                            robots['sim-r1']._queue.put_nowait(next_packet)
                        return result
                    cleanup.enter_context(patch('server.sim_reroute_automatic.publish_next', side_effect=duplicate_first))
                first = PickupWorkflow(runtime, robots['sim-r1'], data, {'p': {'pickup_node': 'N03'}},
                    simulation_enabled=True, task_id='T1', session='boot-1', robot_id='sim-r1',
                    action_at_end={'type': 'none'}, wait_for_reservation=True, reroute_session=reroute)
                second = PickupWorkflow(runtime, robots['sim-r2'], data, {'p': {'pickup_node': 'N05'}},
                    simulation_enabled=True, task_id='T2', session='boot-2', robot_id='sim-r2',
                    action_at_end={'type': 'none'}, wait_for_reservation=True)
                self.assertNotEqual(first.submit('p')['status'], 'unknown')
                self.assertNotEqual(second.submit('p')['status'], 'unknown')
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    states = first.tick(1), second.tick(1)
                    if states[0]['status'] == 'unknown' and mode in ('lost_next', 'lost_event'):
                        break
                    if any(item['status'] == 'unknown' for item in states):
                        self.fail('workflow unknown: {}, phase={}'.format(states, reroute._phase))
                    if all(item['status'] == 'pickup_reached' for item in states):
                        break
                    time.sleep(.02)
                if mode in ('lost_next', 'lost_event'):
                    self.assertEqual(states[0]['status'], 'unknown')
                    self.assertEqual(attempts, ['next' if mode == 'lost_next' else 'event'])
                    held, second_before = runtime.store.list_occupancy(), robots['sim-r2'].executor.snapshot()
                    for _ in range(3):
                        self.assertEqual(first.tick(1)['status'], 'unknown')
                        second.tick(1)
                    self.assertEqual(robots['sim-r2'].executor.snapshot(), second_before)
                    self.assertEqual(runtime.store.list_occupancy(), held)
                    self.assertEqual(len(attempts), 1)
                    reroute.stop(); runtime.stop()
                    restarted = SimRuntime(folder / 'sim.sqlite3', data,
                        {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                        port=port, client_id='auto-multi-v3-restart', field_simulation=True)
                    restarted.start(); cleanup.callback(restarted.stop)
                    self.assertEqual(restarted.store.list_occupancy(), held)
                    with self.assertRaises(RuntimeError): restarted.prepare_reroute('T1')
                    return
                if mode == 'duplicate':
                    self.assertEqual(attempts, ['duplicate'])
                    attacker.join(10)
                    self.assertFalse(attacker.is_alive())
                    self.assertEqual(attacks, ['tick rejected', 'duplicate rejected', 'empty queue tick rejected', 'pose unchanged'])
                self.assertEqual([item['status'] for item in states], ['pickup_reached', 'pickup_reached'])
                self.assertEqual([item['current_node'] for item in states], ['N03', 'N05'])
                self.assertEqual(reroute._phase, 'finished')
                self.assertEqual(states[0]['finished_evidence']['seq'], 4)
                self.assertEqual(json.loads(reroute._payload)['data']['nodes'], ['N07', 'N01', 'J02', 'J07', 'N03'])
                self.assertEqual(runtime.inbox.get('sim-r2', states[1]['finished_evidence']['event_id'])['result']['status'], 'finished')


if __name__ == '__main__':
    unittest.main()
