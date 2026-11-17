import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from server.order_pickup_inventory import PickupInventory, PickupUnavailable


class PickupInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'pickup.sqlite3'
        self.sources = {'D01': 'Red', 'D06': 'Blue'}
        self.world = 'ab' * 16
        self.store = PickupInventory.create_new(self.path, self.sources)
        self.token = self.store.begin_session(self.world)

    def snapshot(self, red=1, blue=2):
        return [{'source_id': 'D01', 'product_type': 'Red', 'available': red},
                {'source_id': 'D06', 'product_type': 'Blue', 'available': blue}]

    def reserve(self, request='request-1'):
        return self.store.reserve(self.token, request, 'D01', 'Red')

    def apply(self, store, token, snapshot):
        return store.apply_snapshot(token, snapshot, snapshot_fence=store.begin_snapshot(token))

    def test_restart_requires_fresh_snapshot_and_preserves_reservation(self):
        with self.assertRaises(PickupUnavailable):
            self.reserve()
        self.apply(self.store, self.token, self.snapshot())
        held = self.reserve()
        reopened = PickupInventory.open_existing(self.path, self.sources, expected_creation_id=self.store.creation_id)
        self.assertFalse(reopened.snapshot()['fresh'])
        new_token = reopened.begin_session(self.world)
        with self.assertRaises(PickupUnavailable):
            reopened.reserve(new_token, 'request-2', 'D01', 'Red')
        self.apply(reopened, new_token, self.snapshot())
        with self.assertRaises(PickupUnavailable):
            reopened.reserve(new_token, 'request-2', 'D01', 'Red')
        self.assertEqual(reopened.snapshot()['reservations'][0]['reservation_id'], held['reservation_id'])
        with self.assertRaises(ValueError):
            reopened.begin_session('cd' * 16)

    def test_concurrent_reservations_cannot_exceed_available_stock(self):
        self.apply(self.store, self.token, self.snapshot(red=2))
        def run(i):
            try:
                return self.store.reserve(self.token, 'request-' + str(i), 'D01', 'Red')
            except PickupUnavailable:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, range(8)))
        self.assertEqual(len([r for r in results if r]), 2)
        self.assertEqual(self.store.snapshot()['sources']['D01']['allocatable'], 0)

    def test_ack_correlation_decrements_once_and_failed_grab_releases(self):
        self.apply(self.store, self.token, self.snapshot(red=2))
        reservation = self.reserve()
        rid = reservation['reservation_id']
        self.store.mark_dispatched(self.token, rid, 'leg-1', 'action-1', 'AGV_01')
        with self.assertRaises(ValueError):
            self.store.confirm(self.token, rid, 'leg-1', 'wrong', 'AGV_01', applied=True)
        self.store.confirm(self.token, rid, 'leg-1', 'action-1', 'AGV_01', applied=True)
        self.store.confirm(self.token, rid, 'leg-1', 'action-1', 'AGV_01', applied=True)
        source = self.store.snapshot()['sources']['D01']
        self.assertEqual((source['available'], source['allocatable']), (1, 1))
        second = self.reserve('request-2')['reservation_id']
        self.store.mark_dispatched(self.token, second, 'leg-2', 'action-2', 'AGV_02')
        self.store.confirm(self.token, second, 'leg-2', 'action-2', 'AGV_02', applied=False)
        self.assertFalse(self.store.snapshot()['fresh'])
        self.apply(self.store, self.token, self.snapshot(red=1))
        self.assertEqual(self.store.snapshot()['sources']['D01']['allocatable'], 1)

    def test_disconnect_or_snapshot_race_keeps_uncertain_reservation(self):
        self.apply(self.store, self.token, self.snapshot())
        rid = self.reserve()['reservation_id']
        self.store.mark_dispatched(self.token, rid, 'leg-1', 'action-1', 'AGV_01')
        self.store.end_session(self.token)
        reopened = PickupInventory.open_existing(self.path, self.sources, expected_creation_id=self.store.creation_id)
        new_token = reopened.begin_session(self.world)
        self.apply(reopened, new_token, self.snapshot(red=0))
        self.assertEqual(reopened.snapshot()['reservations'][0]['state'], 'unknown')
        with self.assertRaises(PickupUnavailable):
            reopened.reserve(new_token, 'request-2', 'D01', 'Red')
        with self.assertRaises(ValueError):
            reopened.confirm(new_token, rid, 'leg-1', 'action-1', 'AGV_01', applied=True)

    def test_snapshot_while_command_inflight_makes_success_uncertain(self):
        self.apply(self.store, self.token, self.snapshot(red=2))
        rid = self.reserve()['reservation_id']
        self.store.mark_dispatched(self.token, rid, 'leg-1', 'action-1', 'AGV_01')
        self.apply(self.store, self.token, self.snapshot(red=1))
        with self.assertRaises(PickupUnavailable):
            self.store.confirm(self.token, rid, 'leg-1', 'action-1', 'AGV_01', applied=True)
        self.assertEqual(self.store.snapshot()['reservations'][0]['state'], 'unknown')
        self.assertFalse(self.store.snapshot()['fresh'])

    def test_invalid_partial_or_wrong_source_snapshot_rejected(self):
        for bad in (self.snapshot()[:1], self.snapshot(red=True),
                    [dict(self.snapshot()[0], product_type='Blue'), self.snapshot()[1]]):
            with self.subTest(snapshot=bad):
                with self.assertRaises(ValueError):
                    self.apply(self.store, self.token, bad)
        self.assertFalse(self.store.snapshot()['fresh'])

    def test_stale_snapshot_response_cannot_undo_confirmed_grab(self):
        self.apply(self.store, self.token, self.snapshot(red=2))
        old_fence = self.store.begin_snapshot(self.token)
        old_snapshot = self.snapshot(red=2)
        rid = self.reserve()['reservation_id']
        self.store.mark_dispatched(self.token, rid, 'leg-1', 'action-1', 'AGV_01')
        self.store.confirm(self.token, rid, 'leg-1', 'action-1', 'AGV_01', applied=True)
        with self.assertRaises(PickupUnavailable):
            self.store.apply_snapshot(self.token, old_snapshot, snapshot_fence=old_fence)
        self.assertEqual(self.store.snapshot()['sources']['D01']['available'], 1)
        self.apply(self.store, self.token, self.snapshot(red=1))
        self.assertEqual(self.store.snapshot()['sources']['D01']['available'], 1)

    def test_external_stock_drop_blocks_dispatch_of_existing_reservations(self):
        self.apply(self.store, self.token, self.snapshot(red=2))
        first = self.reserve()['reservation_id']
        self.reserve('request-2')
        self.apply(self.store, self.token, self.snapshot(red=1))
        with self.assertRaises(PickupUnavailable):
            self.store.mark_dispatched(self.token, first, 'leg-1', 'action-1', 'AGV_01')
        self.assertEqual(self.store.snapshot()['reservations'][0]['state'], 'reserved')

    def test_unanswered_action_is_unknown_without_releasing_stock(self):
        self.apply(self.store, self.token, self.snapshot())
        rid = self.reserve()['reservation_id']
        self.store.mark_dispatched(self.token, rid, 'leg-1', 'action-1', 'AGV_01')
        self.store.mark_unknown(self.token, rid)
        self.assertEqual(self.store.snapshot()['reservations'][0]['state'], 'unknown')
        self.assertFalse(self.store.snapshot()['fresh'])
        self.apply(self.store, self.token, self.snapshot())
        with self.assertRaises(PickupUnavailable):
            self.reserve('request-2')

    def test_crash_before_ack_and_after_confirm_preserve_conservative_counts(self):
        self.apply(self.store, self.token, self.snapshot(red=2))
        first = self.reserve()['reservation_id']
        self.store.mark_dispatched(self.token, first, 'leg-1', 'action-1', 'AGV_01')
        self.store.confirm(self.token, first, 'leg-1', 'action-1', 'AGV_01', applied=True)
        second = self.reserve('request-2')['reservation_id']
        self.store.mark_dispatched(self.token, second, 'leg-2', 'action-2', 'AGV_02')
        # Simulate process death: no explicit end_session or ACK is delivered.
        reopened = PickupInventory.open_existing(self.path, self.sources, expected_creation_id=self.store.creation_id)
        self.assertEqual(reopened.snapshot()['sources']['D01']['available'], 1)
        self.assertFalse(reopened.snapshot()['fresh'])
        new_token = reopened.begin_session(self.world)
        self.assertEqual(reopened.snapshot()['reservations'][1]['state'], 'unknown')
        with self.assertRaises(PickupUnavailable):
            self.store.reserve(self.token, 'old-process', 'D01', 'Red')
        self.apply(reopened, new_token, self.snapshot(red=0))
        self.assertEqual(reopened.snapshot()['sources']['D01']['available'], 0)
        with self.assertRaises(PickupUnavailable):
            reopened.reserve(new_token, 'request-3', 'D01', 'Red')

    def test_missing_wrong_identity_and_fresh_creation_refuse_existing_files(self):
        missing = self.path.parent / 'absent.sqlite3'
        with self.assertRaises(ValueError):
            PickupInventory.open_existing(missing, self.sources, expected_creation_id=self.store.creation_id)
        self.assertFalse(missing.exists())
        with self.assertRaises(ValueError):
            PickupInventory.open_existing(self.path, self.sources, expected_creation_id='0' * 64)
        with self.assertRaises(FileExistsError):
            PickupInventory.create_new(self.path, self.sources)

    def test_file_replacement_is_detected_before_fresh_allocation(self):
        self.apply(self.store, self.token, self.snapshot())
        other_path = self.path.parent / 'other.sqlite3'
        other = PickupInventory.create_new(other_path, self.sources)
        self.assertNotEqual(other.creation_id, self.store.creation_id)
        other_path.replace(self.path)
        with self.assertRaises(ValueError):
            self.store.reserve(self.token, 'after-swap', 'D01', 'Red')
        with self.assertRaises(ValueError):
            PickupInventory.open_existing(self.path, self.sources, expected_creation_id=self.store.creation_id)

    def test_changed_database_identity_is_rejected_without_new_file(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('UPDATE metadata SET creation_id=? WHERE singleton=1', ('0' * 64,))
            db.commit()
        with self.assertRaises(ValueError):
            self.store.snapshot()
        with self.assertRaises(ValueError):
            PickupInventory.open_existing(self.path, self.sources, expected_creation_id=self.store.creation_id)


if __name__ == '__main__':
    unittest.main()
