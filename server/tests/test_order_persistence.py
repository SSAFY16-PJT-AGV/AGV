"""Durable simulated orders, idempotency and conservative shelf reservations."""
import copy
import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest

from server.order_persistence import PersistentOrderStore


CATALOG = [
    {'id': 'red', 'product_type': 'Red', 'pickup_node': 'D01', 'destination_node': 'D04',
     'shelf_id': 'SHELF_RED', 'slot_count': 3},
    {'id': 'blue', 'product_type': 'Blue', 'pickup_node': 'D06', 'destination_node': 'D05',
     'shelf_id': 'SHELF_BLUE', 'slot_count': 3},
]


def shelves():
    return [{'shelf_id': 'SHELF_RED', 'product_type': 'Red', 'slots': [True, False, False]},
            {'shelf_id': 'SHELF_BLUE', 'product_type': 'Blue', 'slots': [False, False, False]}]


class PersistentOrderStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'orders.sqlite3'
        self.store = PersistentOrderStore(self.path, 'warehouse@v1', CATALOG)

    def reopen(self):
        return PersistentOrderStore(self.path, 'warehouse@v1', CATALOG)

    def test_pickup_identity_is_immutable_and_survives_reopen(self):
        self.assertIsNone(self.store.pickup_id)
        identity = 'ab' * 32
        self.assertEqual(self.store.bind_pickup_id(identity), identity)
        self.assertEqual(self.reopen().pickup_id, identity)
        with self.assertRaises(ValueError):
            self.reopen().bind_pickup_id('cd' * 32)
        with self.assertRaises(ValueError):
            self.reopen().bind_pickup_id('bad')

    def test_queued_and_idempotency_survive_restart_in_fifo_order(self):
        first = self.store.submit('red', request_key='one')
        second = self.store.submit('blue', request_key='two')
        same = self.reopen().submit('red', request_key='one')
        self.assertEqual(same['request_id'], first['request_id'])
        self.assertFalse(same['created'])
        with self.assertRaises(ValueError):
            self.reopen().submit('blue', request_key='one')
        self.assertEqual(self.reopen().claim_next()['request_id'], first['request_id'])
        self.assertEqual(self.reopen().claim_next()['request_id'], second['request_id'])

    def test_interrupted_dispatch_and_active_are_unknown_queued_survives(self):
        first = self.store.submit('red')
        second = self.store.submit('blue')
        third = self.store.submit('red')
        self.store.claim_next()
        revision = self.store.sync_shelves(shelves())
        reservation = self.store.reserve_slot('SHELF_RED', 'Red', revision, request_id=first['request_id'])
        self.store.mark_active(first['request_id'], {'assigned_robot': 'sim-r1',
            'order_id': 'job-1', 'shelf_id': 'SHELF_RED', 'slot_index': 1,
            'reservation_id': reservation['reservation_id']})
        self.store.claim_next()
        reopened = self.reopen()
        reopened.recover_interrupted()
        self.assertEqual(reopened.get_order(first['request_id'])['state'], 'unknown')
        self.assertEqual(reopened.get_order(second['request_id'])['state'], 'unknown')
        self.assertEqual(reopened.claim_next()['request_id'], third['request_id'])

    def test_parallel_idempotency_key_starts_once_across_instances(self):
        second = self.reopen()
        barrier = threading.Barrier(3)
        results, errors = [], []
        def submit(store):
            barrier.wait()
            try:
                results.append(store.submit('red', request_key='parallel'))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=submit, args=(store,)) for store in (self.store, second)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
        self.assertFalse(errors)
        self.assertEqual(len({result['request_id'] for result in results}), 1)
        self.assertEqual(len(self.reopen().list_orders()), 1)

    def test_slot_reservations_are_atomic_and_persist_after_restart(self):
        version = self.store.sync_shelves(shelves())
        second = self.reopen()
        barrier = threading.Barrier(3)
        results, errors = [], []
        def reserve(store):
            barrier.wait()
            try:
                results.append(store.reserve_slot('SHELF_RED', 'Red', version))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=reserve, args=(store,)) for store in (self.store, second)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
        self.assertFalse(errors)
        self.assertEqual({item['slot_index'] for item in results}, {1, 2})
        self.assertEqual(len(self.reopen().load_shelves()['reservations']), 2)
        with self.assertRaises(RuntimeError):
            self.reopen().reserve_slot('SHELF_RED', 'Red', version)

    def test_snapshot_cannot_erase_confirmed_or_unresolved_reservation(self):
        revision = self.store.sync_shelves(shelves())
        reserved = self.store.reserve_slot('SHELF_RED', 'Red', revision)
        self.store.confirm_slot(reserved['reservation_id'])
        with self.assertRaises(RuntimeError):
            self.reopen().sync_shelves(shelves())
        self.assertEqual(self.reopen().load_shelves()['reservations'][0]['state'], 'confirmed')

    def test_open_rejects_different_map_and_catalog(self):
        with self.assertRaises(ValueError):
            PersistentOrderStore(self.path, 'other@v1', CATALOG)
        other = copy.deepcopy(CATALOG)
        other[0]['slot_count'] = 4
        with self.assertRaises(ValueError):
            PersistentOrderStore(self.path, 'warehouse@v1', other)

    def test_successful_ack_invalidates_older_snapshot_revision(self):
        revision = self.store.sync_shelves(shelves())
        reserved = self.store.reserve_slot('SHELF_RED', 'Red', revision)
        self.store.confirm_slot(reserved['reservation_id'])
        with self.assertRaises(RuntimeError):
            self.store.reserve_slot('SHELF_BLUE', 'Blue', revision)

    def test_reopen_rejects_replaced_nonunique_reservation_index(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP INDEX unique_held_slot')
            db.execute('CREATE INDEX unique_held_slot ON slot_reservations(shelf_id,slot_index)')
            db.commit()
        with self.assertRaises(ValueError):
            self.reopen()

    def test_active_assignment_requires_all_correlated_fields(self):
        first = self.store.submit('red')
        self.store.claim_next()
        with self.assertRaises(ValueError):
            self.store.mark_active(first['request_id'], {'assigned_robot': 'sim-r1'})
        self.assertEqual(self.store.get_order(first['request_id'])['state'], 'dispatching')

    def test_peek_and_claim_preserve_fifo_and_queued_until_dispatch(self):
        first = self.store.submit('red')
        second = self.store.submit('blue')
        self.assertEqual(self.reopen().peek_next()['request_id'], first['request_id'])
        with self.assertRaises(RuntimeError):
            self.store.claim(second['request_id'])
        self.assertEqual(self.store.get_order(first['request_id'])['state'], 'queued')
        self.assertEqual(self.store.claim(first['request_id'])['state'], 'dispatching')

    def test_completion_merges_original_assignment_into_result(self):
        first = self.store.submit('red')
        self.store.claim(first['request_id'])
        revision = self.store.sync_shelves(shelves())
        reservation = self.store.reserve_slot('SHELF_RED', 'Red', revision, request_id=first['request_id'])
        assignment = {'assigned_robot': 'sim-r1', 'order_id': 'job-1',
                      'shelf_id': 'SHELF_RED', 'slot_index': 1,
                      'reservation_id': reservation['reservation_id']}
        self.store.mark_active(first['request_id'], assignment)
        result = self.store.mark_completed(first['request_id'], {'phase': 'done'})
        self.assertEqual(result['result'], dict(assignment, phase='done'))

    def test_active_must_match_reserved_order_slot(self):
        first = self.store.submit('red')
        second = self.store.submit('red')
        self.store.claim(first['request_id'])
        revision = self.store.sync_shelves(shelves())
        other = self.store.reserve_slot('SHELF_RED', 'Red', revision, request_id=second['request_id'])
        assignment = {'assigned_robot': 'sim-r1', 'order_id': 'job-1',
                      'shelf_id': 'SHELF_RED', 'slot_index': other['slot_index'],
                      'reservation_id': other['reservation_id']}
        with self.assertRaises(ValueError):
            self.store.mark_active(first['request_id'], assignment)
        self.assertEqual(self.store.get_order(first['request_id'])['state'], 'dispatching')

    def test_simulation_store_identity_is_bound_once_and_survives_reopen(self):
        self.assertIsNone(self.store.simulation_id)
        value = 'a' * 64
        self.store.bind_simulation_id(value)
        self.assertEqual(self.reopen().simulation_id, value)
        self.reopen().bind_simulation_id(value)
        with self.assertRaises(ValueError):
            self.reopen().bind_simulation_id('b' * 64)

    def test_unreal_world_identity_is_canonical_and_bound_once(self):
        self.assertIsNone(self.store.world_id)
        value = 'a' * 32
        self.assertEqual(self.store.bind_world_id(value.upper()), value)
        self.assertEqual(self.reopen().world_id, value)
        self.reopen().bind_world_id(value)
        with self.assertRaises(ValueError):
            self.reopen().bind_world_id('b' * 32)
        for invalid in ('', 'a' * 31, 'a' * 33, 'g' * 32, None, 123):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.bind_world_id(invalid)
        self.assertEqual(self.reopen().world_id, value)

    def test_corrupt_unreal_world_identity_fails_closed(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE metadata SET world_id='not-an-identity'")
            db.commit()
        with self.assertRaises(ValueError):
            self.reopen()

    def test_active_phase_and_location_survive_restart_without_reassignment(self):
        first = self.store.submit('red')
        self.store.claim(first['request_id'])
        revision = self.store.sync_shelves(shelves())
        reservation = self.store.reserve_slot('SHELF_RED', 'Red', revision, request_id=first['request_id'])
        assignment = {'assigned_robot': 'sim-r1', 'order_id': 'job-1',
                      'shelf_id': 'SHELF_RED', 'slot_index': 1,
                      'reservation_id': reservation['reservation_id']}
        self.store.mark_active(first['request_id'], assignment)
        self.store.update_active(first['request_id'], {'phase': 'to_destination', 'current_node': 'D01'})
        saved = self.reopen().get_order(first['request_id'])['result']
        self.assertEqual(saved['phase'], 'to_destination')
        self.assertEqual(saved['current_node'], 'D01')
        self.assertEqual(saved['reservation_id'], reservation['reservation_id'])
        with self.assertRaises(ValueError):
            self.store.update_active(first['request_id'], {'slot_index': 2})


if __name__ == '__main__':
    unittest.main()
