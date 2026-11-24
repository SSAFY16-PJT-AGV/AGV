"""Opt-in rules for physical- robots in SimRuntime and the state-proof watcher (no broker)."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.physical_state_proof import FROM_STATE, PhysicalStateWatcher
from server.sim_runtime import SimRuntime
from server.tests.test_field_map_planner import sample_map


def make(robots, **options):
    folder = tempfile.mkdtemp()
    return SimRuntime(Path(folder) / 'db.sqlite3', sample_map(), robots, port=1883, client_id='cfg-test', **options)


class PhysicalRuntimeConfigTests(unittest.TestCase):
    def test_physical_robots_need_explicit_opt_in_and_field_simulation(self):
        robots = {'physical-1': {'P1': FROM_STATE}}
        with self.assertRaisesRegex(ValueError, 'physical_robots'):
            make(robots, field_simulation=True)
        with self.assertRaisesRegex(ValueError, 'field simulation'):
            make(robots, physical_robots=True)
        runtime = make(robots, field_simulation=True, physical_robots=True)
        self.assertEqual(runtime._physical, ('physical-1',))

    def test_only_physical_robots_may_take_session_from_state(self):
        with self.assertRaisesRegex(ValueError, 'physical'):
            make({'sim-r1': {'T1': FROM_STATE}}, field_simulation=True, physical_robots=True)

    def test_sim_only_configuration_is_unchanged(self):
        runtime = make({'sim-r1': {'T1': 'boot-1'}}, field_simulation=True)
        self.assertEqual(runtime._physical, ())


class FakeMessage:
    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, json.dumps(payload).encode(), retain


class WatcherTests(unittest.TestCase):
    def watcher(self):
        return PhysicalStateWatcher(['physical-1'], client=SimpleNamespace(), host='127.0.0.1', port=1,
                                    log=lambda msg: None)

    def test_session_comes_from_live_state_only(self):
        w = self.watcher()
        w._on_message(None, None, FakeMessage('agv/physical-1/state', {'robot_session': 'old'}, retain=True))
        with self.assertRaisesRegex(RuntimeError, 'physical-1'):
            w.wait_sessions(0.1)
        w._on_message(None, None, FakeMessage('agv/physical-1/state', {'robot_session': 'live'}))
        self.assertEqual(w.wait_sessions(0.1), {'physical-1': 'live'})

    def test_restarted_robot_blocks_its_tasks(self):
        w = self.watcher()
        blocked = []
        w._runtime = SimpleNamespace(_tasks={'P1': ('physical-1', 's1'), 'X': ('physical-2', 's2')},
                                     gate=SimpleNamespace(invalidate_many=blocked.append))
        w._sessions = {'physical-1': 's1'}
        self.assertTrue(w._check_session('physical-1', {'robot_session': 's1'}))
        self.assertFalse(w._check_session('physical-1', {'robot_session': 's2'}))
        self.assertEqual(blocked, [('P1',)])
        self.assertFalse(w._check_session('physical-1', {'robot_session': 's1'}))   # stays blocked

    def test_moving_or_unmatched_state_does_not_open_a_challenge(self):
        w = self.watcher()
        calls = []
        w._runtime = SimpleNamespace(_tasks={'P1': ('physical-1', 's1')}, path='/nonexistent',
                                     _field_simulation=True, begin=lambda *a: calls.append(a))
        for state in ({'status': 'MOVING', 'order_id': 'P1', 'seq': 1, 'last_node': 'A', 'released_until': 'A'},
                      {'status': 'IDLE', 'order_id': 'P1', 'seq': 1, 'last_node': 'B', 'released_until': 'C'},
                      {'status': 'IDLE', 'order_id': 'other', 'seq': 1, 'last_node': 'C', 'released_until': 'C'},
                      {'status': 'IDLE', 'order_id': 'P1', 'seq': 1, 'last_node': 'C', 'released_until': 'C'}):
            with self.subTest(state=state):
                w._maybe_prove('physical-1', dict(state, robot_session='s1'))
        self.assertEqual(calls, [])   # the last one has no readable allocation, so no challenge either


if __name__ == '__main__':
    unittest.main()


class PhysicalHandoffStoreTests(unittest.TestCase):
    def test_physical_idle_robots_need_a_physical_store(self):
        from server.sim_handoff_store import SimHandoffStore
        folder = Path(tempfile.mkdtemp())
        with self.assertRaisesRegex(ValueError, 'physical_robots'):
            SimHandoffStore.create_new(folder / 'a.sqlite3', simulation_enabled=True,
                                       idle_positions={'physical-1': 'A'})
        store = SimHandoffStore.create_new(folder / 'b.sqlite3', simulation_enabled=True,
                                           idle_positions={'physical-1': 'A', 'sim-r1': 'C'}, physical_robots=True)
        store.bind_field_robots(('physical-1', 'sim-r1'), {'physical-1': 'A', 'sim-r1': 'C'})
        self.assertEqual(store.list_occupancy(), {('node', 'A'): 'physical-1', ('node', 'C'): 'sim-r1'})

    def test_sim_store_keeps_rejecting_physical_jobs_in_the_database(self):
        import sqlite3
        from server.sim_handoff_store import SimHandoffStore
        folder = Path(tempfile.mkdtemp())
        store = SimHandoffStore.create_new(folder / 'c.sqlite3', simulation_enabled=True)
        with self.assertRaises(ValueError):
            store.bind_field_robots(('physical-1',), {'physical-1': 'A'})
        with sqlite3.connect(store.path) as db:
            db.execute("INSERT INTO tasks VALUES ('T','PENDING')")
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'simulation robot ID'):
                db.execute("INSERT INTO allocation_jobs VALUES ('T','physical-1','m','[]','A')")

    def test_physical_runtime_needs_a_physical_handoff_store(self):
        from server.sim_handoff_store import SimHandoffStore
        folder = Path(tempfile.mkdtemp())
        store = SimHandoffStore.create_new(folder / 'd.sqlite3', simulation_enabled=True)
        with self.assertRaisesRegex(ValueError, 'physical_robots=True'):
            SimRuntime(folder / 'd.sqlite3', sample_map(), {'physical-1': {'P1': FROM_STATE}}, port=1883,
                       client_id='cfg', field_simulation=True, physical_robots=True, simulation_store=store)
