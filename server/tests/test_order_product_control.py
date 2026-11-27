"""Product status requires reserved MQTT legs and correlated Unreal results."""
import copy
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from server.order_product_control import ProductControlEngine


def map_data():
    return {'map_id': 'demo', 'version': 1, 'unit': 'mm',
            'nodes': [{'id': 'A', 'pos': [0, 0]}, {'id': 'P', 'pos': [1000, 0]},
                      {'id': 'D', 'pos': [2000, 0]}],
            'edges': [{'from': 'A', 'to': 'P', 'shape': [[0, 0], [1000, 0]],
                       'bidirectional': True, 'lanes': 1},
                      {'from': 'P', 'to': 'D', 'shape': [[1000, 0], [2000, 0]],
                       'bidirectional': True, 'lanes': 1}],
            'zones': [{'id': 'Await', 'type': 'waiting', 'node': 'A'},
                      {'id': 'Pwait', 'type': 'waiting', 'node': 'P'},
                      {'id': 'Dwait', 'type': 'waiting', 'node': 'D'}]}


PRODUCTS = {'Red': {'pickup_node': 'P', 'destination_node': 'D',
                    'shelf_id': 'SHELF_RED', 'slot_index': 0},
            'Blue': {'pickup_node': 'A', 'destination_node': 'D',
                     'shelf_id': 'SHELF_BLUE', 'slot_index': 0}}


class Executor:
    def __init__(self):
        self.state = {'status': 'idle', 'current_node': 'A', 'pose': {'x': 0, 'y': 0, 'th': 0},
                      'order': None, 'receipt': None}
    def snapshot(self):
        return copy.deepcopy(self.state)


class Robot:
    ready = True
    def __init__(self):
        self.executor = Executor()


class Runtime:
    store = object()
    gate = object()
    progress = object()
    receiver = object()
    is_ready = True
    def __init__(self):
        self.calls = []
    def status(self):
        return {'ready': self.is_ready}
    def begin(self, task, session):
        self.calls.append(('begin', task, session))
        return {'challenge': 'new-proof'}
    def complete(self, *args):
        self.calls.append(('complete',) + args)
        return {'reconciled': True}


class Workflow:
    created = []
    def __init__(self, runtime, robot, data, products, **kwargs):
        self.runtime, self.robot, self.products, self.kwargs = runtime, robot, products, kwargs
        self.task = kwargs.get('task_id', 'web-pickup')
        self.ticks = 0
        self.current = {'status': 'idle', 'route': [], 'finished_evidence': None}
        self.__class__.created.append(self)
    def submit(self, product):
        self.product = product
        self.current['status'] = 'moving'
        return self.snapshot()
    def tick(self, dt):
        self.ticks += 1
        return self.snapshot()
    def snapshot(self):
        return copy.deepcopy(self.current)


class ProductControlTests(unittest.TestCase):
    def setUp(self):
        Workflow.created = []
        self.robot, self.runtime = Robot(), Runtime()
        self.workflow_patch = patch('server.order_product_control.PickupWorkflow', Workflow)
        self.handoff_patch = patch('server.order_product_control.SimHandoffCoordinatorStore')
        self.workflow_patch.start()
        self.handoff = self.handoff_patch.start()
        self.addCleanup(self.workflow_patch.stop)
        self.addCleanup(self.handoff_patch.stop)
        self.engine = ProductControlEngine(self.runtime, self.robot, map_data(), PRODUCTS)

    def product_order(self):
        return self.engine.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)

    def test_second_robot_has_distinct_task_and_twin_identity(self):
        robot = Robot()
        engine = ProductControlEngine(self.runtime, robot, map_data(), PRODUCTS,
                                      robot_id='sim-r2', twin_id='AGV_02',
                                      session='demo-session-r2', task_prefix='r2-web',
                                      wait_for_reservation=True)
        result = engine.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)
        self.assertEqual(result['order_id'], 'r2-web-pickup')
        self.assertEqual(Workflow.created[-1].kwargs['robot_id'], 'sim-r2')
        self.assertEqual(Workflow.created[-1].kwargs['session'], 'demo-session-r2')
        self.assertTrue(Workflow.created[-1].kwargs['wait_for_reservation'])

    def test_reserved_segment_wait_is_visible_and_can_resume(self):
        self.product_order()
        Workflow.created[0].current['status'] = 'waiting_reservation'
        waiting = self.engine.tick(.1)
        self.assertEqual((waiting['status'], waiting['phase']), ('waiting', 'reservation_wait'))
        Workflow.created[0].current['status'] = 'moving'
        resumed = self.engine.tick(.1)
        self.assertEqual((resumed['status'], resumed['phase']), ('moving', 'to_pickup'))

    def test_virtual_reroute_distinguishes_waiting_from_motion(self):
        self.product_order()
        Workflow.created[0].current.update(status='rerouting', reroute_motion='waiting')
        waiting = self.engine.tick(.1)
        self.assertEqual((waiting['status'], waiting['phase']), ('waiting', 'reroute_wait'))
        Workflow.created[0].current['reroute_motion'] = 'moving'
        moving = self.engine.tick(.1)
        self.assertEqual((moving['status'], moving['phase']), ('moving', 'reroute_moving'))
        Workflow.created[0].current.pop('reroute_motion')
        self.assertEqual(self.engine.tick(.1)['status'], 'unknown')

    def finish_pickup(self):
        self.robot.executor.state = {'status': 'stopped', 'current_node': 'P',
            'pose': {'x': 1000, 'y': 0, 'th': 0},
            'order': {'order_id': 'web-pickup', 'seq': 1, 'map_id': 'demo@v1',
                      'nodes': ['A', 'P'], 'released_until': 'P', 'action_at_end': {'type': 'load'}},
            'receipt': {'new': True, 'state': 'completed', 'order_id': 'web-pickup',
                        'seq': 1, 'digest': 'digest1'}}
        Workflow.created[0].current.update(status='pickup_reached', finished_evidence={
            'task_id': 'web-pickup', 'session': 'demo-session', 'robot_id': 'sim-r1',
            'seq': 1, 'stopped_at': 'P', 'event_id': 'web-pickup-1',
            'map_id': 'demo@v1', 'digest': 'digest1'})
        return self.engine.tick(1)

    def ack(self, pending, applied=True):
        return self.engine.apply_action_result(pending['order_id'], pending['action_id'],
                                               pending['agv_id'], pending['action'], applied)

    def test_order_uses_workflow_and_only_grab_ack_starts_handoff(self):
        self.assertTrue(self.engine.control_ready)
        started = self.product_order()
        self.assertEqual((started['status'], started['phase']), ('moving', 'to_pickup'))
        self.assertEqual(started['route'], ['A', 'P', 'D'])
        self.assertEqual(Workflow.created[0].product, 'Red')
        waiting = self.finish_pickup()
        self.assertEqual((waiting['status'], waiting['phase']), ('waiting', 'waiting_grab'))
        grab = waiting['pending_action']
        self.assertEqual((grab['order_id'], grab['action'], grab['product_type']), ('web-pickup', 'grab', 'Red'))
        self.assertFalse(self.engine.apply_action_result('web-pickup', 'wrong', 'AGV_01', 'grab', True))
        self.handoff.assert_not_called()
        self.assertTrue(self.ack(grab))
        self.assertEqual(self.engine.snapshot()['phase'], 'to_destination')
        self.assertEqual(len(Workflow.created), 2)
        options = Workflow.created[1].kwargs
        self.assertEqual(options['task_id'], 'web-delivery')
        self.assertEqual(options['accept_preceding'], ('web-pickup', 'P'))
        self.assertIs(options['prepare_options']['handoff_store'], self.handoff.return_value)
        self.assertEqual(self.handoff.call_args.kwargs['action_ack']['action_id'], grab['action_id'])
        self.assertFalse(self.ack(grab))

    def finish_delivery(self):
        self.robot.executor.state = {'status': 'stopped', 'current_node': 'D',
            'pose': {'x': 2000, 'y': 0, 'th': 0},
            'order': {'order_id': 'web-delivery', 'seq': 1, 'map_id': 'demo@v1',
                      'nodes': ['P', 'D'], 'released_until': 'D', 'action_at_end': {'type': 'unload'}},
            'receipt': {'new': True, 'state': 'completed', 'order_id': 'web-delivery',
                        'seq': 1, 'digest': 'digest2'}}
        Workflow.created[1].current.update(status='pickup_reached', finished_evidence={
            'task_id': 'web-delivery', 'session': 'demo-session', 'robot_id': 'sim-r1',
            'seq': 1, 'stopped_at': 'D', 'event_id': 'web-delivery-1',
            'map_id': 'demo@v1', 'digest': 'digest2'})
        return self.engine.tick(1)

    def test_delivery_completion_requires_place_ack(self):
        self.product_order()
        grab = self.finish_pickup()['pending_action']
        self.ack(grab)
        waiting = self.finish_delivery()
        self.assertEqual((waiting['status'], waiting['phase']), ('waiting', 'waiting_place'))
        place = waiting['pending_action']
        self.assertEqual((place['order_id'], place['action'], place['shelf_id'], place['slot_index']),
                         ('web-delivery', 'place', 'SHELF_RED', 0))
        self.assertTrue(self.ack(place))
        self.assertEqual((self.engine.snapshot()['status'], self.engine.snapshot()['phase']),
                         ('completed', 'done'))

    def test_completed_reroute_guard_follows_each_handoff(self):
        self.product_order()
        pickup_guard, delivery_guard = object(), object()
        Workflow.created[0].reroute_handoff_guard = pickup_guard
        grab = self.finish_pickup()['pending_action']
        self.ack(grab)
        self.assertIs(self.handoff.call_args.kwargs['reroute_guard'], pickup_guard)
        Workflow.created[1].reroute_handoff_guard = delivery_guard
        place = self.finish_delivery()['pending_action']
        self.ack(place)
        calls_before = list(self.runtime.calls)
        self.engine.create_product_order('Blue', 'A', 'D', 'SHELF_BLUE', 0)
        self.assertIs(self.handoff.call_args.kwargs['reroute_guard'], delivery_guard)
        self.assertEqual(self.runtime.calls, calls_before)

    def test_next_product_refreshes_proof_and_atomically_hands_off_previous_stop(self):
        self.product_order()
        grab = self.finish_pickup()['pending_action']
        self.ack(grab)
        place = self.finish_delivery()['pending_action']
        self.ack(place)
        prior_ticks = Workflow.created[1].ticks
        self.engine.tick(10)
        self.assertEqual(Workflow.created[1].ticks, prior_ticks)
        second = self.engine.create_product_order('Blue', 'A', 'D', 'SHELF_BLUE', 0)
        self.assertEqual((second['status'], second['phase']), ('moving', 'to_pickup'))
        self.assertEqual(second['mode'], 'coordinated')
        self.assertEqual(second['orders_remaining'], 14)
        self.assertEqual(second['order_id'], 'web-2-pickup')
        self.assertEqual(second['route'], ['D', 'P', 'A', 'P', 'D'])
        self.assertEqual(self.runtime.calls[0], ('begin', 'web-delivery', 'demo-session'))
        self.assertEqual(self.runtime.calls[1], ('complete', 'web-delivery', 'demo-session', 'new-proof',
                                               'sim-r1', 'web-delivery', 1, 'demo@v1', 'D', 'digest2', True))
        self.assertEqual(self.handoff.call_count, 2)
        handoff = self.handoff.call_args.kwargs
        self.assertEqual((handoff['source_task_id'], handoff['target_task_id'], handoff['stopped_at']),
                         ('web-delivery', 'web-2-pickup', 'D'))
        self.assertEqual(handoff['action_ack']['action'], 'place')
        self.assertEqual(handoff['action_ack']['action_id'], place['action_id'])
        self.assertEqual(Workflow.created[2].kwargs['accept_preceding'], ('web-delivery', 'D'))

    def test_cannot_start_next_product_after_uncertain_previous_position(self):
        self.product_order()
        self.ack(self.finish_pickup()['pending_action'])
        self.ack(self.finish_delivery()['pending_action'])
        self.robot.executor.state['current_node'] = 'P'
        with self.assertRaises(RuntimeError):
            self.engine.create_product_order('Blue', 'A', 'D', 'SHELF_BLUE', 0)
        self.assertEqual(self.engine.snapshot()['status'], 'unknown')
        self.assertEqual(self.handoff.call_count, 1)

    def test_invalid_mapping_failures_and_unknown_do_not_advance(self):
        with self.assertRaises(ValueError):
            self.engine.create_product_order('Red', 'P', 'D', 'wrong', 0)
        self.product_order()
        grab = self.finish_pickup()['pending_action']
        self.assertFalse(self.ack(dict(grab, action_id='wrong')))
        self.assertTrue(self.ack(grab, False))
        self.assertEqual(self.engine.snapshot()['status'], 'failed')
        self.handoff.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.product_order()

    def test_disconnect_and_missing_evidence_mark_unknown(self):
        self.product_order()
        Workflow.created[0].current['status'] = 'pickup_reached'
        self.assertEqual(self.engine.tick(1)['status'], 'unknown')
        self.handoff.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.product_order()

    def test_unready_runtime_and_direct_node_order_rejected(self):
        self.runtime.is_ready = False
        self.assertFalse(self.engine.control_ready)
        with self.assertRaises(RuntimeError):
            self.product_order()
        with self.assertRaises(RuntimeError):
            self.engine.create_order('A', 'D')


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class ProductControlBrokerTests(unittest.TestCase):
    def test_product_legs_use_real_reservations_orders_events_and_atomic_handoff(self):
        from server.sim_handoff_store import SimHandoffStore
        from server.sim_order_authorization import SimOrderAuthorizer
        from server.sim_runtime import SimRuntime
        from server.tests.test_twin_broker_integration import (
            BROKER, free_port, loopback_environment, stop, wait_for_owned_broker,
        )
        from sim.mqtt_order_robot import SimMqttOrderRobot
        from sim.order_executor import SimOrderExecutor
        from sim.order_inbox import SimOrderInbox

        port = free_port()
        env = loopback_environment()
        env['PYTHONPATH'] = os.environ['AMQTT_TEST_PATH']
        python = getattr(sys, '_base_executable', sys.executable)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with tempfile.TemporaryDirectory(prefix='sim-product-control-') as folder, \
                open(os.devnull, 'w', encoding='utf-8') as broker_log:
            folder = Path(folder)
            ready_file = str(folder / 'broker.ready')
            broker = subprocess.Popen([python, '-c', BROKER, str(port), ready_file], env=env,
                                      stdout=broker_log, stderr=broker_log, creationflags=flags)
            try:
                wait_for_owned_broker(self, broker, ready_file)
                with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost',
                                             'no_proxy': '127.0.0.1,localhost'}), \
                        patch('sim.mqtt_order_robot.read_mqtt_security', return_value=(None, None, None)), \
                        patch('server.sim_runtime.read_mqtt_security', return_value=(None, None, None)):
                    path = folder / 'allocation.sqlite3'
                    store = SimHandoffStore.create_new(path, simulation_enabled=True)
                    runtime = SimRuntime(path, map_data(), {'sim-r1': {'web-pickup': 'demo-session',
                                         'web-delivery': 'demo-session', 'web-2-pickup': 'demo-session',
                                         'web-2-delivery': 'demo-session'}}, port=port,
                                         client_id='product-control-integration', simulation_store=store)
                    inbox = SimOrderInbox(folder / 'robot.sqlite3', 'sim-r1', 'demo@v1')
                    inbox.initialize()
                    authorizer = SimOrderAuthorizer(runtime)
                    robot = SimMqttOrderRobot(SimOrderExecutor(inbox, map_data(), 'A'), port,
                                             authorization_guard=authorizer.guard)
                    receiver_errors = []
                    try:
                        runtime.start()
                        runtime.receiver.on_error = lambda error: receiver_errors.append(str(error))
                        robot.start()
                        engine = ProductControlEngine(runtime, robot, map_data(), PRODUCTS)
                        self.assertEqual(engine.create_product_order('Red', 'P', 'D', 'SHELF_RED', 0)['phase'],
                                         'to_pickup')

                        def await_phase(phase):
                            deadline = time.monotonic() + 15
                            while time.monotonic() < deadline:
                                state = engine.tick(0.1)
                                if state['phase'] == phase:
                                    return state
                                self.assertNotIn(state['status'], ('unknown', 'failed'), state)
                                time.sleep(0.03)
                            self.fail('virtual product order did not reach ' + phase)

                        grab = await_phase('waiting_grab')['pending_action']
                        self.assertTrue(engine.apply_action_result(grab['order_id'], grab['action_id'],
                                                                   grab['agv_id'], grab['action'], True))
                        place = await_phase('waiting_place')['pending_action']
                        self.assertTrue(engine.apply_action_result(place['order_id'], place['action_id'],
                                                                   place['agv_id'], place['action'], True))
                        self.assertEqual(engine.snapshot()['status'], 'completed')
                        self.assertEqual(engine.snapshot()['current_node'], 'D')
                        first_pose = engine.snapshot()['pose']
                        self.assertEqual(engine.tick(5)['pose'], first_pose)
                        next_order = engine.create_product_order('Blue', 'A', 'D', 'SHELF_BLUE', 0)
                        self.assertEqual(next_order['order_id'], 'web-2-pickup')
                        self.assertEqual(next_order['route'], ['D', 'P', 'A', 'P', 'D'])
                        grab = await_phase('waiting_grab')['pending_action']
                        self.assertEqual((grab['order_id'], grab['product_type']), ('web-2-pickup', 'Blue'))
                        self.assertTrue(engine.apply_action_result(grab['order_id'], grab['action_id'],
                                                                   grab['agv_id'], grab['action'], True))
                        place = await_phase('waiting_place')['pending_action']
                        self.assertEqual((place['order_id'], place['shelf_id']), ('web-2-delivery', 'SHELF_BLUE'))
                        self.assertTrue(engine.apply_action_result(place['order_id'], place['action_id'],
                                                                   place['agv_id'], place['action'], True))
                        self.assertEqual(engine.snapshot()['status'], 'completed')
                        with closing(sqlite3.connect(path)) as db:
                            self.assertEqual(db.execute('SELECT task_id,state FROM allocation_outbox ORDER BY task_id').fetchall(),
                                             [('web-2-delivery', 'acked'), ('web-2-pickup', 'acked'),
                                              ('web-delivery', 'acked'), ('web-pickup', 'acked')])
                            self.assertEqual(db.execute('SELECT source_task_id,target_task_id,stopped_at FROM sim_handoff_transfers ORDER BY rowid').fetchall(),
                                             [('web-pickup', 'web-delivery', 'P'),
                                              ('web-delivery', 'web-2-pickup', 'D'),
                                              ('web-2-pickup', 'web-2-delivery', 'A')])
                            self.assertEqual(db.execute('SELECT task_id FROM sim_active_robots WHERE robot_id=?', ('sim-r1',)).fetchone(),
                                             ('web-2-delivery',))
                            self.assertEqual(db.execute("SELECT task_id,resource_id FROM allocation_occupancy WHERE kind='node'").fetchall(),
                                             [('web-2-delivery', 'D')])
                    finally:
                        runtime.stop()
                        robot.stop()
                    self.assertEqual(receiver_errors, ['simulation runtime stopped'])
                    self.assertFalse(runtime.status()['ready'])
                    self.assertFalse(runtime.receiver.ready)
                    self.assertTrue(runtime.receiver._closed)
                    self.assertTrue({'web-pickup', 'web-delivery', 'web-2-pickup',
                                     'web-2-delivery'}.issubset(runtime.gate._blocked))
                    with closing(sqlite3.connect(path)) as db:
                        challenges = db.execute('SELECT status FROM recovery_challenges').fetchall()
                        self.assertTrue(challenges)
                        self.assertTrue(all(status == 'revoked' for (status,) in challenges))
                    with self.assertRaises(RuntimeError):
                        runtime.submit('web-2-delivery', 1, 'demo-session')
            finally:
                stop(broker)
