"""Crash-gap recovery must distinguish repeated waiting-node occurrences."""
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from server.allocation_store import AllocationConflict
from server.route_occurrence import occurrence_clearance, plan_occurrence_segment
from server.segment_progress import SegmentProgress
from server.tests import test_repeated_allocation_integration as fixture_module
from server.tests.test_segment_progress import FakeGate
from sim.event_inbox import SimEventInbox


class RepeatedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.RepeatedAllocationTests(
            'test_progress_uses_visit_index_when_stop_name_repeats')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        # Three intermediate releases: B, A, B. The second B is not the
        # first B in the route, and A supplies an intervening history row.
        f.map['zones'].append({'id': 'wait-a', 'type': 'waiting', 'node': 'A'})
        f.first = plan_occurrence_segment(f.map, f.path, 0)
        f.fingerprint = hashlib.sha256(json.dumps(
            f.map, sort_keys=True, ensure_ascii=True, allow_nan=False,
            separators=(',', ':')).encode('ascii')).hexdigest()
        allocation = f.allocate(release_index=1)
        record = f.store.get_order('T1')
        f.store.mark_attempted('T1', record['payload'], allocation, f.first.resources)
        f.store.mark_broker_acked('T1', record['payload'], allocation, f.first.resources)
        self.gate = FakeGate(f.store)
        self.progress = SegmentProgress(f.store, f.map, simulation_enabled=True, recovery_gate=self.gate)
        self.progress.initialize()
        for seq, start in ((1, 0), (2, 1)):
            segment = plan_occurrence_segment(f.map, f.path, start)
            self.gate.expected = ('T1', 'boot-1', seq, segment.released_until)
            event = self.event(seq, start, segment.released_until)
            result = self.progress.advance('sim-r1', event)
            next_segment = plan_occurrence_segment(f.map, f.path, segment.end_index)
            self.progress.mark_attempted('T1', seq + 1, 'boot-1', result['payload'], next_segment.resources)
            self.progress.mark_broker_acked('T1', seq + 1, 'boot-1', result['payload'], next_segment.resources)
        self.gate.expected = ('T1', 'boot-1', 3, 'B')
        self.inbox = SimEventInbox(f.store.path, robot_tasks={'sim-r1': ['T1']}, simulation_enabled=True)
        self.inbox.initialize()
        self.inbox.store('sim-r1', self.event(3, 2, 'B'))
        with patch.object(self.inbox, '_mark_applied', side_effect=sqlite3.OperationalError('crash')):
            with self.assertRaises(sqlite3.OperationalError):
                self.inbox.retry('sim-r1', 'event-3', self.progress)

    def event(self, seq, start, stopped_at):
        f = self.fixture
        return {'type': 'rear_clear', 'event_id': 'event-%d' % seq,
                'robot_session': 'boot-1', 'order_id': 'T1', 'seq': seq,
                'map_id': 'repeat@v1', 'stopped_at': stopped_at,
                'cleared_resources': [list(r) for r in occurrence_clearance(f.map, f.path, start)]}

    def test_retry_after_repeated_stop_commit_gap_preserves_occupancy(self):
        before = self.fixture.store.list_occupancy()
        result = self.inbox.retry('sim-r1', 'event-3', self.progress)
        self.assertEqual((result['status'], result['next_seq']), ('duplicate', 4))
        self.assertEqual(json.loads(result['payload'])['released_until'], 'D')
        self.assertEqual(self.inbox.get('sim-r1', 'event-3')['state'], 'applied')
        self.assertEqual(self.fixture.store.list_occupancy(), before)

    def test_retry_rejects_missing_intermediate_order(self):
        before = self.fixture.store.list_occupancy()
        with sqlite3.connect(self.fixture.store.path) as db:
            db.execute("DELETE FROM progress_orders WHERE task_id='T1' AND seq=2")
        with self.assertRaises(AllocationConflict):
            self.inbox.retry('sim-r1', 'event-3', self.progress)
        self.assertEqual(self.inbox.get('sim-r1', 'event-3')['state'], 'pending')
        self.assertEqual(self.fixture.store.list_occupancy(), before)

    def test_retry_rejects_changed_intermediate_release(self):
        before = self.fixture.store.list_occupancy()
        with sqlite3.connect(self.fixture.store.path) as db:
            payload = db.execute("SELECT payload FROM progress_orders WHERE task_id='T1' AND seq=2").fetchone()[0]
            changed = json.loads(payload)
            changed['released_until'] = 'B'
            db.execute("UPDATE progress_orders SET payload=? WHERE task_id='T1' AND seq=2", (json.dumps(changed),))
        with self.assertRaises(AllocationConflict):
            self.inbox.retry('sim-r1', 'event-3', self.progress)
        self.assertEqual(self.inbox.get('sim-r1', 'event-3')['state'], 'pending')
        self.assertEqual(self.fixture.store.list_occupancy(), before)


if __name__ == '__main__':
    unittest.main()
