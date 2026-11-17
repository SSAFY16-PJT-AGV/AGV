"""Durable order admission retains identifiers and pending work after restart."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from server.order_demo_app import PRODUCTS
from server.order_persistence import PersistentOrderStore
from server.order_persistent_admission import PersistentAdmission


def wait_for(predicate, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(.01)
    return False


class PersistentAdmissionTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'orders.sqlite3'
        self.store = PersistentOrderStore(self.path, 'test@v1', PRODUCTS)

    def test_unstarted_order_and_key_survive_restart(self):
        stopped = threading.Event()
        entered = threading.Event()
        def wait_without_side_effect(order):
            entered.set()
            stopped.wait(2)
        first = PersistentAdmission(self.store, wait_without_side_effect, stopped, wait_seconds=.01)
        pending = first.submit_product('red', 'same-key')
        self.assertEqual(pending['status'], 'pending')
        self.assertTrue(entered.wait(1))
        stopped.set()
        first.shutdown()
        self.assertTrue(first.join(2))
        reopened = PersistentOrderStore(self.path, 'test@v1', PRODUCTS)
        reopened.recover_interrupted()
        self.assertEqual(reopened.get_order(pending['request_id'])['state'], 'queued')
        self.assertEqual(reopened.submit('red', 'same-key')['request_id'], pending['request_id'])

    def test_assigned_order_returns_stable_request_identifier(self):
        stopped = threading.Event()
        def dispatch(order):
            self.store.claim(order['request_id'])
            result = {'assigned_robot': 'sim-r1', 'order_id': 'web-pickup',
                      'shelf_id': 'SHELF_RED', 'slot_index': 1, 'reservation_id': 'r1'}
            self.store.mark_active(order['request_id'], result)
        # This fake dispatch intentionally lacks a valid durable reservation;
        # the journal must reject it instead of claiming an order is active.
        admission = PersistentAdmission(self.store, dispatch, stopped, wait_seconds=.01)
        self.addCleanup(lambda: (stopped.set(), admission.shutdown(), admission.join(2)))
        pending = admission.submit_product('red', 'one')
        self.assertIn(pending['status'], ('pending', 'unknown'))
        self.assertTrue(wait_for(lambda: self.store.get_order(pending['request_id'])['state'] == 'unknown'))

    def test_completed_assignment_and_idempotency_survive_reopen(self):
        revision = self.store.sync_shelves([
            {'shelf_id': 'SHELF_RED', 'product_type': 'Red', 'slots': [False] * 6},
            {'shelf_id': 'SHELF_BLUE', 'product_type': 'Blue', 'slots': [False] * 6}])
        stopped = threading.Event()
        def dispatch(order):
            self.store.claim(order['request_id'])
            held = self.store.reserve_slot('SHELF_RED', 'Red', revision, request_id=order['request_id'])
            result = dict(held, assigned_robot='sim-r1', order_id='web-pickup')
            self.store.mark_active(order['request_id'], result)
            self.store.confirm_slot(held['reservation_id'])
            self.store.mark_completed(order['request_id'], {'status': 'completed'})
        admission = PersistentAdmission(self.store, dispatch, stopped, wait_seconds=.5)
        result = admission.submit_product('red', 'stable-key')
        stopped.set()
        admission.shutdown()
        self.assertTrue(admission.join(2))
        restarted = PersistentOrderStore(self.path, 'test@v1', PRODUCTS)
        restarted.recover_interrupted()
        state = restarted.get_order(result['request_id'])
        self.assertEqual(state['state'], 'completed')
        self.assertEqual(state['result']['slot_index'], 0)
        self.assertEqual(restarted.submit('red', 'stable-key')['request_id'], result['request_id'])


if __name__ == '__main__':
    unittest.main()
