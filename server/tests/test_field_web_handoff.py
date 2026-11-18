"""Field routes in isolated web-order ownership; no real robot is contacted."""
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
import paho.mqtt.client as mqtt

from server.field_map_planner import plan_field_route
from server.recovery import RecoveryGate
from server.route_occurrence import plan_occurrence_segment, occurrence_clearance
from server.segment_progress import SegmentProgress
from server.sim_handoff_store import SimHandoffStore
from server.sim_runtime import SimRuntime
from server.sim_order_authorization import SimOrderAuthorizer
from server.tests.test_sim_order_authorization import Runtime
from server.order_pickup_workflow import PickupWorkflow
from server.order_product_control import ProductControlEngine
from server.order_fleet import FleetProductControlEngine
from server.tests.test_occurrence_allocation_lab import laboratory_map
from sim.event_inbox import SimEventInbox
from sim.event_receiver import SimEventReceiver
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


class FieldWebHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.map = laboratory_map()
        self.path = Path(self.tmp.name) / 'field-web.sqlite3'
        self.store = SimHandoffStore.create_new(self.path, simulation_enabled=True,
                                                 idle_positions={'sim-r1': 'A', 'sim-r2': 'X'})
        self.fingerprint = hashlib.sha256(json.dumps(self.map, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.store.create_task('web-pickup')
        self.nodes = ['A', 'B', 'C', 'B', 'D']
        self.segment = plan_occurrence_segment(self.map, self.nodes, 0)

    def test_field_first_allocation_retains_occurrence_and_idle_ownership(self):
        allocation = self.store.allocate('web-pickup', 'sim-r1', 'lab@v1', self.nodes,
            self.segment.released_until, self.segment.resources, {'type': 'load'},
            map_fingerprint=self.fingerprint, release_index=self.segment.end_index,
            field_context={'map_data': self.map, 'incoming_edge': 'E0', 'goal': 'D'})
        self.assertEqual(allocation['field_route'], {'incoming_edge': 'E0', 'goal': 'D'})
        self.assertEqual(self.store.list_occupancy()[('node', 'B')], 'sim-r1')
        self.assertEqual(self.store.list_occupancy()[('node', 'X')], 'sim-r2')
        with self.store._write() as db:
            self.assertEqual(db.execute('SELECT current_index FROM occurrence_allocations WHERE task_id=?',
                                        ('web-pickup',)).fetchone(), (1,))

    def test_virtual_robot_exposes_checked_incoming_direction_only_at_stop(self):
        inbox = SimOrderInbox(Path(self.tmp.name) / 'robot.sqlite3', 'sim-r1', 'lab@v1')
        inbox.initialize()
        executor = SimOrderExecutor(inbox, self.map, 'A', experimental_field_map=True, field_incoming_edge='E0')
        self.assertEqual(executor.field_stop_direction(), ('A', 'E0'))
        executor._x = float('nan')
        with self.assertRaises(ValueError):
            executor.field_stop_direction()

    def test_field_runtime_accepts_explicit_private_handoff_store(self):
        runtime = SimRuntime(self.path, self.map, {'sim-r1': {'web-pickup': 'session'}},
                             port=18831, client_id='field-web-test',
                             simulation_store=self.store, field_simulation=True)
        self.assertIs(runtime._simulation_store, self.store)

    def test_workflow_clears_second_visit_not_first_same_named_node(self):
        runtime = Mock()
        runtime._field_simulation = True
        runtime.begin.return_value = {'challenge': 'c'}
        robot = Mock()
        flow = PickupWorkflow(runtime, robot, self.map, {'p': {'pickup_node': 'D'}},
                              simulation_enabled=True, task_id='web-pickup', session='session',
                              robot_id='sim-r1')
        flow._expected_seq = 2
        flow._segment_start = 'B'
        flow._segment_start_index = 1
        state = {'current_node': 'B', 'visit_index': 3}
        order = {'order_id': 'web-pickup', 'seq': 2, 'map_id': 'lab@v1',
                 'nodes': self.nodes, 'released_until': 'B'}
        receipt = {'state': 'completed', 'digest': 'digest'}
        flow._publish_stopped(state, order, receipt)
        self.assertFalse(flow._waiting['final'])
        self.assertEqual(flow._waiting['end_index'], 3)
        sent = robot.publish_event.call_args.args[0]
        self.assertEqual(sent['seq'], 2)
        self.assertIn(['node', 'C'], sent['cleared_resources'])

    def test_product_legs_plan_from_verified_stop_and_arrival_direction(self):
        inbox = SimOrderInbox(Path(self.tmp.name) / 'product-robot.sqlite3', 'sim-r1', 'lab@v1')
        inbox.initialize()
        executor = SimOrderExecutor(inbox, self.map, 'A', experimental_field_map=True, field_incoming_edge='E0')
        robot = Mock(executor=executor)
        runtime = Mock()
        runtime._field_simulation = True
        engine = ProductControlEngine(runtime, robot, self.map, {'p': {'pickup_node': 'B'}})
        first, second = engine._plan_product_legs('A', 'B', 'D')
        self.assertEqual(list(first.nodes), ['A', 'B'])
        self.assertEqual(list(second.nodes), ['B', 'C', 'B', 'D'])

    def test_web_fleet_selects_nearest_direction_valid_field_robot(self):
        class Engine:
            def __init__(self, node, incoming):
                self.node, self.incoming = node, incoming
                self.runtime = Mock()
                self.runtime._field_simulation = True
            def snapshot(self):
                return {'status': 'idle', 'orders_remaining': 5, 'current_node': self.node}
            def _field_route_to(inner, destination):
                return plan_field_route(self.map, map_ref='lab@v1', incoming_edge=inner.incoming, goal=destination)
            def create_product_order(self, *args):
                return {'status': 'moving'}
        fleet = FleetProductControlEngine({'sim-r1': Engine('A', 'E0'), 'sim-r2': Engine('X', 'E5')}, self.map)
        result = fleet.create_product_order('p', 'B', 'D', 'shelf', 0)
        self.assertEqual(result['assigned_robot'], 'sim-r1')

    def test_missing_other_idle_robot_blocks_field_allocation(self):
        self.store._field_required = True
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM sim_idle_robots WHERE robot_id='sim-r2'")
            db.commit()
        with self.assertRaises(ValueError):
            self.store.allocate('web-pickup', 'sim-r1', 'lab@v1', self.nodes,
                self.segment.released_until, self.segment.resources, {'type': 'load'},
                map_fingerprint=self.fingerprint, release_index=self.segment.end_index,
                field_context={'map_data': self.map, 'incoming_edge': 'E0', 'goal': 'D'})
        self.assertIsNone(self.store.get_allocation('web-pickup'))

    def test_altered_other_idle_start_node_blocks_field_allocation(self):
        self.store._field_required = True
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE sim_idle_robots SET node='Y' WHERE robot_id='sim-r2'")
            db.commit()
        with self.assertRaises(ValueError):
            self.store.allocate('web-pickup', 'sim-r1', 'lab@v1', self.nodes,
                self.segment.released_until, self.segment.resources, {'type': 'load'},
                map_fingerprint=self.fingerprint, release_index=self.segment.end_index,
                field_context={'map_data': self.map, 'incoming_edge': 'E0', 'goal': 'D'})
        self.assertIsNone(self.store.get_allocation('web-pickup'))

    def test_reopened_field_store_requires_external_start_positions(self):
        reopened = SimHandoffStore.open_existing(self.path, expected_creation_id=self.store.creation_id,
            expected_idle_enabled=True, simulation_enabled=True)
        with self.assertRaises(ValueError):
            reopened.bind_field_robots(('sim-r1', 'sim-r2'))
        reopened.bind_field_robots(('sim-r1', 'sim-r2'), {'sim-r1': 'A', 'sim-r2': 'X'})
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE sim_idle_robots SET node='Y' WHERE robot_id='sim-r2'")
            db.commit()
        reopened._field_required = True
        with self.assertRaises(ValueError):
            reopened.allocate('web-pickup', 'sim-r1', 'lab@v1', self.nodes,
                self.segment.released_until, self.segment.resources, {'type': 'load'},
                map_fingerprint=self.fingerprint, release_index=self.segment.end_index,
                field_context={'map_data': self.map, 'incoming_edge': 'E0', 'goal': 'D'})

    def test_reopened_field_store_rejects_idle_and_active_node_overlap(self):
        self.store._field_required = True
        allocation = self.store.allocate('web-pickup', 'sim-r1', 'lab@v1', self.nodes,
            self.segment.released_until, self.segment.resources, {'type': 'load'},
            map_fingerprint=self.fingerprint, release_index=self.segment.end_index,
            field_context={'map_data': self.map, 'incoming_edge': 'E0', 'goal': 'D'})
        order = self.store.get_order('web-pickup')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE sim_idle_robots SET node='B' WHERE robot_id='sim-r2'")
            db.commit()
        reopened = SimHandoffStore.open_existing(self.path, expected_creation_id=self.store.creation_id,
            expected_idle_enabled=True, simulation_enabled=True)
        reopened.bind_field_robots(('sim-r1', 'sim-r2'), {'sim-r1': 'A', 'sim-r2': 'B'})
        reopened._field_required = True
        with self.assertRaises(ValueError):
            reopened.mark_attempted('web-pickup', order['payload'], allocation, self.segment.resources)
        self.assertEqual(reopened.get_order('web-pickup')['state'], 'pending')

    def test_missing_idle_robot_blocks_field_publish_and_order_acceptance(self):
        gate = RecoveryGate(self.path, field_required=True)
        gate.initialize()
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True,
                                   recovery_gate=gate, field_simulation=True)
        progress.initialize()
        allocation = self.store.allocate('web-pickup', 'sim-r1', 'lab@v1', self.nodes,
            self.segment.released_until, self.segment.resources, {'type': 'load'},
            map_fingerprint=self.fingerprint, release_index=self.segment.end_index,
            field_context={'map_data': self.map, 'incoming_edge': 'E0', 'goal': 'D'})
        order = self.store.get_order('web-pickup')
        runtime = Runtime(self.path, self.store, progress, gate)
        runtime._tasks = {'web-pickup': ('sim-r1', 'boot-1')}
        runtime.receiver.robot_sessions = {'sim-r1': {'web-pickup': 'boot-1'}}
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM sim_idle_robots WHERE robot_id='sim-r2'")
            db.commit()
        with self.assertRaises(ValueError):
            self.store.mark_attempted('web-pickup', order['payload'], allocation, self.segment.resources)
        self.assertFalse(SimOrderAuthorizer(runtime, robot_id='sim-r1')(order['payload'].encode()))

    def test_missing_idle_robot_rejects_already_published_order(self):
        gate = RecoveryGate(self.path, field_required=True)
        gate.initialize()
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True,
                                   recovery_gate=gate, field_simulation=True)
        progress.initialize()
        allocation = self.store.allocate('web-pickup', 'sim-r1', 'lab@v1', self.nodes,
            self.segment.released_until, self.segment.resources, {'type': 'load'},
            map_fingerprint=self.fingerprint, release_index=self.segment.end_index,
            field_context={'map_data': self.map, 'incoming_edge': 'E0', 'goal': 'D'})
        order = self.store.get_order('web-pickup')
        self.store.mark_attempted('web-pickup', order['payload'], allocation, self.segment.resources)
        runtime = Runtime(self.path, self.store, progress, gate)
        runtime._tasks = {'web-pickup': ('sim-r1', 'boot-1')}
        runtime.receiver.robot_sessions = {'sim-r1': {'web-pickup': 'boot-1'}}
        authorizer = SimOrderAuthorizer(runtime, robot_id='sim-r1')
        self.assertTrue(authorizer(order['payload'].encode()))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM sim_idle_robots WHERE robot_id='sim-r2'")
            db.commit()
        self.assertFalse(authorizer(order['payload'].encode()))

    def test_missing_idle_robot_blocks_progress_transition(self):
        self.store._field_required = True
        gate = RecoveryGate(self.path, field_required=True)
        gate.initialize()
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True,
                                   recovery_gate=gate, field_simulation=True)
        progress.initialize()
        allocation = self.store.allocate('web-pickup', 'sim-r1', 'lab@v1', self.nodes,
            self.segment.released_until, self.segment.resources, {'type': 'load'},
            map_fingerprint=self.fingerprint, release_index=self.segment.end_index,
            field_context={'map_data': self.map, 'incoming_edge': 'E0', 'goal': 'D'})
        order = self.store.get_order('web-pickup')
        self.store.mark_attempted('web-pickup', order['payload'], allocation, self.segment.resources)
        self.store.mark_broker_acked('web-pickup', order['payload'], allocation, self.segment.resources)
        digest = hashlib.sha256(json.dumps(json.loads(order['payload']), sort_keys=True, ensure_ascii=True,
                                          allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        challenge = gate.begin('web-pickup', 'session')
        gate.complete('web-pickup', 'session', challenge, robot_id='sim-r1', order_id='web-pickup',
                      seq=1, map_id='lab@v1', stopped_at='B', order_digest=digest, stopped=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM sim_idle_robots WHERE robot_id='sim-r2'")
            db.commit()
        event = {'type': 'rear_clear', 'event_id': 'e1', 'robot_session': 'session',
                 'order_id': 'web-pickup', 'seq': 1, 'map_id': 'lab@v1', 'stopped_at': 'B',
                 'cleared_resources': [list(x) for x in occurrence_clearance(self.map, self.nodes, 0)]}
        with self.assertRaises(ValueError):
            progress.advance('sim-r1', event)
        self.assertIsNone(progress.get_order('web-pickup', 2))

    def test_completed_pickup_hands_repeated_delivery_to_same_robot_atomically(self):
        gate = RecoveryGate(self.path, field_required=True)
        gate.initialize()
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True,
                                   recovery_gate=gate, field_simulation=True)
        progress.initialize()
        inbox = SimEventInbox(self.path, robot_tasks={'sim-r1': ('web-pickup', 'web-delivery')},
                              simulation_enabled=True)
        inbox.initialize()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='field-handoff-test',
                             clean_session=False, reconnect_on_failure=False)
        receiver = SimEventReceiver(client, progress, inbox=inbox, recovery_gate=gate,
            robot_tasks={'sim-r1': ('web-pickup', 'web-delivery')},
            robot_sessions={'sim-r1': {'web-pickup': 'session', 'web-delivery': 'session'}},
            simulation_enabled=True)
        receiver._ready = True
        first = ['A', 'B']
        first_segment = plan_occurrence_segment(self.map, first, 0)
        allocated = self.store.allocate('web-pickup', 'sim-r1', 'lab@v1', first,
            first_segment.released_until, first_segment.resources, {'type': 'load'},
            map_fingerprint=self.fingerprint,
            field_context={'map_data': self.map, 'incoming_edge': 'E0', 'goal': 'B'})
        order = self.store.get_order('web-pickup')
        self.store.mark_attempted('web-pickup', order['payload'], allocated, first_segment.resources)
        self.store.mark_broker_acked('web-pickup', order['payload'], allocated, first_segment.resources)
        digest = hashlib.sha256(json.dumps(json.loads(order['payload']), sort_keys=True, ensure_ascii=True,
                                          allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        challenge = gate.begin('web-pickup', 'session')
        gate.complete('web-pickup', 'session', challenge, robot_id='sim-r1', order_id='web-pickup',
                      seq=1, map_id='lab@v1', stopped_at='B', order_digest=digest, stopped=True)
        event = {'type': 'rear_clear', 'event_id': 'pickup-final', 'robot_session': 'session',
                 'order_id': 'web-pickup', 'seq': 1, 'map_id': 'lab@v1', 'stopped_at': 'B',
                 'cleared_resources': [list(x) for x in sorted(set(first_segment.resources) - {('node', 'B')})]}
        inbox.store('sim-r1', event)
        self.assertEqual(inbox.retry('sim-r1', 'pickup-final', progress)['status'], 'finished')
        self.store.create_task('web-delivery')
        nodes = list(plan_field_route(self.map, map_ref='lab@v1', incoming_edge='E1', goal='D').nodes)
        self.assertEqual(nodes, ['B', 'C', 'B', 'D'])
        segment = plan_occurrence_segment(self.map, nodes, 0)
        # A shorter but false incoming direction must not replace the pickup's
        # actual final edge or release its existing stop.
        forged = list(plan_field_route(self.map, map_ref='lab@v1', incoming_edge='E3', goal='D').nodes)
        forged_segment = plan_occurrence_segment(self.map, forged, 0)
        before = self.store.list_occupancy()
        with self.assertRaises(ValueError):
            self.store.handoff_allocate(task_id='web-delivery', robot_id='sim-r1', map_id='lab@v1',
                nodes=forged, released_until=forged_segment.released_until, resources=forged_segment.resources,
                action_at_end={'type': 'unload'}, map_fingerprint=self.fingerprint,
                field_context={'map_data': self.map, 'incoming_edge': 'E3', 'goal': 'D'},
                source_task_id='web-pickup', robot_session='session', seq=1, stopped_at='B',
                event_id='pickup-final', action_ack={'order_id': 'web-pickup', 'action_id': 'grab-1',
                    'agv_id': 'AGV_01', 'action': 'grab', 'applied': True}, expected_twin_id='AGV_01',
                gate=gate, progress=progress, receiver=receiver, simulation_enabled=True)
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertIsNone(self.store.get_allocation('web-delivery'))
        result = self.store.handoff_allocate(task_id='web-delivery', robot_id='sim-r1', map_id='lab@v1',
            nodes=nodes, released_until=segment.released_until, resources=segment.resources,
            action_at_end={'type': 'unload'}, map_fingerprint=self.fingerprint,
            release_index=segment.end_index,
            field_context={'map_data': self.map, 'incoming_edge': 'E1', 'goal': 'D'},
            source_task_id='web-pickup', robot_session='session', seq=1, stopped_at='B',
            event_id='pickup-final', action_ack={'order_id': 'web-pickup', 'action_id': 'grab-1',
                'agv_id': 'AGV_01', 'action': 'grab', 'applied': True}, expected_twin_id='AGV_01',
            gate=gate, progress=progress, receiver=receiver, simulation_enabled=True)
        self.assertEqual(result['nodes'], nodes)
        self.assertEqual(result['field_route'], {'incoming_edge': 'E1', 'goal': 'D'})
        self.assertEqual(self.store.list_occupancy()[('node', 'X')], 'sim-r2')
        with self.store._write() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks WHERE task_id=?', ('web-pickup',)).fetchone(), ('DONE',))
            self.assertEqual(db.execute('SELECT current_index FROM occurrence_allocations WHERE task_id=?',
                                        ('web-delivery',)).fetchone(), (segment.end_index,))


if __name__ == '__main__':
    unittest.main()
