"""Repeat visits in the existing simulation allocation database."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from server.allocation_store import AllocationStore, InvalidAllocation
from server.route_occurrence import occurrence_clearance, plan_occurrence_segment
from server.field_map_planner import plan_field_route
from server.tests.test_field_map_planner import sample_map


def map_data():
    points = {'A': [0, 0], 'B': [1, 0], 'C': [0, 1], 'D': [2, 0]}
    return {'map_id': 'repeat', 'version': 1, 'unit': 'mm',
            'nodes': [{'id': key, 'pos': value} for key, value in points.items()],
            'edges': [{'from': a, 'to': b, 'shape': [points[a], points[b]],
                       'bidirectional': True, 'lanes': 1}
                      for a, b in [('A', 'B'), ('A', 'C'), ('C', 'B'), ('B', 'D')]],
            'zones': [{'id': 'wait-b', 'type': 'waiting', 'node': 'B'},
                      {'id': 'wait-d', 'type': 'waiting', 'node': 'D'},
                      {'id': 'shared', 'type': 'shared', 'nodes': ['C']}]}


class RepeatedAllocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AllocationStore(Path(self.tmp.name) / 'state.sqlite3')
        self.store.initialize()
        self.map = map_data()
        self.path = ['A', 'B', 'A', 'C', 'B', 'D']
        self.first = plan_occurrence_segment(self.map, self.path, 0)
        self.fingerprint = hashlib.sha256(json.dumps(self.map, sort_keys=True,
            ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.store.create_task('T1')

    def allocate(self, **kwargs):
        return self.store.allocate('T1', 'sim-r1', 'repeat@v1', self.path, 'B',
            self.first.resources, {'type': 'none'}, map_fingerprint=self.fingerprint, **kwargs)

    def test_physical_repeat_is_rejected_before_reserving_or_creating_an_order(self):
        with self.assertRaisesRegex(InvalidAllocation, 'physical.*repeated'):
            self.store.allocate('T1', 'physical-1', 'repeat@v1', self.path, 'B',
                self.first.resources, {'type': 'none'}, map_fingerprint=self.fingerprint,
                release_index=1)
        self.assertIsNone(self.store.get_allocation('T1'))
        self.assertIsNone(self.store.get_order('T1'))
        self.assertEqual(self.store.list_occupancy(), {})

    def test_physical_unique_path_is_still_allocatable(self):
        route = ['A', 'B']
        first = plan_occurrence_segment(self.map, route, 0)
        result = self.store.allocate('T1', 'physical-1', 'repeat@v1', route, 'B',
            first.resources, {'type': 'none'}, map_fingerprint=self.fingerprint)
        self.assertEqual(result['nodes'], route)
        self.assertEqual(result['robot_id'], 'physical-1')

    def test_repeat_requires_explicit_first_occurrence_and_retains_it(self):
        with self.assertRaises(InvalidAllocation):
            self.allocate()
        for index in (0, 4, True):
            with self.subTest(index=index), self.assertRaises(InvalidAllocation):
                self.allocate(release_index=index)
        allocation = self.allocate(release_index=1)
        self.assertEqual(allocation['nodes'], self.path)
        order = self.store.get_order('T1')
        self.store.mark_attempted('T1', order['payload'], allocation, self.first.resources)
        self.store.mark_broker_acked('T1', order['payload'], allocation, self.first.resources)
        with self.store._write() as db:
            self.assertEqual(db.execute('SELECT current_index FROM occurrence_allocations WHERE task_id=?',
                                        ('T1',)).fetchone(), (1,))

    def test_legacy_unique_route_database_does_not_require_new_tables(self):
        from server.segment_progress import SegmentProgress
        route = ['A', 'B']
        segment = plan_occurrence_segment(self.map, route, 0)
        allocation = self.store.allocate('T1', 'sim-r1', 'repeat@v1', route, 'B', segment.resources,
                                         {'type': 'none'}, map_fingerprint=self.fingerprint)
        payload = self.store.get_order('T1')['payload']
        with self.store._write() as db:
            db.execute('DROP TABLE occurrence_allocations')
            db.execute('DROP TABLE field_route_provenance')
        self.assertEqual(self.store.get_allocation('T1'), allocation)
        self.store.mark_attempted('T1', payload, allocation, segment.resources)
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True)
        with self.store._write() as db:
            self.assertEqual(progress._job(db, 'T1')[1], route)

    def test_progress_uses_visit_index_when_stop_name_repeats(self):
        from server.segment_progress import SegmentProgress
        from server.tests.test_segment_progress import FakeGate
        allocation = self.allocate(release_index=1)
        record = self.store.get_order('T1')
        self.store.mark_attempted('T1', record['payload'], allocation, self.first.resources)
        self.store.mark_broker_acked('T1', record['payload'], allocation, self.first.resources)
        gate = FakeGate(self.store)
        gate.expected = ('T1', 'boot-1', 1, 'B')
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=gate)
        progress.initialize()
        event = {'type': 'rear_clear', 'event_id': 'event-1', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'repeat@v1', 'stopped_at': 'B',
                 'cleared_resources': [list(r) for r in occurrence_clearance(self.map, self.path, 0)]}
        self.assertEqual(progress.advance('sim-r1', event)['next_seq'], 2)
        self.assertEqual(progress.advance('sim-r1', event)['status'], 'duplicate')
        second = plan_occurrence_segment(self.map, self.path, 1)
        self.assertEqual((second.end_index, second.released_until), (4, 'B'))
        self.assertEqual(set(self.store.list_occupancy()), set(second.resources))
        with self.store._write() as db:
            self.assertEqual(db.execute('SELECT current_index FROM occurrence_allocations WHERE task_id=?',
                                        ('T1',)).fetchone(), (4,))
        next_record = progress.get_order('T1', 2)
        progress.mark_attempted('T1', 2, 'boot-1', next_record['payload'], second.resources)
        progress.mark_broker_acked('T1', 2, 'boot-1', next_record['payload'], second.resources)
        gate.expected = ('T1', 'boot-1', 2, 'B')
        event.update(event_id='event-2', seq=2,
                     cleared_resources=[list(r) for r in occurrence_clearance(self.map, self.path, 1)])
        self.assertEqual(progress.advance('sim-r1', event)['next_seq'], 3)
        third = plan_occurrence_segment(self.map, self.path, 4)
        self.assertEqual(set(self.store.list_occupancy()), set(third.resources))

    def test_competing_resource_rolls_back_repeated_visit_index_and_outbox(self):
        from server.segment_progress import SegmentOccupied, SegmentProgress
        from server.tests.test_segment_progress import FakeGate
        allocation = self.allocate(release_index=1)
        record = self.store.get_order('T1')
        self.store.mark_attempted('T1', record['payload'], allocation, self.first.resources)
        self.store.mark_broker_acked('T1', record['payload'], allocation, self.first.resources)
        self.store.create_task('T2')
        self.store.allocate('T2', 'sim-r2', 'repeat@v1', ['C', 'D'], 'D',
            [('node', 'C'), ('node', 'D'), ('edge', '["C","D"]')],
            {'type': 'none'}, map_fingerprint=self.fingerprint)
        gate = FakeGate(self.store)
        gate.expected = ('T1', 'boot-1', 1, 'B')
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=gate)
        progress.initialize()
        event = {'type': 'rear_clear', 'event_id': 'blocked', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'repeat@v1', 'stopped_at': 'B',
                 'cleared_resources': [list(r) for r in occurrence_clearance(self.map, self.path, 0)]}
        before = self.store.list_occupancy()
        with self.assertRaises(SegmentOccupied):
            progress.advance('sim-r1', event)
        self.assertEqual(self.store.list_occupancy(), before)
        self.assertIsNone(progress.get_order('T1', 2))
        with self.store._write() as db:
            self.assertEqual(db.execute('SELECT current_index FROM occurrence_allocations WHERE task_id=?',
                                        ('T1',)).fetchone(), (1,))

    def test_recovery_proof_binds_repeated_visit_index(self):
        from server.recovery import RecoveryError, RecoveryGate
        allocation = self.allocate(release_index=1)
        record = self.store.get_order('T1')
        self.store.mark_attempted('T1', record['payload'], allocation, self.first.resources)
        gate = RecoveryGate(self.store.path)
        gate.initialize()
        challenge = gate.begin('T1', 'boot-1')
        digest = hashlib.sha256(json.dumps(json.loads(record['payload']), sort_keys=True,
            ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        with self.store._write() as db:
            db.execute('UPDATE occurrence_allocations SET current_index=4 WHERE task_id=?', ('T1',))
        with self.assertRaises(RecoveryError):
            gate.complete('T1', 'boot-1', challenge, robot_id='sim-r1', order_id='T1',
                          seq=1, map_id='repeat@v1', stopped_at='B', stopped=True, order_digest=digest)

    def test_repeated_visit_advances_through_real_recovery_gate(self):
        from server.recovery import RecoveryGate, RecoveryError
        from server.segment_progress import SegmentProgress
        allocation = self.allocate(release_index=1)
        initial = self.store.get_order('T1')
        self.store.mark_attempted('T1', initial['payload'], allocation, self.first.resources)
        self.store.mark_broker_acked('T1', initial['payload'], allocation, self.first.resources)
        gate = RecoveryGate(self.store.path)
        gate.initialize()
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=gate)
        progress.initialize()

        def prove(seq, payload):
            challenge = gate.begin('T1', 'boot-1')
            digest = hashlib.sha256(json.dumps(json.loads(payload), sort_keys=True,
                ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
            gate.complete('T1', 'boot-1', challenge, robot_id='sim-r1', order_id='T1',
                          seq=seq, map_id='repeat@v1', stopped_at='B', stopped=True, order_digest=digest)

        prove(1, initial['payload'])
        first = {'type': 'rear_clear', 'event_id': 'first', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'repeat@v1', 'stopped_at': 'B',
                 'cleared_resources': [list(r) for r in occurrence_clearance(self.map, self.path, 0)]}
        self.assertEqual(progress.advance('sim-r1', first)['next_seq'], 2)
        second = plan_occurrence_segment(self.map, self.path, 1)
        record = progress.get_order('T1', 2)
        progress.mark_attempted('T1', 2, 'boot-1', record['payload'], second.resources)
        progress.mark_broker_acked('T1', 2, 'boot-1', record['payload'], second.resources)
        second_event = dict(first, event_id='second', seq=2,
            cleared_resources=[list(r) for r in occurrence_clearance(self.map, self.path, 1)])
        with self.assertRaises(RecoveryError):
            progress.advance('sim-r1', second_event)
        self.assertEqual(set(self.store.list_occupancy()), set(second.resources))
        prove(2, record['payload'])
        self.assertEqual(progress.advance('sim-r1', second_event)['next_seq'], 3)

    def test_field_provenance_is_bound_to_initial_order_and_rechecked_before_publish(self):
        from server.allocation_store import AllocationConflict
        field = sample_map()
        for edge in field['edges']:
            edge['lanes'] = 1
        field['zones'] = [{'id': 'wait-c', 'type': 'waiting', 'node': 'C'},
                          {'id': 'wait-a', 'type': 'waiting', 'node': 'A'}]
        route = list(plan_field_route(field, map_ref='field-test@v1', incoming_edge='E1', goal='A').nodes)
        self.assertEqual(route, ['B', 'C', 'B', 'A'])
        segment = plan_occurrence_segment(field, route, 0)
        fingerprint = hashlib.sha256(json.dumps(field, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        context = {'map_data': field, 'incoming_edge': 'E1', 'goal': 'A'}
        with self.assertRaises((InvalidAllocation, ValueError)):
            self.store.allocate('T1', 'sim-r1', 'field-test@v1', route, 'C', segment.resources,
                                {'type': 'none'}, map_fingerprint='b' * 64,
                                release_index=1, field_context=context)
        allocation = self.store.allocate('T1', 'sim-r1', 'field-test@v1', route, 'C', segment.resources,
                                         {'type': 'none'}, map_fingerprint=fingerprint,
                                         release_index=1, field_context=context)
        record = self.store.get_order('T1')
        with self.store._write() as db:
            db.execute('UPDATE field_route_provenance SET incoming_edge=? WHERE task_id=?', ('E3', 'T1'))
        with self.assertRaises(AllocationConflict):
            self.store.mark_attempted('T1', record['payload'], allocation, segment.resources)
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')

    def test_field_progress_requires_provenance_and_explicit_simulation_opt_in(self):
        from server.allocation_store import AllocationConflict
        from server.segment_progress import SegmentProgress
        field = sample_map()
        for edge in field['edges']:
            edge['lanes'] = 1
        field['zones'] = [{'id': 'wait-c', 'type': 'waiting', 'node': 'C'},
                          {'id': 'wait-a', 'type': 'waiting', 'node': 'A'}]
        route = list(plan_field_route(field, map_ref='field-test@v1', incoming_edge='E1', goal='A').nodes)
        segment = plan_occurrence_segment(field, route, 0)
        fingerprint = hashlib.sha256(json.dumps(field, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.store.allocate('T1', 'sim-r1', 'field-test@v1', route, 'C', segment.resources,
            {'type': 'none'}, map_fingerprint=fingerprint, release_index=1,
            field_context={'map_data': field, 'incoming_edge': 'E1', 'goal': 'A'})
        with self.assertRaises(ValueError):
            SegmentProgress(self.store, field, simulation_enabled=True)
        with self.assertRaises(ValueError):
            SegmentProgress(self.store, field, field_simulation=True)
        progress = SegmentProgress(self.store, field, simulation_enabled=True, field_simulation=True)
        with self.store._write() as db:
            self.assertEqual(progress._job(db, 'T1')[1], route)
            db.execute('DELETE FROM field_route_provenance WHERE task_id=?', ('T1',))
        with self.store._write() as db:
            with self.assertRaises(AllocationConflict):
                progress._job(db, 'T1')

    def test_field_progress_rejects_deleted_required_mode_marker(self):
        from server.allocation_store import AllocationConflict
        from server.segment_progress import SegmentProgress
        field = sample_map()
        for edge in field['edges']:
            edge['lanes'] = 1
        field['zones'] = [{'id': 'wait-c', 'type': 'waiting', 'node': 'C'},
                          {'id': 'wait-a', 'type': 'waiting', 'node': 'A'}]
        route = list(plan_field_route(field, map_ref='field-test@v1', incoming_edge='E1', goal='A').nodes)
        segment = plan_occurrence_segment(field, route, 0)
        fingerprint = hashlib.sha256(json.dumps(field, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.store.allocate('T1', 'sim-r1', 'field-test@v1', route, 'C', segment.resources,
            {'type': 'none'}, map_fingerprint=fingerprint, release_index=1,
            field_context={'map_data': field, 'incoming_edge': 'E1', 'goal': 'A'})
        progress = SegmentProgress(self.store, field, simulation_enabled=True, field_simulation=True)
        with self.store._write() as db:
            db.execute("DELETE FROM allocation_meta WHERE key='field_route_mode'")
        with self.store._write() as db:
            with self.assertRaises(AllocationConflict):
                progress._job(db, 'T1')

    def test_field_allocation_rejects_missing_other_robot_occupancy(self):
        from server.allocation_store import AllocationConflict
        field = sample_map()
        for edge in field['edges']:
            edge['lanes'] = 1
        field['zones'] = [{'id': 'wait-c', 'type': 'waiting', 'node': 'C'},
                          {'id': 'wait-d', 'type': 'waiting', 'node': 'D'}]
        fingerprint = hashlib.sha256(json.dumps(field, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        first = list(plan_field_route(field, map_ref='field-test@v1', incoming_edge='E1', goal='C').nodes)
        segment = plan_occurrence_segment(field, first, 0)
        self.store.allocate('T1', 'sim-r1', 'field-test@v1', first, 'C', segment.resources,
            {'type': 'none'}, map_fingerprint=fingerprint,
            field_context={'map_data': field, 'incoming_edge': 'E1', 'goal': 'C'})
        with self.store._write() as db:
            db.execute("DELETE FROM allocation_occupancy WHERE task_id='T1'")
        self.store.create_task('T2')
        second = list(plan_field_route(field, map_ref='field-test@v1', incoming_edge='E3', goal='D').nodes)
        segment2 = plan_occurrence_segment(field, second, 0)
        with self.assertRaises(AllocationConflict):
            self.store.allocate('T2', 'sim-r2', 'field-test@v1', second, 'D', segment2.resources,
                {'type': 'none'}, map_fingerprint=fingerprint,
                field_context={'map_data': field, 'incoming_edge': 'E3', 'goal': 'D'})
        self.assertIsNone(self.store.get_allocation('T2'))

    def test_deleted_field_marker_cannot_downgrade_database_to_generic_mode(self):
        from server.allocation_store import AllocationConflict
        field = sample_map()
        for edge in field['edges']:
            edge['lanes'] = 1
        field['zones'] = [{'id': 'wait-c', 'type': 'waiting', 'node': 'C'},
                          {'id': 'wait-d', 'type': 'waiting', 'node': 'D'}]
        fingerprint = hashlib.sha256(json.dumps(field, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        first = list(plan_field_route(field, map_ref='field-test@v1', incoming_edge='E1', goal='C').nodes)
        segment = plan_occurrence_segment(field, first, 0)
        self.store.allocate('T1', 'sim-r1', 'field-test@v1', first, 'C', segment.resources,
            {'type': 'none'}, map_fingerprint=fingerprint,
            field_context={'map_data': field, 'incoming_edge': 'E1', 'goal': 'C'})
        with self.store._write() as db:
            db.execute("DELETE FROM allocation_meta WHERE key='field_route_mode'")
            db.execute("DELETE FROM allocation_occupancy WHERE task_id='T1'")
        self.store.create_task('T2')
        with self.assertRaisesRegex(AllocationConflict, 'mode is missing'):
            self.store.allocate('T2', 'sim-r2', 'field-test@v1', ['B', 'C'], 'C', segment.resources,
                                {'type': 'none'}, map_fingerprint=fingerprint)
        self.assertIsNone(self.store.get_allocation('T2'))

    def test_field_required_store_rejects_marker_and_provenance_both_deleted(self):
        from server.allocation_store import AllocationConflict
        from server.recovery import RecoveryError, RecoveryGate
        self.store = AllocationStore(self.store.path, field_required=True)
        self.store.initialize()
        field = sample_map()
        for edge in field['edges']:
            edge['lanes'] = 1
        field['zones'] = [{'id': 'wait-c', 'type': 'waiting', 'node': 'C'}]
        fingerprint = hashlib.sha256(json.dumps(field, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        route = list(plan_field_route(field, map_ref='field-test@v1', incoming_edge='E1', goal='C').nodes)
        segment = plan_occurrence_segment(field, route, 0)
        allocation = self.store.allocate('T1', 'sim-r1', 'field-test@v1', route, 'C', segment.resources,
            {'type': 'none'}, map_fingerprint=fingerprint,
            field_context={'map_data': field, 'incoming_edge': 'E1', 'goal': 'C'})
        record = self.store.get_order('T1')
        self.store.mark_attempted('T1', record['payload'], allocation, segment.resources)
        with self.store._write() as db:
            db.execute("DELETE FROM allocation_meta WHERE key='field_route_mode'")
            db.execute("DELETE FROM field_route_provenance WHERE task_id='T1'")
        with self.assertRaises(AllocationConflict):
            self.store.mark_broker_acked('T1', record['payload'], allocation, segment.resources)
        self.store.create_task('T2')
        with self.assertRaises(AllocationConflict):
            self.store.allocate('T2', 'sim-r2', 'field-test@v1', route, 'C', segment.resources,
                                {'type': 'none'}, map_fingerprint=fingerprint)
        gate = RecoveryGate(self.store.path, field_required=True)
        gate.initialize()
        with self.assertRaisesRegex(RecoveryError, 'field route mode'):
            gate.begin('T1', 'boot-1')

    def test_field_authorization_rejects_another_tasks_missing_occupancy(self):
        from server.recovery import RecoveryGate
        from server.segment_progress import SegmentProgress
        from server.sim_order_authorization import SimOrderAuthorizer
        from server.tests.test_sim_order_authorization import Runtime
        from server.tests.test_occurrence_allocation_lab import laboratory_map

        data = laboratory_map()
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.store = AllocationStore(self.store.path, field_required=True)
        gate = RecoveryGate(self.store.path, field_required=True)
        gate.initialize()
        progress = SegmentProgress(self.store, data, simulation_enabled=True,
                                   field_simulation=True, recovery_gate=gate)
        progress.initialize()
        self.store.create_task('T2')
        records = {}
        for task, robot, edge, goal in [('T1', 'sim-r1', 'E0', 'D'),
                                        ('T2', 'sim-r2', 'E5', 'Y')]:
            route = list(plan_field_route(data, map_ref='lab@v1', incoming_edge=edge, goal=goal).nodes)
            segment = plan_occurrence_segment(data, route, 0)
            allocation = self.store.allocate(task, robot, 'lab@v1', route, segment.released_until,
                segment.resources, {'type': 'none'}, map_fingerprint=fingerprint,
                release_index=segment.end_index if len(set(route)) != len(route) else None,
                field_context={'map_data': data, 'incoming_edge': edge, 'goal': goal})
            record = self.store.get_order(task)
            self.store.mark_attempted(task, record['payload'], allocation, segment.resources)
            records[task] = record['payload'].encode('utf-8')
        runtime = Runtime(self.store.path, self.store, progress, gate)
        runtime._tasks['T2'] = ('sim-r2', 'boot-2')
        runtime.receiver.robot_sessions['sim-r2'] = {'T2': 'boot-2'}
        authorizer = SimOrderAuthorizer(runtime, robot_id='sim-r2')
        self.assertTrue(authorizer(records['T2']))
        with self.store._write() as db:
            db.execute("DELETE FROM allocation_occupancy WHERE task_id='T1'")
        self.assertFalse(authorizer(records['T2']))

    def test_field_recovery_proof_rejects_changed_planner_provenance(self):
        from server.recovery import RecoveryError, RecoveryGate
        field = sample_map()
        for edge in field['edges']:
            edge['lanes'] = 1
        field['zones'] = [{'id': 'wait-c', 'type': 'waiting', 'node': 'C'},
                          {'id': 'wait-a', 'type': 'waiting', 'node': 'A'}]
        route = list(plan_field_route(field, map_ref='field-test@v1', incoming_edge='E1', goal='A').nodes)
        segment = plan_occurrence_segment(field, route, 0)
        fingerprint = hashlib.sha256(json.dumps(field, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        allocation = self.store.allocate('T1', 'sim-r1', 'field-test@v1', route, 'C', segment.resources,
            {'type': 'none'}, map_fingerprint=fingerprint, release_index=1,
            field_context={'map_data': field, 'incoming_edge': 'E1', 'goal': 'A'})
        record = self.store.get_order('T1')
        self.store.mark_attempted('T1', record['payload'], allocation, segment.resources)
        gate = RecoveryGate(self.store.path)
        gate.initialize()
        challenge = gate.begin('T1', 'boot-1')
        digest = hashlib.sha256(json.dumps(json.loads(record['payload']), sort_keys=True,
            ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        with self.store._write() as db:
            db.execute('UPDATE field_route_provenance SET incoming_edge=? WHERE task_id=?', ('E3', 'T1'))
        with self.assertRaises(RecoveryError):
            gate.complete('T1', 'boot-1', challenge, robot_id='sim-r1', order_id='T1',
                          seq=1, map_id='field-test@v1', stopped_at='C', stopped=True, order_digest=digest)

    def test_field_recovery_rejects_missing_provenance_before_new_proof(self):
        from server.recovery import RecoveryError, RecoveryGate
        field = sample_map()
        for edge in field['edges']:
            edge['lanes'] = 1
        field['zones'] = [{'id': 'wait-c', 'type': 'waiting', 'node': 'C'},
                          {'id': 'wait-a', 'type': 'waiting', 'node': 'A'}]
        route = list(plan_field_route(field, map_ref='field-test@v1', incoming_edge='E1', goal='A').nodes)
        segment = plan_occurrence_segment(field, route, 0)
        fingerprint = hashlib.sha256(json.dumps(field, sort_keys=True, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        allocation = self.store.allocate('T1', 'sim-r1', 'field-test@v1', route, 'C', segment.resources,
            {'type': 'none'}, map_fingerprint=fingerprint, release_index=1,
            field_context={'map_data': field, 'incoming_edge': 'E1', 'goal': 'A'})
        record = self.store.get_order('T1')
        self.store.mark_attempted('T1', record['payload'], allocation, segment.resources)
        gate = RecoveryGate(self.store.path)
        gate.initialize()
        with self.store._write() as db:
            db.execute('DELETE FROM field_route_provenance WHERE task_id=?', ('T1',))
        with self.assertRaises(RecoveryError):
            gate.begin('T1', 'boot-1')


if __name__ == '__main__':
    unittest.main()
