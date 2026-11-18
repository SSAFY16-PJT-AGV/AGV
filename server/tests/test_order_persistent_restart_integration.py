"""Process restart of the isolated web fleet; no physical device is used.

The Unreal double keeps the same synthetic shelf and pickup-stock world while
only this test's own server child is terminated and recreated. A separate
test-owned broker, ports and database directory prevent interference.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, nullcontext
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from websockets.sync.client import connect

from server.order_demo_app import DemoRuntime
from server.tests.test_order_fleet_integration import scenario_map
from server.tests.test_order_multi_slot_integration import FakeUnreal, post_order
from server.tests.test_order_product_integration import action_reply, request
from server.tests.test_twin_broker_integration import (
    BROKER, free_port, stop, wait_for_owned_broker, wait_for_owned_server)
from server.tools.twin_multi_test import isolated_environment


ROOT = Path(__file__).resolve().parents[2]


def _start_test_server(python, port, env, log, cwd, flags):
    """Start the isolated app normally; no asynchronous traceback watchdog."""
    return subprocess.Popen([python, '-m', 'uvicorn', 'server.order_demo_app:app',
                             '--host', '127.0.0.1', '--port', str(port), '--workers', '1',
                             '--no-proxy-headers', '--no-access-log'], cwd=cwd, env=env,
                            stdout=log, stderr=log, creationflags=flags)


def _read_log_tail(path, limit=18000):
    """Read bounded evidence even if a child's log is missing or unreadable."""
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read(limit).decode('utf-8', errors='replace')
    except OSError as exc:
        return '<log unavailable: {}>'.format(exc)


def _request_with_diagnostics(operation, diagnostics):
    try:
        return operation()
    except Exception as exc:
        try:
            evidence = diagnostics()
        except Exception as diagnostic_exc:
            evidence = 'diagnostics unavailable: {!r}'.format(diagnostic_exc)
        raise AssertionError('server request failed: {!r}; {}'.format(exc, evidence)) from exc


def _wait_for_test_server(testcase, child, folder, port):
    """Keep startup errors and the owned child's exit/log evidence together."""
    logfile = Path(folder) / 'server.log'
    try:
        return wait_for_owned_server(testcase, child, str(logfile), port)
    except Exception as exc:
        raise AssertionError('server startup failed: {!r}; pid={!r}; exit={!r}; log={!r}'.format(
            exc, child.pid, child.poll(), _read_log_tail(logfile))) from exc


class PersistentFakeUnreal(FakeUnreal):
    """Keep the synthetic world's own pose, separate from restored server state."""
    def __init__(self, port, token, simulated_map, world=None, *, drop_grab_ack=False):
        super().__init__(port, token)
        self.states_before_snapshot = []
        self.drop_grab_ack = drop_grab_ack
        self.grab_ack_dropped = threading.Event()
        self.ping_requested = threading.Event()
        self.ping_completed = threading.Event()
        if world is None:
            nodes = {node['id']: node for node in simulated_map['nodes']}
            self.world_id = secrets.token_hex(16)
            self.robots = {
                agv: {'agv_id': agv, 'x': nodes[node]['x'], 'y': nodes[node]['y'],
                      'yaw': 0.0, 'carrying': False, 'interpolating': False}
                for agv, node in (('AGV_01', 'J01'), ('AGV_02', 'J05'))
            }
        else:
            self.slots = {key: list(value) for key, value in world['slots'].items()}
            self.stock = dict(world['stock'])
            self.world_id = world['world_id']
            self.robots = {key: dict(value) for key, value in world['robots'].items()}

    def capture_world(self):
        with self.lock:
            return {'slots': {key: list(value) for key, value in self.slots.items()},
                    'stock': dict(self.stock), 'world_id': self.world_id,
                    'robots': {key: dict(value) for key, value in self.robots.items()}}

    def probe_connection(self):
        """Require a fresh WebSocket pong from this test's server."""
        self.ping_completed.clear()
        self.ping_requested.set()
        return self.ping_completed.wait(5)

    def _run(self):
        try:
            uri = 'ws://127.0.0.1:{}/ws/digital-twin'.format(self.port)
            with connect(uri, proxy=None, origin='http://127.0.0.1', open_timeout=5,
                         additional_headers={'X-AGV-Twin-Token': self.token}) as ws:
                self.connected.set()
                initial_snapshot_sent = False
                while not self.stop_event.is_set():
                    if self.ping_requested.is_set():
                        self.ping_requested.clear()
                        if not ws.ping().wait(3):
                            raise AssertionError('fake Unreal did not receive a WebSocket pong')
                        self.ping_completed.set()
                    try:
                        message = json.loads(ws.recv(timeout=.1))
                    except TimeoutError:
                        continue
                    kind, data = message.get('type'), message.get('data', {})
                    if kind == 'robot.state':
                        with self.lock:
                            if not initial_snapshot_sent:
                                self.states_before_snapshot.append(dict(data))
                            robot = self.robots.get(data.get('agv_id'))
                            if robot is not None and all(key in data for key in ('x', 'y', 'yaw')):
                                robot.update({key: data[key] for key in ('x', 'y', 'yaw')})
                    elif kind == 'shelf.snapshot.request':
                        with self.lock:
                            shelves = [{'shelf_id': key,
                                        'product_type': {'SHELF_RED': 'Red', 'SHELF_BLUE': 'Blue'}[key],
                                        'slots': list(self.slots[key])} for key in data['shelf_ids']]
                            robots = [dict(robot) for robot in self.robots.values()]
                            pickups = [{'source_id': 'D01', 'product_type': 'Red', 'available': self.stock['Red']},
                                       {'source_id': 'D06', 'product_type': 'Blue', 'available': self.stock['Blue']}]
                            self.snapshots.append(shelves)
                        ws.send(json.dumps({'type': 'shelf.snapshot',
                                            'data': {'request_id': data['request_id'], 'shelves': shelves,
                                                     'world_id': self.world_id, 'robots': robots,
                                                     'pickups': pickups}}))
                        initial_snapshot_sent = True
                    elif kind == 'robot.action':
                        with self.lock:
                            if any(old['action_id'] == data['action_id'] for old in self.actions):
                                raise AssertionError('duplicate Unreal action')
                            robot = self.robots[data['agv_id']]
                            if data['action'] == 'grab':
                                product = data['product_type']
                                applied = self.stock.get(product, 0) > 0 and not robot['carrying']
                                if applied:
                                    self.stock[product] -= 1
                                    robot['carrying'] = True
                            elif data['action'] == 'place':
                                shelf, slot = data['shelf_id'], data['slot_index']
                                applied = (shelf in self.slots and type(slot) is int and 0 <= slot < 6
                                           and not self.slots[shelf][slot] and robot['carrying'])
                                if applied:
                                    self.slots[shelf][slot] = True
                                    robot['carrying'] = False
                            else:
                                raise AssertionError('unexpected action')
                            self.actions.append(dict(data, applied=applied))
                        if self.drop_grab_ack and data['action'] == 'grab' and applied:
                            # The synthetic world changed, but the server never
                            # receives a result for this action.
                            self.grab_ack_dropped.set()
                            continue
                        ws.send(action_reply(data, applied=applied))
                        if not applied:
                            raise AssertionError('fake Unreal rejected missing stock or occupied slot')
        except BaseException as exc:
            self.errors.append(repr(exc))
            self.connected.set()


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    return None


class RestoredWorldProofTest(unittest.TestCase):
    def setUp(self):
        self.world_id = 'a' * 32
        self.bound = []
        journal = SimpleNamespace(world_id=self.world_id, bind_world_id=self.bound.append,
                                  list_orders=lambda: [{'state': 'completed'}],
                                  load_shelves=lambda: {'reservations': []})
        robots = {
            'r1': SimpleNamespace(TWIN='AGV_01', snapshot=lambda: {'status': 'completed', 'pose': {'x': 1500, 'y': 2000, 'th': 0.0}}),
            'r2': SimpleNamespace(TWIN='AGV_02', snapshot=lambda: {'status': 'idle', 'pose': {'x': 14500, 'y': 7000, 'th': 0.0}}),
        }
        runtime = self.runtime = DemoRuntime.__new__(DemoRuntime)
        runtime.journal, runtime._world_ready = journal, threading.Event()
        runtime.field_mapping = None  # Match the legacy non-field demo constructor.
        runtime.pickup_store = None
        runtime._restart_blocked = False
        runtime._bootstrap_pending = False
        runtime.actions = SimpleNamespace(available_fast=lambda: True)
        runtime.auto_recovery = False
        runtime.engine = SimpleNamespace(engines=robots, inventory=SimpleNamespace(ready=True),
                                         snapshot=lambda: {})
        runtime._transport_ready = lambda: True
        runtime._store_snapshot = lambda *args: None
        self.proof = {'request_id': 'test', 'shelves': [], 'world_id': self.world_id,
                      'robots': [
                          {'agv_id': 'AGV_01', 'x': 1.5, 'y': 2.0, 'yaw': 0.0,
                           'carrying': False, 'interpolating': False},
                          {'agv_id': 'AGV_02', 'x': 14.5, 'y': 7.0, 'yaw': 0.0,
                           'carrying': False, 'interpolating': False},
                      ]}

    def test_matching_world_and_stopped_positions_can_open_gate(self):
        self.assertFalse(self.runtime._ready())
        self.assertFalse(self.runtime._may_send_state())
        self.runtime._validate_world(self.proof)
        self.assertFalse(self.runtime._ready(), 'validation alone must not open the gate')
        self.assertFalse(self.runtime._may_send_state())
        self.runtime._accept_world()
        self.assertEqual(self.bound, [self.world_id])
        self.assertTrue(self.runtime._ready())
        self.assertTrue(self.runtime._may_send_state())
        self.runtime._invalidate_world()
        self.assertFalse(self.runtime._ready())
        self.assertFalse(self.runtime._may_send_state())

    def test_wrong_world_mismatched_or_uncertain_pose_never_opens_gate(self):
        variants = []
        for change in ('world', 'position', 'carrying', 'interpolating', 'missing', 'duplicate'):
            proof = json.loads(json.dumps(self.proof))
            if change == 'world':
                proof['world_id'] = 'b' * 32
            elif change == 'position':
                proof['robots'][0]['x'] += 1
            elif change in ('carrying', 'interpolating'):
                proof['robots'][0][change] = True
            elif change == 'missing':
                del proof['robots']
            else:
                proof['robots'][1]['agv_id'] = 'AGV_01'
            variants.append((change, proof))
        for change, proof in variants:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.runtime._validate_world(proof)
            self.assertFalse(self.runtime._world_ready.is_set())
            self.assertFalse(self.runtime._ready())
            self.assertFalse(self.runtime._may_send_state())
            self.assertEqual(self.bound, [])

    def test_restored_completed_robot_yaw_mismatch_blocks_world_proof(self):
        self.proof['robots'][0]['yaw'] = 90.0
        with self.assertRaises(ValueError):
            self.runtime._validate_world(self.proof)
        self.assertFalse(self.runtime._world_ready.is_set())
        self.assertFalse(self.runtime._ready())
        self.assertFalse(self.runtime._may_send_state())
        self.assertEqual(self.bound, [])

    def test_restored_robot_invalid_yaw_never_opens_gate(self):
        for value in (float('nan'), float('inf'), float('-inf'), True, '0'):
            with self.subTest(value=value):
                self.proof['robots'][0]['yaw'] = value
                with self.assertRaises(ValueError):
                    self.runtime._validate_world(self.proof)
                self.assertFalse(self.runtime._ready())
                self.assertFalse(self.runtime._may_send_state())

    def test_invalid_saved_heading_never_opens_gate(self):
        for value in (None, float('nan'), float('inf'), True, '0'):
            with self.subTest(value=value):
                self.runtime.engine.engines['r1'].snapshot = lambda: {
                    'status': 'completed', 'pose': {'x': 1500, 'y': 2000, 'th': value}}
                with self.assertRaises(ValueError):
                    self.runtime._validate_world(self.proof)
                self.assertFalse(self.runtime._ready())
                self.assertFalse(self.runtime._may_send_state())

    def test_restored_robot_wraparound_yaws_are_equivalent(self):
        self.runtime.engine.engines['r1'].snapshot = lambda: {
            'status': 'completed', 'pose': {'x': 1500, 'y': 2000, 'th': 180.0}}
        self.proof['robots'][0]['yaw'] = -180.0
        self.runtime._validate_world(self.proof)
        self.runtime._accept_world()
        self.assertTrue(self.runtime._ready())

    def test_pristine_world_needs_confirmed_bootstrap_before_opening_gate(self):
        self.runtime.journal.list_orders = lambda: []
        for engine in self.runtime.engine.engines.values():
            old_snapshot = engine.snapshot
            engine.snapshot = lambda old=old_snapshot: dict(old(), status='idle')
        self.proof['robots'][0]['x'] += 1
        self.runtime._validate_world(self.proof)
        self.assertTrue(self.runtime._validated_bootstrap)
        self.assertFalse(self.runtime._may_send_state())
        self.runtime._accept_world()
        self.assertTrue(self.runtime._bootstrap_pending)
        self.assertTrue(self.runtime._may_send_state())
        self.assertFalse(self.runtime._ready())
        self.proof['robots'][0]['x'] -= 1
        self.runtime._validate_world(self.proof)
        self.runtime._accept_world()
        self.assertFalse(self.runtime._bootstrap_pending)
        self.assertTrue(self.runtime._ready())

    def test_pristine_yaw_mismatch_is_initialized_then_verified(self):
        self.runtime.journal.list_orders = lambda: []
        for engine in self.runtime.engine.engines.values():
            old_snapshot = engine.snapshot
            engine.snapshot = lambda old=old_snapshot: dict(old(), status='idle')
        self.proof['robots'][0]['yaw'] = 90.0
        self.runtime._validate_world(self.proof)
        self.runtime._accept_world()
        self.assertTrue(self.runtime._bootstrap_pending)
        self.assertFalse(self.runtime._ready())
        self.proof['robots'][0]['yaw'] = 0.0
        self.runtime._validate_world(self.proof)
        self.runtime._accept_world()
        self.assertFalse(self.runtime._bootstrap_pending)
        self.assertTrue(self.runtime._ready())

    def test_pristine_bootstrap_disallows_carrying_or_existing_reservation(self):
        self.runtime.journal.list_orders = lambda: []
        self.proof['robots'][0]['carrying'] = True
        with self.assertRaises(ValueError):
            self.runtime._validate_world(self.proof)
        self.proof['robots'][0]['carrying'] = False
        self.proof['robots'][0]['x'] += 1
        self.runtime.journal.load_shelves = lambda: {'reservations': [object()]}
        with self.assertRaises(ValueError):
            self.runtime._validate_world(self.proof)
        self.assertFalse(self.runtime._ready())


class PersistentDispatchBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.request = {'request_id': 'new', 'product_id': 'red'}
        product = {'product_type': 'Red', 'pickup_node': 'D01', 'destination_node': 'D04',
                   'shelf_id': 'SHELF_RED', 'slot_index': 0}
        active = {'request_id': 'old', 'state': 'active',
                  'result': {'assigned_robot': 'sim-r1', 'order_id': 'old-order'}}
        self.journal = SimpleNamespace(
            products={'red': product}, list_orders=lambda: [active],
            mark_completed=lambda *args: self.events.append('completed'),
            claim=lambda *args: self.events.append('claim'),
            mark_active=lambda *args: self.events.append('active'),
            get_order=lambda *args: {'state': 'dispatching'},
            mark_unknown=lambda *args: self.events.append('unknown'))
        checkpoint_engine = SimpleNamespace(export_completed_checkpoint=lambda: {'complete': True})
        state = {'accepting_orders': True, 'robots': [{'robot_id': 'sim-r1',
                 'order_id': 'old-order', 'status': 'completed'}]}
        self.engine = SimpleNamespace(snapshot=lambda: state, engines={'sim-r1': checkpoint_engine},
            inventory=SimpleNamespace(can_reserve=lambda *args: True),
            can_dispatch_destination=lambda *args: True,
            create_product_order=Mock(side_effect=lambda *args, **kwargs:
                                      (self.events.append('dispatch') or {'status': 'active'})),
            action_unknown=Mock(), tick=Mock())
        runtime = self.runtime = DemoRuntime.__new__(DemoRuntime)
        runtime.journal, runtime.engine = self.journal, self.engine
        runtime.pickup_store = None
        runtime.lock, runtime.stop_event = threading.RLock(), threading.Event()
        runtime.actions = SimpleNamespace(available_fast=lambda: True, motion_guard=lambda: nullcontext(True))
        runtime._restart_blocked, runtime.auto_recovery = False, False
        runtime._ready = lambda: not runtime._restart_blocked
        runtime._store_snapshot = lambda *args: None

    def test_completed_checkpoint_is_recorded_before_next_claim_or_dispatch(self):
        completed = Mock(side_effect=lambda *args: self.events.append('completed'))
        self.journal.mark_completed = completed
        self.runtime._dispatch_persistent(self.request)
        self.assertEqual(self.events, ['completed', 'claim', 'dispatch', 'active'])
        self.assertEqual(completed.call_args.args[1]['last_observation']['status'], 'completed')

    def test_dispatch_exception_after_claim_blocks_further_ticks(self):
        self.engine.create_product_order.side_effect = RuntimeError('uncertain dispatch')
        with self.assertRaisesRegex(RuntimeError, 'uncertain dispatch'):
            self.runtime._dispatch_persistent(self.request)
        self.assertEqual(self.events, ['completed', 'claim', 'unknown'])
        self.assertTrue(self.runtime._restart_blocked)
        self.engine.action_unknown.assert_called_once()
        self.assertIsNone(self.runtime._tick_if_ready(.1))
        self.engine.tick.assert_not_called()

    def test_pending_shelf_snapshot_pauses_without_starting_recovery(self):
        self.runtime.stop_event = SimpleNamespace(is_set=Mock(side_effect=[False, True]), wait=Mock())
        self.runtime._world_ready = threading.Event()
        self.runtime._world_ready.set()
        self.runtime.engine.inventory.ready = False
        self.runtime._ready = lambda: False
        self.runtime._transport_ready = lambda: True
        self.runtime._refresh_waiting_snapshot = Mock()
        self.runtime._enter_recovery = Mock()
        self.runtime._tick_if_ready = Mock()
        self.runtime.auto_recovery = True
        self.runtime._run()
        self.runtime._refresh_waiting_snapshot.assert_called_once()
        self.runtime._enter_recovery.assert_not_called()
        self.runtime._tick_if_ready.assert_not_called()
        self.engine.action_unknown.assert_not_called()


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class PersistentRestartIntegrationTest(unittest.TestCase):
    def test_active_and_queued_orders_and_key_survive_server_crash(self):
        self._exercise_restart(completed=False)

    def test_completed_order_and_occupied_slot_survive_server_restart(self):
        self._exercise_restart(completed=True)

    def test_applied_grab_with_lost_ack_stays_unknown_after_server_restart(self):
        self._exercise_restart(completed=False, lost_grab=True)

    def _exercise_restart(self, *, completed, lost_grab=False):
        broker_port, server_port = free_port(), free_port()
        while broker_port == server_port or 8000 in (broker_port, server_port):
            broker_port, server_port = free_port(), free_port()
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        token = secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory(prefix='agv-restart-persist-') as folder, \
                open(Path(folder) / 'broker.log', 'w', encoding='utf-8') as broker_log, \
                open(Path(folder) / 'server.log', 'w', encoding='utf-8') as server_log, \
                ExitStack() as cleanup:
            folder = Path(folder)
            durable = folder / 'durable-demo'
            durable.mkdir()
            map_path = folder / 'warehouse_map.json'
            simulated_map = scenario_map(False)
            for node in simulated_map['nodes']:
                node['x'] *= 4
                node['y'] *= 4
            map_path.write_text(json.dumps(simulated_map), encoding='utf-8')
            broker_env = isolated_environment(os.environ)
            broker_env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
            ready_file = str(folder / 'broker.ready')
            broker = subprocess.Popen([python, '-c', BROKER, str(broker_port), ready_file], cwd=ROOT,
                                      env=broker_env, stdout=broker_log, stderr=broker_log, creationflags=flags)
            def stop_owned(child):
                stop(child)
                self.assertIsNotNone(child.poll(), 'test-owned child must exit during cleanup')
            cleanup.callback(stop_owned, broker)
            wait_for_owned_broker(self, broker, ready_file)
            env = isolated_environment(os.environ)
            for key in list(env):
                if key.upper().startswith('AGV_'):
                    del env[key]
            env.update({'PYTHONPATH': os.pathsep.join([sysconfig.get_paths()['purelib'], str(ROOT)]),
                        'TEMP': str(folder), 'TMP': str(folder), 'AGV_ORDER_PERSIST_DIR': str(durable),
                        'AGV_ORDER_DEMO_ENABLED': '1', 'AGV_ORDER_COORDINATED': '1',
                        'AGV_ORDER_FLEET': '1', 'AGV_ORDER_SHELF_SYNC': '1',
                        'AGV_ORDER_DEMO_MAP': str(map_path), 'MQTT_HOST': '127.0.0.1',
                        'MQTT_PORT': str(broker_port), 'AGV_TWIN_WEBSOCKET': '1',
                        'AGV_TWIN_UNREAL_TOKEN': token,
                        'AGV_TWIN_ROBOT_MAP': json.dumps({'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}),
                        'AGV_TWIN_POSE_MAP_ID': 'twin-validation-v1'})

            def start_server():
                child = _start_test_server(python, server_port, env, server_log, ROOT, flags)
                cleanup.callback(stop_owned, child)
                _wait_for_test_server(self, child, folder, server_port)
                return child

            def start_unreal(world=None, *, drop_grab_ack=False):
                unreal = PersistentFakeUnreal(server_port, token, simulated_map, world,
                                              drop_grab_ack=drop_grab_ack)
                unreal.start()
                cleanup.callback(unreal.stop)
                return unreal

            def order(request_id):
                return get('/order-demo/api/orders/' + request_id)

            def child_evidence():
                return ('server_exit={!r}; broker_exit={!r}; unreal_alive={!r}; unreal_errors={!r}; '
                        'broker_log={!r}; server_log={!r}'.format(
                            server.poll(), broker.poll(), unreal.thread.is_alive(), unreal.errors,
                            _read_log_tail(folder / 'broker.log', 5000),
                            _read_log_tail(folder / 'server.log', 18000)))

            def get(path):
                return _request_with_diagnostics(lambda: request(server_port, path), child_evidence)

            def submit(key):
                return _request_with_diagnostics(lambda: post_order(server_port, key), child_evidence)

            suspended_since = {}
            def terminal(request_id):
                value = order(request_id)
                if value.get('status') in ('completed', 'unknown', 'failed'):
                    return value
                status = get('/order-demo/api/status')
                if value.get('status') == 'active' and status.get('status') == 'suspended':
                    since = suspended_since.setdefault(request_id, time.monotonic())
                    if time.monotonic() - since >= 15:
                        return value
                else:
                    suspended_since.pop(request_id, None)
                return None

            def diagnostics(request_id):
                return 'request_id={!r}; actions={!r}; {}'.format(request_id, unreal.actions, child_evidence())

            server = start_server()
            unreal = start_unreal(drop_grab_ack=lost_grab)
            self.assertTrue(wait_for(lambda: get('/order-demo/api/status').get('accepting_orders')))
            self.assertEqual(unreal.states_before_snapshot, [], 'poses must not precede the first world proof')
            products_before = get('/order-demo/api/products')
            self.assertEqual(get('/order-demo/api/inventory')['pickup']['sources']['D01']['available'], 3)
            code, first = submit('persist-running')
            self.assertIn(code, (200, 202))
            first_id = first.get('request_id')
            self.assertIsInstance(first_id, str, 'all accepted orders need an addressable durable ID')

            if lost_grab:
                self.assertTrue(unreal.grab_ack_dropped.wait(120), diagnostics(first_id))
                with unreal.lock:
                    self.assertEqual(unreal.stock['Red'], 2)
                    self.assertEqual([a['action'] for a in unreal.actions], ['grab'])
                    self.assertTrue(unreal.actions[0]['applied'])
                    self.assertTrue(unreal.robots[unreal.actions[0]['agv_id']]['carrying'])
                pickup_db = durable / 'pickup-inventory.sqlite3'
                simulation_db = durable / 'simulation.sqlite3'
                with closing(sqlite3.connect(pickup_db)) as db:
                    before = db.execute('SELECT reservation_id,state FROM reservations WHERE request_id=?',
                                        (first_id,)).fetchone()
                self.assertIsNotNone(before)
                self.assertEqual(before[1], 'inflight')
                with closing(sqlite3.connect(simulation_db)) as db:
                    occupancy = tuple(db.execute('SELECT kind,resource_id,task_id FROM allocation_occupancy '
                                                 'ORDER BY kind,resource_id,task_id'))
                self.assertTrue(occupancy)

                # Stop only this test's server after the fake world applied the
                # grab but before it sent any acknowledgement.
                stop(server)
                unreal.stop()
                world = unreal.capture_world()
                server = start_server()
                unreal = start_unreal(world)
                self.assertTrue(wait_for(lambda: get('/order-demo/api/status').get('unreal_connected')))
                self.assertTrue(wait_for(lambda: unreal.snapshots), 'restart must request a world snapshot')
                recovered = order(first_id)
                self.assertEqual(recovered['status'], 'unknown', diagnostics(first_id))
                self.assertFalse(get('/order-demo/api/status')['accepting_orders'])
                def reconciled_pickup_observation():
                    observed = get('/order-demo/api/inventory')['pickup']
                    if (observed['sources']['D01']['available'] == 2
                            and any(item['request_id'] == first_id and item['state'] == 'unknown'
                                    for item in observed['reservations'])):
                        return observed
                    return None
                pickup = wait_for(reconciled_pickup_observation)
                self.assertIsNotNone(pickup, diagnostics(first_id))
                with closing(sqlite3.connect(pickup_db)) as db:
                    after = db.execute('SELECT reservation_id,state FROM reservations WHERE request_id=?',
                                       (first_id,)).fetchone()
                self.assertEqual(after, (before[0], 'unknown'))
                self.assertEqual(pickup['sources']['D01']['available'], 2)
                self.assertEqual(pickup['sources']['D01']['reserved'], 1)
                self.assertTrue(any(item['request_id'] == first_id and item['state'] == 'unknown'
                                    for item in pickup['reservations']))
                with closing(sqlite3.connect(simulation_db)) as db:
                    self.assertEqual(tuple(db.execute('SELECT kind,resource_id,task_id FROM allocation_occupancy '
                                                      'ORDER BY kind,resource_id,task_id')), occupancy)
                _code, replay = submit('persist-running')
                self.assertEqual(replay['request_id'], first_id)
                self.assertTrue(unreal.thread.is_alive(), 'the new fake Unreal must still receive actions')
                self.assertEqual(unreal.errors, [], 'the new fake Unreal must not have failed')
                self.assertTrue(unreal.probe_connection(), 'the new fake Unreal must receive a pong')
                self.assertTrue(wait_for(lambda: get('/order-demo/api/status')['unreal_connected'], timeout=5))
                time.sleep(1)
                self.assertTrue(unreal.thread.is_alive(), 'the fake Unreal must stay alive during observation')
                self.assertEqual(unreal.errors, [], 'the fake Unreal must not have failed during observation')
                self.assertTrue(unreal.probe_connection(), 'the fake Unreal must remain connected')
                self.assertTrue(wait_for(lambda: get('/order-demo/api/status')['unreal_connected'], timeout=5))
                unreal.stop()
                self.assertFalse(unreal.thread.is_alive(), 'the fake Unreal must stop cleanly')
                self.assertEqual(unreal.errors, [], 'the fake Unreal must exit without an error')
                with unreal.lock:
                    self.assertEqual(unreal.actions, [], 'the unknown grab must not run twice')
                    self.assertEqual(unreal.stock['Red'], 2)
                    self.assertEqual(sum(robot['carrying'] for robot in unreal.robots.values()), 1)
                self.assertEqual(order(first_id)['status'], 'unknown')
                return

            if completed:
                completed_order = wait_for(lambda: terminal(first_id), timeout=120)
                self.assertIsNotNone(completed_order, diagnostics(first_id))
                self.assertEqual(completed_order['status'], 'completed', diagnostics(first_id))
                self.assertEqual(completed_order['product_id'], 'red')
                self.assertEqual(completed_order['slot_index'], 1, 'initial red slot 0 was already occupied')
                self.assertEqual(get('/order-demo/api/inventory')['pickup']['sources']['D01']['available'], 2)
                before_status = get('/order-demo/api/status')
                robot_id = completed_order['assigned_robot']
                before_robot = next(item for item in before_status['robots'] if item['robot_id'] == robot_id)
                before_yaw = before_robot['pose']['th']
                self.assertTrue(math.isfinite(before_yaw))
                self.assertGreater(abs((before_yaw + 180) % 360 - 180), 1,
                                   'the test must finish at a nonzero orientation')
                twin_id = {'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}[robot_id]
                def actor_yaw_matches(expected):
                    with unreal.lock:
                        actual = unreal.robots[twin_id]['yaw']
                    return abs((actual - expected + 180) % 360 - 180) < 1e-4
                self.assertTrue(wait_for(lambda: actor_yaw_matches(before_yaw)),
                                'Unreal must have observed the final heading before restart')
                with unreal.lock:
                    self.assertTrue(unreal.slots['SHELF_RED'][1])
                    self.assertEqual(len([a for a in unreal.actions if a['action'] == 'place']), 1)
                stop(server)
                unreal.stop()
                world = unreal.capture_world()
                server = start_server()
                unreal = start_unreal(world)
                self.assertTrue(wait_for(lambda: get('/order-demo/api/status').get('unreal_connected')))
                self.assertTrue(wait_for(lambda: unreal.snapshots), 'world proof was not requested after restart')
                self.assertEqual(unreal.states_before_snapshot, [], 'restart poses must not precede world proof')
                restored_status = wait_for(lambda: (state if not (state := get(
                    '/order-demo/api/status')).get('stale') and state.get('accepting_orders') else None))
                self.assertIsNotNone(restored_status, 'matching same-world heading must permit safe continuation')
                after_robot = next(item for item in restored_status['robots'] if item['robot_id'] == robot_id)
                after_yaw = after_robot['pose']['th']
                self.assertLess(abs((after_yaw - before_yaw + 180) % 360 - 180), 1e-4,
                                'restored server heading must match the last completed heading')
                time.sleep(.6)
                self.assertTrue(actor_yaw_matches(before_yaw),
                                'the Unreal actor must retain its heading after state publication')
                self.assertEqual(get('/order-demo/api/inventory')['pickup']['sources']['D01']['available'], 2)
                self.assertEqual(get('/order-demo/api/products'), products_before)
                restored = order(first_id)
                self.assertEqual(restored['request_id'], first_id)
                self.assertEqual(restored['status'], 'completed')
                self.assertEqual((restored['product_id'], restored['slot_index']), ('red', 1))
                _code, replay = submit('persist-running')
                self.assertEqual(replay['request_id'], first_id)
                time.sleep(1)
                with unreal.lock:
                    self.assertEqual(unreal.actions, [], 'a completed order must never run twice')
                    self.assertTrue(unreal.slots['SHELF_RED'][1])
                # A terminal task has an actual completed receipt and place
                # acknowledgement; the same simulated world may continue.
                code, next_order = submit('after-completed')
                self.assertIn(code, (200, 202))
                next_id = next_order.get('request_id')
                self.assertIsInstance(next_id, str)
                self.assertNotEqual(next_id, first_id)
                next_done = wait_for(lambda: terminal(next_id), timeout=120)
                self.assertIsNotNone(next_done, diagnostics(next_id))
                self.assertEqual(next_done['status'], 'completed', diagnostics(next_id))
                self.assertEqual(next_done['slot_index'], 2)
                self.assertEqual(get('/order-demo/api/inventory')['pickup']['sources']['D01']['available'], 1)
                with unreal.lock:
                    self.assertTrue(unreal.slots['SHELF_RED'][2])
                    self.assertEqual(len([a for a in unreal.actions if a['action'] == 'place']), 1)
                    grabs_before = len([a for a in unreal.actions if a['action'] == 'grab'])
                # Only one pickup remains. Concurrent web submissions must not
                # invent a second product or send a second Unreal grab.
                with ThreadPoolExecutor(max_workers=2) as pool:
                    responses = list(pool.map(submit,
                                              ('last-stock-a', 'last-stock-b')))
                self.assertTrue(all(code in (200, 202) for code, _ in responses), responses)
                last_ids = [response['request_id'] for _, response in responses]
                self.assertEqual(len(set(last_ids)), 2)
                for last_id in last_ids:
                    result = wait_for(lambda: terminal(last_id), timeout=120)
                    self.assertIsNotNone(result, diagnostics(last_id))
                self.assertEqual(sorted(order(last_id)['status'] for last_id in last_ids), ['completed', 'failed'])
                self.assertEqual(get('/order-demo/api/inventory')['pickup']['sources']['D01']['available'], 0)
                with unreal.lock:
                    self.assertEqual(len([a for a in unreal.actions if a['action'] == 'grab']), grabs_before + 1)
                    self.assertTrue(unreal.slots['SHELF_RED'][4])
                return

            def moving():
                state = get('/order-demo/api/status')
                return (not state.get('stale') and any(r.get('status') == 'moving'
                                                       for r in state.get('robots', [])))
            self.assertTrue(wait_for(moving), 'first simulated AGV should start before crash')
            code, second = submit('persist-queued')
            self.assertEqual(code, 202, 'same-destination second order must wait')
            second_id = second.get('request_id')
            self.assertIsInstance(second_id, str)
            self.assertNotEqual(first_id, second_id)
            dbs = list(durable.rglob('simulation.sqlite3'))
            self.assertEqual(len(dbs), 1, 'the active reservation DB must be in the explicit persistent directory')
            with closing(sqlite3.connect(dbs[0])) as db:
                before = tuple(db.execute('SELECT kind,resource_id,task_id FROM allocation_occupancy '
                                          'ORDER BY kind,resource_id,task_id'))
            self.assertTrue(before)
            with unreal.lock:
                self.assertEqual(unreal.actions, [], 'crash must be injected before any Unreal action')
                action_ids = {a['action_id'] for a in unreal.actions}
            # Windows terminate is an abrupt process death; do not let the app
            # run its normal shutdown routine or discard its volatile state.
            stop(server)
            unreal.stop()
            world = unreal.capture_world()
            server = start_server()
            unreal = start_unreal(world)
            self.assertTrue(wait_for(lambda: get('/order-demo/api/status').get('unreal_connected')))
            self.assertTrue(wait_for(lambda: unreal.snapshots), 'world proof was not requested after restart')
            self.assertEqual(unreal.states_before_snapshot, [], 'restart poses must not precede world proof')
            self.assertEqual(get('/order-demo/api/products'), products_before)
            first_recovered, second_recovered = order(first_id), order(second_id)
            self.assertEqual((first_recovered['request_id'], second_recovered['request_id']),
                             (first_id, second_id))
            self.assertEqual((first_recovered['product_id'], second_recovered['product_id']), ('red', 'red'))
            self.assertEqual(first_recovered['status'], 'unknown')
            self.assertEqual(second_recovered['status'], 'queued')
            with closing(sqlite3.connect(dbs[0])) as db:
                after = tuple(db.execute('SELECT kind,resource_id,task_id FROM allocation_occupancy '
                                         'ORDER BY kind,resource_id,task_id'))
            self.assertEqual(after, before, 'a process crash must not release reserved resources')
            # Identical idempotency keys may only resolve to their old records;
            # neither retry may start a new physical or simulated action.
            for key, expected in (('persist-running', first_id), ('persist-queued', second_id)):
                _code, response = submit(key)
                self.assertEqual(response['request_id'], expected)
            time.sleep(1)
            with unreal.lock:
                self.assertTrue(action_ids.isdisjoint({a['action_id'] for a in unreal.actions}))
                self.assertEqual(unreal.actions, [], 'uncertain work must not be replayed automatically')
            with closing(sqlite3.connect(dbs[0])) as db:
                self.assertEqual(tuple(db.execute('SELECT kind,resource_id,task_id FROM allocation_occupancy '
                                                  'ORDER BY kind,resource_id,task_id')), before)


if __name__ == '__main__':
    unittest.main()
