"""Two ``physical-`` robots (team field-run code on a virtual mat) through SimRuntime.

This runs the team robot's real serve-mode stack (MqttLink, LinkController,
Navigator) as ``physical-1``/``physical-2`` with synthetic cameras and motors,
and checks the physical path end to end:

- sessions are read from each robot's live state (``from_state``),
- stop proofs come from robot state, and blocked rear_clear events are retried,
- routes end at spur ends (the only waiting points of the real map),
- a second robot waits while the first holds a shared node,
- each robot's next leg starts from the spur where the last one ended (handoff
  without an action acknowledgement, because the legs have no end action).

Nothing here moves hardware. Requires a field-run checkout (AGV_FIELD_REPO),
OpenCV/NumPy (AGV_FIELD_MAT_DEPS) and Mosquitto (MOSQUITTO_EXE). The aMQTT test
broker is not used: it delivers a retained ``agv/<id>/state`` to an
``agv/+/event`` subscriber once another client subscribed to that state topic,
which the strict event receiver correctly rejects. Mosquitto does not.
"""
import json
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest

import paho.mqtt.client as mqtt

from server.tools.twin_multi_test import isolated_environment, stop_process


ROOT = Path(__file__).resolve().parents[2]
FIELD = os.environ.get('AGV_FIELD_REPO')
MAT_DEPS = os.environ.get('AGV_FIELD_MAT_DEPS')
MOSQUITTO = os.environ.get('MOSQUITTO_EXE')
MAP_REF = 'mat_ssafy_v2@v0'


@unittest.skipUnless(FIELD and MAT_DEPS and MOSQUITTO,
                     'set AGV_FIELD_REPO, AGV_FIELD_MAT_DEPS and MOSQUITTO_EXE for the physical fleet test')
class PhysicalFleetIntegration(unittest.TestCase):
    def test_two_physical_robots_spur_to_spur_with_shared_segment(self):
        from server.sim_handoff_store import SimHandoffStore
        from server.sim_runtime import SimRuntime
        field = Path(FIELD).resolve()
        mat_deps = Path(MAT_DEPS).resolve()
        field_map = json.loads((field / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        parent = ROOT / '.venv'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='physical-fleet-', dir=parent) as temp, ExitStack() as stack:
            run = Path(temp)
            python = getattr(sys, '_base_executable', sys.executable)
            conf = run / 'mosquitto.conf'
            conf.write_text('listener %d 127.0.0.1\nallow_anonymous true\n' % port, encoding='utf-8')
            blog = (run / 'broker.log').open('w', encoding='utf-8')
            stack.callback(blog.close)
            broker = subprocess.Popen([MOSQUITTO, '-c', str(conf)], stdout=blog, stderr=blog)
            stack.callback(stop_process, broker)
            deadline = time.monotonic() + 10
            while True:
                try:
                    socket.create_connection(('127.0.0.1', port), 0.5).close()
                    break
                except OSError:
                    if time.monotonic() > deadline or broker.poll() is not None:
                        self.fail('mosquitto did not start')
                    time.sleep(.1)

            states = {}
            lock = threading.Lock()
            subscribed = threading.Event()
            watch = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)

            def on_message(_c, _u, msg):
                robot = msg.topic.split('/')[1]
                with lock:
                    states[robot] = json.loads(msg.payload.decode('utf-8'))
            watch.on_connect = lambda c, u, f, r, p: c.subscribe('agv/+/state', 0)
            watch.on_subscribe = lambda c, u, mid, codes, props: subscribed.set()
            watch.on_message = on_message
            watch.connect('127.0.0.1', port, 15)
            watch.loop_start()
            stack.callback(watch.loop_stop)
            stack.callback(watch.disconnect)
            self.assertTrue(subscribed.wait(5))

            robot_env = isolated_environment(os.environ)
            robot_env.update(PYTHONPATH=os.pathsep.join([str(mat_deps), sysconfig.get_paths()['purelib']]),
                             PYTHONUTF8='1', TEMP=str(run), TMP=str(run))
            logs = {}
            # physical-1 in front of orange (the mat's default pose); physical-2 in front of blue.
            starts = {'physical-1': ('P_orange', []), 'physical-2': ('P_blue', ['--start-pose', '528,188.3,180'])}
            for rid, (start, extra) in starts.items():
                logs[rid] = run / (rid + '.log')
                handle = logs[rid].open('w', encoding='utf-8')
                stack.callback(handle.close)
                proc = subprocess.Popen(
                    [python, '-u', str(field / 'sim/link_demo.py'), '--host', '127.0.0.1', '--port', str(port),
                     '--robot-id', rid, '--start', start, '--waiting', 'zones', '--battery', '-1',
                     '--max-time', '900', '--out', str(run / rid)] + extra, cwd=field, env=robot_env,
                    stdout=handle, stderr=handle)
                stack.callback(stop_process, proc)

            def robot_logs():
                return '\n'.join('--- %s\n%s' % (r, p.read_text(encoding='utf-8', errors='replace')[-3000:])
                                 for r, p in logs.items())

            def wait(predicate, label, seconds):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(.1)
                with lock:
                    snap = dict(states)
                self.fail('timed out: %s\nstates=%r\nwatcher=%r\n%s' % (
                    label, snap, getattr(runtime.physical_watcher, 'last_error', None) if runtime else None,
                    robot_logs()))

            runtime = None
            wait(lambda: len(states) == 2, 'both robots report state', 30)

            positions = {rid: start for rid, (start, _extra) in starts.items()}
            store = SimHandoffStore.create_new(run / 'server.sqlite3', simulation_enabled=True,
                                               idle_positions=positions, physical_robots=True)
            runtime = SimRuntime(run / 'server.sqlite3', field_map,
                                 {'physical-1': {'P1-1': 'from_state', 'P1-2': 'from_state'},
                                  'physical-2': {'P2-1': 'from_state', 'P2-2': 'from_state'}},
                                 port=port, client_id='physical-fleet-test', field_simulation=True,
                                 physical_robots=True, simulation_store=store, field_initial_positions=positions)
            runtime.start()
            stack.callback(runtime.stop)
            with lock:
                for rid, task in (('physical-1', 'P1-1'), ('physical-2', 'P2-1')):
                    self.assertEqual(runtime._tasks[task], (rid, states[rid]['robot_session']))

            def at(rid, node):
                with lock:
                    s = states.get(rid) or {}
                return s.get('status') == 'IDLE' and s.get('last_node') == node

            def finished(task, goal):
                # A finished leg stays ASSIGNED and keeps only its stop node reserved.
                status = runtime.task_status(task)
                return (status['released_until'] == goal and status['occupancy'] == [['node', goal]]
                        and not runtime.pending_events()['events'])

            def go(task, rid, node, edge, goal, after=None):
                runtime.create_task(task)
                deadline = time.monotonic() + 120
                while True:
                    try:
                        if after is not None:
                            result = runtime.prepare_field_next(task, after, goal)
                        else:
                            result = runtime.prepare_field(task, goal, [{'robot_id': rid, 'node': node,
                                                                         'incoming_edge': edge, 'available': True}],
                                                           {'type': 'none'})
                        break
                    except ValueError:
                        # Another robot holds part of the route; wait like an operator would.
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(.5)
                runtime.publish(task)
                return result

            # physical-1 takes P_orange -> red spur first; physical-2's route from blue to the
            # purple spur also needs P_orange and S1, so it is refused until physical-1 parked.
            first = go('P1-1', 'physical-1', 'P_orange', 'E13', 'D_P_red')
            self.assertEqual(first['nodes'][-2:], ['P_red', 'D_P_red'])
            started = time.monotonic()
            second = go('P2-1', 'physical-2', 'P_blue', 'E12', 'D_P_purple')
            self.assertGreater(time.monotonic() - started, 2.0, 'second robot must wait for the shared nodes')
            self.assertEqual(second['nodes'][0], 'P_blue')
            self.assertTrue(at('physical-1', 'D_P_red'), 'route released only after physical-1 parked')
            wait(lambda: at('physical-2', 'D_P_purple') and finished('P2-1', 'D_P_purple'), 'physical-2 parks', 120)
            self.assertTrue(finished('P1-1', 'D_P_red'))

            # Next legs start from the spurs (handoff). Their routes share P_orange, so one waits.
            third = go('P1-2', 'physical-1', None, None, 'D_P_orange', after='P1-1')
            self.assertEqual(third['nodes'][:2], ['D_P_red', 'P_red'])
            fourth = go('P2-2', 'physical-2', None, None, 'D_P_blue', after='P2-1')
            self.assertEqual(fourth['nodes'][:2], ['D_P_purple', 'P_purple'])
            wait(lambda: at('physical-1', 'D_P_orange') and finished('P1-2', 'D_P_orange'), 'physical-1 second leg', 180)
            wait(lambda: at('physical-2', 'D_P_blue') and finished('P2-2', 'D_P_blue'), 'physical-2 second leg', 180)
            self.assertEqual(runtime.store.list_occupancy(),
                             {('node', 'D_P_orange'): 'physical-1', ('node', 'D_P_blue'): 'physical-2'})
            for task in ('P1-1', 'P2-1'):
                self.assertEqual(runtime.task_status(task)['status'], 'DONE')
            self.assertIsNone(runtime.physical_watcher.last_error)
            self.assertIsNone(runtime.workflow.last_error)


if __name__ == '__main__':
    unittest.main()
