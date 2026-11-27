"""Synthetic approvals keep durable identity but never stale authority."""
from pathlib import Path
import tempfile
import unittest

from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_persistent_orders import TeamShelfPersistentOrders
from server.team_shelf_synthetic_replenishment import SyntheticReplenishment
from server.tests.test_team_shelf_synthetic_replenishment import SyntheticReplenishmentTest
from server.order_demo_admission import AdmissionConflict, AdmissionStopped

PRODUCTS = [{'id': 'milk', 'product_type': 'milk', 'pickup_node': 'D01',
             'destination_node': 'D02', 'shelf_id': 'N02', 'slot_count': 4}]


class DurableSyntheticAdmissionTest(unittest.TestCase):
    def test_key_and_approval_fingerprint_survive_reopen_without_duplication(self):
        with tempfile.TemporaryDirectory(prefix='team-durable-replenish-') as folder:
            path = Path(folder)
            slots = TeamShelfOrderSlots.create_new(path / 'slots.sqlite3')
            journal = TeamShelfPersistentOrders(path / 'orders.sqlite3', 'v3', PRODUCTS, slots)
            first = journal.submit_approved('milk', 'request-key', threshold=1)
            self.assertEqual(journal.submit_approved('milk', 'request-key', threshold=1)['request_id'],
                             first['request_id'])
            with self.assertRaises(ValueError):
                journal.submit_approved('milk', 'request-key', threshold=2)
            with self.assertRaises(ValueError):
                journal.submit('milk', 'request-key')
            reopened = TeamShelfPersistentOrders(path / 'orders.sqlite3', 'v3', PRODUCTS, slots)
            self.assertEqual(reopened.get_approval(first['request_id']), ('milk', 1))
            self.assertEqual(reopened.get_order_by_key('request-key')['request_id'], first['request_id'])
            self.assertEqual(len(reopened.list_orders()), 1)

    def test_interrupted_approved_order_remains_unknown(self):
        with tempfile.TemporaryDirectory(prefix='team-durable-replenish-') as folder:
            path = Path(folder)
            slots = TeamShelfOrderSlots.create_new(path / 'slots.sqlite3')
            journal = TeamShelfPersistentOrders(path / 'orders.sqlite3', 'v3', PRODUCTS, slots)
            order = journal.submit_approved('milk', 'key', threshold=1)
            journal.claim(order['request_id'])
            reopened = TeamShelfPersistentOrders(path / 'orders.sqlite3', 'v3', PRODUCTS, slots)
            reopened.recover_interrupted()
            self.assertEqual(reopened.get_order_by_key('key')['state'], 'unknown')
            self.assertEqual(reopened.get_approval(order['request_id']), ('milk', 1))


class _IdleAdmission:
    def __init__(self, journal): self.journal, self.resumes = journal, 0
    def resume(self): self.resumes += 1
    def get_by_key(self, key):
        order = self.journal.get_order_by_key(key)
        return {'status': 'unknown' if order['state'] == 'unknown' else 'pending',
                'request_id': order['request_id']}
    def shutdown(self): pass
    def join(self, _timeout=None): return True


class DurableSyntheticServiceTest(unittest.TestCase):
    shutdown = SyntheticReplenishmentTest.shutdown

    def setUp(self):
        SyntheticReplenishmentTest.setUp(self)
        # Preserve the original fixture's stop hooks while replacing its
        # in-memory admission with the durable journal used by this mode.
        self.demo.admission.shutdown()
        self.demo.admission.join(3)
        catalog = [dict(self.products[0], slot_count=4)]
        self.demo.journal = TeamShelfPersistentOrders(Path(self.tmp.name) / 'orders.sqlite3',
                                                       'v3', catalog, self.store)
        self.demo.journal.bind_world_id('a' * 32)
        self.demo.admission = _IdleAdmission(self.demo.journal)
        self.demo._world_ready.set()
        self.service = SyntheticReplenishment(self.demo, clock=lambda: self.now, max_age_seconds=5)

    def test_approved_order_is_durable_and_revalidated_before_dispatch(self):
        self.assertEqual(self.service.capture(threshold=1)['milk']['status'], 'shortage')
        first = self.service.submit('milk', threshold=1, approved=True, request_key='same')
        duplicate = self.service.submit('milk', threshold=1, approved=True, request_key='same')
        self.assertEqual(first['request_id'], duplicate['request_id'])
        self.assertEqual(len(self.demo.journal.list_orders()), 1)
        order = self.demo.journal.get_order(first['request_id'])
        self.assertTrue(self.service.validate_durable(order))
        self.assertEqual(self.store.inspect()['claims'], [])
        with self.assertRaises(AdmissionConflict):
            self.service.submit('milk', threshold=2, approved=True, request_key='same')

    def test_expired_or_restart_lost_approval_cannot_dispatch(self):
        self.service.capture(threshold=1)
        first = self.service.submit('milk', threshold=1, approved=True, request_key='key')
        order = self.demo.journal.get_order(first['request_id'])
        self.now = 7.0
        with self.assertRaises((ConnectionError, ValueError)):
            self.service.validate_durable(order)
        self.assertEqual(self.store.inspect()['claims'], [])
        restarted = SyntheticReplenishment(self.demo, clock=lambda: self.now, max_age_seconds=5)
        with self.assertRaises((ConnectionError, ValueError)):
            restarted.validate_durable(order)
        self.assertEqual(self.demo.journal.get_order_by_key('key')['request_id'], first['request_id'])

    def test_recovery_blocked_preserves_existing_key_and_rejects_new(self):
        self.service.capture(threshold=1)
        first = self.service.submit('milk', threshold=1, approved=True, request_key='key')
        self.demo._restart_blocked = True
        self.assertEqual(self.service.submit('milk', threshold=1, approved=True, request_key='key'),
                         {'status': 'unknown', 'request_id': first['request_id']})
        with self.assertRaises(AdmissionStopped):
            self.service.submit('milk', threshold=1, approved=True, request_key='new')


if __name__ == '__main__':
    unittest.main()
