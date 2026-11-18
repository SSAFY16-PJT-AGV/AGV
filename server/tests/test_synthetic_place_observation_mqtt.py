"""Real isolated MQTT delivery observation; never a physical stop proof."""
from contextlib import ExitStack, closing
import importlib
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

from server.order_product_control import ProductControlEngine
from server.sim_handoff_store import SimHandoffStore
from server.sim_order_authorization import SimOrderAuthorizer
from server.sim_runtime import SimRuntime
from server.synthetic_action_transaction import SyntheticActionSession
from server.tests.test_order_product_control import map_data, PRODUCTS
from server.tests.test_twin_broker_integration import BROKER, free_port, loopback_environment, stop, wait_for_owned_broker
from sim.mqtt_order_robot import SimMqttOrderRobot
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


WORLD = 'a' * 32
CATALOG = [dict(id='red', product_type='Red', pickup_node='P', destination_node='D',
                shelf_id='SHELF_RED', slot_count=3)]


def seed_simulated_order(folder, *, pickup='P'):
    assignment = {'assigned_robot': 'sim-r1', 'order_id': 'web-pickup', 'shelf_id': 'SHELF_RED',
                  'slot_index': 0, 'reservation_id': 'slot-1', 'pickup_reservation_id': 'pickup-1'}
    with closing(sqlite3.connect(folder / 'orders.sqlite3')) as db:
        db.execute("INSERT INTO orders(request_id,fingerprint,product_id,state,result_json) "
                   "VALUES ('request-1','product:red','red','active',?)", (json.dumps(assignment),))
        db.execute("INSERT INTO slot_reservations VALUES ('slot-1','SHELF_RED',0,'request-1','reserved')")
        db.commit()
    with closing(sqlite3.connect(folder / 'pickup-inventory.sqlite3')) as db:
        db.execute('UPDATE counts SET available=1 WHERE source_id=?', (pickup,))
        db.execute('UPDATE metadata SET revision=1 WHERE singleton=1')
        db.execute("INSERT INTO reservations(reservation_id,request_id,source_id,product_type,state) "
                   "VALUES ('pickup-1','request-1',?,'Red','reserved')", (pickup,))
        db.commit()


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class SyntheticPlaceObservationMqttTests(unittest.TestCase):
    def test_team_map_delivery_revisit_uses_final_visit_index(self):
        data = json.loads((Path(__file__).resolve().parents[1] / 'examples' /
                           'mat_ssafy_v2_simulated_waits.json').read_text(encoding='utf-8'))
        product = {'Red': {'pickup_node': 'M1', 'destination_node': 'P_blue',
                           'shelf_id': 'SHELF_RED', 'slot_index': 0}}
        catalog = [dict(id='red', product_type='Red', pickup_node='M1', destination_node='P_blue',
                        shelf_id='SHELF_RED', slot_count=3)]
        port = free_port()
        env = loopback_environment()
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='synthetic-place-repeat-') as folder, ExitStack() as cleanup:
            folder = Path(folder)
            ready_file = str(folder / 'broker.ready')
            log = cleanup.enter_context(open(os.devnull, 'w', encoding='utf-8'))
            broker = subprocess.Popen([python, '-c', BROKER, str(port), ready_file], env=env,
                                      stdout=log, stderr=log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_file)
            with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}), \
                    patch('sim.mqtt_order_robot.read_mqtt_security', return_value=(None, None, None)), \
                    patch('server.sim_runtime.read_mqtt_security', return_value=(None, None, None)):
                path = folder / 'allocation.sqlite3'
                starts = {'sim-r1': 'S1'}
                store = SimHandoffStore.create_new(path, simulation_enabled=True, idle_positions=starts)
                runtime = SimRuntime(path, data, {'sim-r1': {'web-pickup': 'demo-session',
                                     'web-delivery': 'demo-session'}}, port=port,
                                     client_id='place-repeat-observation-test', simulation_store=store,
                                     field_simulation=True, field_initial_positions=starts)
                inbox = SimOrderInbox(folder / 'robot.sqlite3', 'sim-r1', 'mat_ssafy_v2@v0')
                inbox.initialize()
                authorizer = SimOrderAuthorizer(runtime)
                executor = SimOrderExecutor(inbox, data, 'S1', experimental_field_map=True,
                                            field_incoming_edge='E1')
                robot = SimMqttOrderRobot(executor, port, authorization_guard=authorizer.guard)
                try:
                    runtime.start()
                    robot.start()
                    engine = ProductControlEngine(runtime, robot, data, product)
                    engine.create_product_order('Red', 'M1', 'P_blue', 'SHELF_RED', 0)
                    def await_phase(phase):
                        deadline = time.monotonic() + 25
                        while time.monotonic() < deadline:
                            state = engine.tick(.1)
                            if state['phase'] == phase:
                                return state
                            self.assertNotIn(state['status'], ('unknown', 'failed'), state)
                            time.sleep(.03)
                        self.fail('field synthetic order did not reach ' + phase)
                    grab = await_phase('waiting_grab')['pending_action']
                    session = SyntheticActionSession.create_new(folder / 'pair', map_id='mat_ssafy_v2@v0',
                                                                 catalog=catalog, pickup_sources={'M1': 'Red'},
                                                                 world_id=WORLD, robot_twins={'sim-r1': 'AGV_01'})
                    try:
                        seed_simulated_order(folder / 'pair', pickup='M1')
                        session.stage_attempt(request_id='request-1', task_id=grab['order_id'],
                                              action_id=grab['action_id'], agv_id=grab['agv_id'], action='grab')
                        session.record_result(request_id='request-1', task_id=grab['order_id'],
                                              action_id=grab['action_id'], agv_id=grab['agv_id'],
                                              world_id=WORLD, session_id=session.session_id, outcome='applied')
                        self.assertTrue(engine.apply_action_result(grab['order_id'], grab['action_id'],
                                                                   grab['agv_id'], grab['action'], True))
                        place = await_phase('waiting_place')['pending_action']
                        state = executor.snapshot()
                        self.assertEqual(state['order']['nodes'],
                                         ['M1', 'P_purple', 'M1', 'S1', 'P_orange', 'P_blue'])
                        self.assertEqual((state['visit_index'], state['released_index']), (5, 5))
                        verifier = importlib.import_module('server.synthetic_place_observation')
                        result = verifier.observe_waiting_place(engine, session, request_id='request-1', simulation=True)
                        self.assertEqual(result['action_id'], place['action_id'])
                        with executor._lock:
                            original_visit = executor._visit_index
                            executor._visit_index = 2
                            try:
                                with self.assertRaises((ValueError, RuntimeError)):
                                    verifier.observe_waiting_place(engine, session, request_id='request-1', simulation=True)
                            finally:
                                executor._visit_index = original_visit
                        with closing(sqlite3.connect(path)) as db:
                            self.assertEqual(db.execute('SELECT current_index FROM occurrence_allocations '
                                                        "WHERE task_id='web-delivery'").fetchone(), (5,))
                            db.execute("UPDATE occurrence_allocations SET current_index=2 WHERE task_id='web-delivery'")
                            db.commit()
                        with self.assertRaises((ValueError, RuntimeError)):
                            verifier.observe_waiting_place(engine, session, request_id='request-1', simulation=True)
                    finally:
                        session.close()
                finally:
                    runtime.stop()
                    robot.stop()

    def test_real_pickup_handoff_and_delivery_pending_place_are_observed_without_side_effect(self):
        port = free_port()
        env = loopback_environment()
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='synthetic-place-observe-') as folder, ExitStack() as cleanup:
            folder = Path(folder)
            ready_file = str(folder / 'broker.ready')
            log = cleanup.enter_context(open(os.devnull, 'w', encoding='utf-8'))
            broker = subprocess.Popen([python, '-c', BROKER, str(port), ready_file], env=env,
                                      stdout=log, stderr=log, creationflags=flags)
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_file)
            with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}), \
                    patch('sim.mqtt_order_robot.read_mqtt_security', return_value=(None, None, None)), \
                    patch('server.sim_runtime.read_mqtt_security', return_value=(None, None, None)):
                path = folder / 'allocation.sqlite3'
                store = SimHandoffStore.create_new(path, simulation_enabled=True)
                runtime = SimRuntime(path, map_data(), {'sim-r1': {'web-pickup': 'demo-session',
                                     'web-delivery': 'demo-session'}}, port=port,
                                     client_id='place-observation-test', simulation_store=store)
                inbox = SimOrderInbox(folder / 'robot.sqlite3', 'sim-r1', 'demo@v1')
                inbox.initialize()
                authorizer = SimOrderAuthorizer(runtime)
                robot = SimMqttOrderRobot(SimOrderExecutor(inbox, map_data(), 'A'), port,
                                          authorization_guard=authorizer.guard)
                try:
                    runtime.start()
                    robot.start()
                    engine = ProductControlEngine(runtime, robot, map_data(), PRODUCTS)
                    engine.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)
                    def await_phase(phase):
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            state = engine.tick(.1)
                            if state['phase'] == phase:
                                return state
                            self.assertNotIn(state['status'], ('unknown', 'failed'), state)
                            time.sleep(.03)
                        self.fail('synthetic order did not reach ' + phase)
                    grab = await_phase('waiting_grab')['pending_action']
                    session = SyntheticActionSession.create_new(folder / 'pair', map_id='demo@v1', catalog=CATALOG,
                                                                 pickup_sources={'P': 'Red'}, world_id=WORLD,
                                                                 robot_twins={'sim-r1': 'AGV_01'})
                    try:
                        seed_simulated_order(folder / 'pair')
                        session.stage_attempt(request_id='request-1', task_id=grab['order_id'],
                                              action_id=grab['action_id'], agv_id=grab['agv_id'], action='grab')
                        session.record_result(request_id='request-1', task_id=grab['order_id'],
                                              action_id=grab['action_id'], agv_id=grab['agv_id'],
                                              world_id=WORLD, session_id=session.session_id, outcome='applied')
                        self.assertTrue(engine.apply_action_result(grab['order_id'], grab['action_id'],
                                                                   grab['agv_id'], grab['action'], True))
                        place = await_phase('waiting_place')['pending_action']
                        self.assertEqual(place['order_id'], 'web-delivery')
                        try:
                            verifier = importlib.import_module('server.synthetic_place_observation')
                        except ModuleNotFoundError as exc:
                            if exc.name != 'server.synthetic_place_observation':
                                raise
                            self.fail('synthetic delivery observation boundary is not implemented')
                        before = (store.list_occupancy(), engine.snapshot(), session._pair.orders.load_shelves())
                        with self.assertRaises(ValueError):
                            verifier.observe_waiting_place(engine, session, request_id='request-1')
                        result = verifier.observe_waiting_place(engine, session, request_id='request-1', simulation=True)
                        self.assertEqual(result['scope'], 'isolated_simulation')
                        self.assertEqual(result['snapshot_consistency'], 'separate_databases')
                        self.assertEqual(result['causality'], 'not_established')
                        self.assertFalse(result['valid_for_later_action'])
                        self.assertEqual((result['pickup_task'], result['delivery_task'], result['action_id']),
                                         ('web-pickup', 'web-delivery', place['action_id']))
                        self.assertEqual((store.list_occupancy(), engine.snapshot(), session._pair.orders.load_shelves()), before)
                        with self.assertRaises((ValueError, RuntimeError)):
                            verifier.observe_waiting_place(engine, session, request_id='missing-request', simulation=True)
                        self.assertTrue(session.record_result(
                            request_id='request-1', task_id=grab['order_id'], action_id=grab['action_id'],
                            agv_id=grab['agv_id'], world_id=WORLD, session_id=session.session_id,
                            outcome='applied')['duplicate'])
                        wrong_catalog = [dict(CATALOG[0], pickup_node='X')]
                        wrong_session = SyntheticActionSession.create_new(
                            folder / 'wrong-pair', map_id='demo@v1', catalog=wrong_catalog,
                            pickup_sources={'X': 'Red'}, world_id=WORLD, robot_twins={'sim-r1': 'AGV_01'})
                        try:
                            seed_simulated_order(folder / 'wrong-pair', pickup='X')
                            wrong_session.stage_attempt(request_id='request-1', task_id=grab['order_id'],
                                                        action_id=grab['action_id'], agv_id=grab['agv_id'], action='grab')
                            wrong_session.record_result(request_id='request-1', task_id=grab['order_id'],
                                                        action_id=grab['action_id'], agv_id=grab['agv_id'],
                                                        world_id=WORLD, session_id=wrong_session.session_id,
                                                        outcome='applied')
                            with self.assertRaises((ValueError, RuntimeError)):
                                verifier.observe_waiting_place(engine, wrong_session,
                                                               request_id='request-1', simulation=True)
                        finally:
                            wrong_session.close()
                        with engine.lock:
                            engine._pending['order_id'] = 'wrong'
                            with self.assertRaises((ValueError, RuntimeError)):
                                verifier.observe_waiting_place(engine, session, request_id='request-1', simulation=True)
                            engine._pending['order_id'] = 'web-delivery'
                            actual_action_id = engine._pending['action_id']
                            engine._pending['action_id'] = 'not-an-engine-action-id'
                            try:
                                with self.assertRaises((ValueError, RuntimeError)):
                                    verifier.observe_waiting_place(engine, session, request_id='request-1', simulation=True)
                            finally:
                                engine._pending['action_id'] = actual_action_id
                        with closing(sqlite3.connect(path)) as db:
                            previous = db.execute('SELECT ack_json FROM sim_handoff_transfers').fetchone()[0]
                            ack = json.loads(previous)
                            ack['action_id'] = 'wrong'
                            db.execute('UPDATE sim_handoff_transfers SET ack_json=?', (json.dumps(ack),))
                            db.commit()
                        with self.assertRaises((ValueError, RuntimeError)):
                            verifier.observe_waiting_place(engine, session, request_id='request-1', simulation=True)
                        with closing(sqlite3.connect(path)) as db:
                            db.execute('UPDATE sim_handoff_transfers SET ack_json=?', (previous,))
                            db.commit()
                        self.assertEqual(verifier.observe_waiting_place(
                            engine, session, request_id='request-1', simulation=True)['delivery_task'], 'web-delivery')
                        read_grab = verifier._grab_observation
                        read_count = []
                        def shutdown_during_last_check(*args):
                            value = read_grab(*args)
                            read_count.append(True)
                            if len(read_count) == 2:
                                runtime._stopped = True
                            return value
                        try:
                            with patch.object(verifier, '_grab_observation', side_effect=shutdown_during_last_check):
                                with self.assertRaises(RuntimeError):
                                    verifier.observe_waiting_place(engine, session,
                                                                   request_id='request-1', simulation=True)
                        finally:
                            runtime._stopped = False
                        with closing(sqlite3.connect(path)) as db:
                            previous_digest = db.execute("SELECT digest FROM progress_events WHERE task_id='web-delivery'").fetchone()[0]
                            db.execute("UPDATE progress_events SET digest=? WHERE task_id='web-delivery'", ('0' * 64,))
                            db.commit()
                        with self.assertRaises((ValueError, RuntimeError)):
                            verifier.observe_waiting_place(engine, session, request_id='request-1', simulation=True)
                        with closing(sqlite3.connect(path)) as db:
                            db.execute("UPDATE progress_events SET digest=? WHERE task_id='web-delivery'", (previous_digest,))
                            db.commit()
                        runtime.gate.invalidate('web-delivery')
                        with self.assertRaises((ValueError, RuntimeError)):
                            verifier.observe_waiting_place(engine, session, request_id='request-1', simulation=True)
                        evidence = engine._delivery_evidence
                        challenge = runtime.begin('web-delivery', engine.SESSION)['challenge']
                        runtime.complete('web-delivery', engine.SESSION, challenge, engine.ROBOT,
                                         'web-delivery', evidence['seq'], evidence['map_id'],
                                         evidence['stopped_at'], evidence['digest'], True)
                        entered, release, fenced = threading.Event(), threading.Event(), threading.Event()
                        observed, stopped = [], []
                        original_final = verifier._final_event
                        original_stop = runtime.workflow.stop
                        def slow_final(*args):
                            original_final(*args)
                            entered.set()
                            if not release.wait(5):
                                raise RuntimeError('test did not release final observation')
                        def marked_stop():
                            fenced.set()
                            return original_stop()
                        def observe():
                            try:
                                observed.append(verifier.observe_waiting_place(
                                    engine, session, request_id='request-1', simulation=True))
                            except BaseException as exc:
                                observed.append(exc)
                        def shutdown():
                            try:
                                runtime.stop()
                                stopped.append(True)
                            except BaseException as exc:
                                stopped.append(exc)
                        with patch.object(verifier, '_final_event', side_effect=slow_final), \
                                patch.object(runtime.workflow, 'stop', side_effect=marked_stop):
                            checker = threading.Thread(target=observe)
                            stopper = threading.Thread(target=shutdown)
                            checker.start()
                            try:
                                self.assertTrue(entered.wait(5))
                                stopper.start()
                                self.assertTrue(fenced.wait(3), 'runtime stop must fence while DB observation waits')
                            finally:
                                release.set()
                                checker.join(10)
                                if stopper.ident is not None:
                                    stopper.join(10)
                            self.assertFalse(checker.is_alive())
                            self.assertFalse(stopper.is_alive())
                            self.assertEqual(len(observed), 1)
                            self.assertIsInstance(observed[0], (RuntimeError, ValueError))
                            self.assertEqual(stopped, [True])
                    finally:
                        session.close()
                finally:
                    runtime.stop()
                    robot.stop()
