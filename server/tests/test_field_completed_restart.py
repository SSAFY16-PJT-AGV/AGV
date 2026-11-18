"""Rebuild a stopped field-simulation pose from durable route and visit history."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import paho.mqtt.client as mqtt

from server.recovery import RecoveryGate
from server.route_occurrence import occurrence_clearance, plan_occurrence_segment
from server.segment_progress import SegmentProgress
from server.sim_handoff_store import SimHandoffStore
from server.sim_runtime import SimRuntime
from server.tests.test_occurrence_allocation_lab import laboratory_map
from sim.event_inbox import SimEventInbox
from sim.event_receiver import SimEventReceiver
from sim.order_executor import SimOrderExecutor
from sim.order_inbox import SimOrderInbox


class FieldCompletedRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.path = root / 'server.sqlite3'
        self.data = laboratory_map()
        self.starts = {'sim-r1': 'A', 'sim-r2': 'X'}
        self.store = SimHandoffStore.create_new(self.path, simulation_enabled=True, idle_positions=self.starts)
        self.store._field_required = True
        fingerprint = hashlib.sha256(json.dumps(self.data, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.gate = RecoveryGate(self.path, field_required=True)
        self.gate.initialize()
        self.progress = SegmentProgress(self.store, self.data, simulation_enabled=True,
                                        recovery_gate=self.gate, field_simulation=True)
        self.progress.initialize()
        self.events = SimEventInbox(self.path, robot_tasks={'sim-r1': ('delivery',)}, simulation_enabled=True)
        self.events.initialize()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='field-restart-unit',
                             clean_session=False, reconnect_on_failure=False)
        self.receiver = SimEventReceiver(client, self.progress, inbox=self.events, recovery_gate=self.gate,
            robot_tasks={'sim-r1': ('delivery',)}, robot_sessions={'sim-r1': {'delivery': 'session'}},
            simulation_enabled=True)
        self.receiver._ready = True
        self.robot_inbox = SimOrderInbox(root / 'robot.sqlite3', 'sim-r1', 'lab@v1')
        self.robot_inbox.initialize()
        original = SimOrderExecutor(self.robot_inbox, self.data, 'A', experimental_field_map=True,
                                    field_incoming_edge='E0')
        route = ['A', 'B', 'C', 'B', 'D']
        segment = plan_occurrence_segment(self.data, route, 0)
        self.store.create_task('delivery')
        allocation = self.store.allocate('delivery', 'sim-r1', 'lab@v1', route, segment.released_until,
            segment.resources, {'type': 'unload'}, map_fingerprint=fingerprint, release_index=segment.end_index,
            field_context={'map_data': self.data, 'incoming_edge': 'E0', 'goal': 'D'})
        order = self.store.get_order('delivery')
        self.store.mark_attempted('delivery', order['payload'], allocation, segment.resources)
        self.store.mark_broker_acked('delivery', order['payload'], allocation, segment.resources)
        start = 0
        for seq in range(1, 4):
            original.accept(order['payload'].encode())
            state = original.tick(1)
            receipt = state['receipt']
            token = self.gate.begin('delivery', 'session')
            self.gate.complete('delivery', 'session', token, robot_id='sim-r1', order_id='delivery', seq=seq,
                map_id='lab@v1', stopped_at=state['current_node'], order_digest=receipt['digest'], stopped=True)
            event = {'type': 'rear_clear', 'event_id': 'e{}'.format(seq), 'robot_session': 'session',
                     'order_id': 'delivery', 'seq': seq, 'map_id': 'lab@v1', 'stopped_at': state['current_node'],
                     'cleared_resources': [list(x) for x in occurrence_clearance(self.data, route, start)]}
            self.events.store('sim-r1', event)
            result = self.events.retry('sim-r1', event['event_id'], self.progress)
            if seq < 3:
                self.assertEqual(result['status'], 'advanced')
                start = state['visit_index']
                segment = plan_occurrence_segment(self.data, route, start)
                order = self.progress.get_order('delivery', seq + 1)
                self.progress.mark_attempted('delivery', seq + 1, 'session', order['payload'], segment.resources)
                self.progress.mark_broker_acked('delivery', seq + 1, 'session', order['payload'], segment.resources)
            else:
                self.assertEqual(result['status'], 'finished')
        self.previous = original.snapshot()
        reopened = SimHandoffStore.open_existing(self.path, expected_creation_id=self.store.creation_id,
                                                expected_idle_enabled=True, simulation_enabled=True)
        reopened.bind_field_robots(tuple(self.starts), self.starts)
        reopened._field_required = True
        self.runtime = SimRuntime(self.path, self.data,
            {'sim-r1': {'delivery': 'session'}, 'sim-r2': {'other': 'session-2'}}, port=18831,
            client_id='field-restart-test', simulation_store=reopened, field_simulation=True,
            field_initial_positions=self.starts)
        self.runtime.store, self.runtime.progress = reopened, self.progress
        self.runtime.gate, self.runtime.receiver, self.runtime._started = self.gate, self.receiver, True
        self.gate.invalidate_many(('delivery',))
        self.executor = SimOrderExecutor(self.robot_inbox, self.data, 'A', experimental_field_map=True,
                                         field_incoming_edge='E0')
        self.ack = {'order_id': 'delivery', 'action_id': 'place-1', 'agv_id': 'AGV_01',
                    'action': 'place', 'applied': True}

    def restore(self):
        return self.runtime.restore_completed_executor(self.executor, task_id='delivery', session='session',
            event_id='e3', action_ack=self.ack, expected_twin_id='AGV_01')

    def test_repeated_terminal_route_restores_pose_direction_and_visit(self):
        self.restore()
        state = self.executor.snapshot()
        self.assertEqual(state['current_node'], 'D')
        self.assertEqual(state['visit_index'], 4)
        self.assertEqual(state['pose'], self.previous['pose'])
        self.assertEqual(self.executor.field_stop_direction(), ('D', 'E4'))
        self.assertEqual(self.executor.tick(1), state)
        self.assertIn('delivery', self.gate._blocked)

    def test_changed_route_provenance_does_not_restore(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE field_route_provenance SET incoming_edge='E3' WHERE task_id='delivery'")
            db.commit()
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(self.executor.snapshot()['status'], 'idle')

    def test_missing_prior_visit_event_does_not_restore(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM progress_events WHERE task_id='delivery' AND seq=2")
            db.commit()
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(self.executor.snapshot()['status'], 'idle')

    def test_unconfirmed_place_does_not_restore(self):
        self.ack['applied'] = False
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(self.executor.snapshot()['status'], 'idle')

    def test_missing_parked_robot_ownership_does_not_restore(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM sim_idle_robots WHERE robot_id='sim-r2'")
            db.commit()
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(self.executor.snapshot()['status'], 'idle')

    def test_different_executor_map_does_not_restore(self):
        other_map = dict(self.data, untrusted_metadata='changed')
        self.executor = SimOrderExecutor(self.robot_inbox, other_map, 'A', experimental_field_map=True,
                                         field_incoming_edge='E0')
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(self.executor.snapshot()['status'], 'idle')


if __name__ == '__main__':
    unittest.main()
