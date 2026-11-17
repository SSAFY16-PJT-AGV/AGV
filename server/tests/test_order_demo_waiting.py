"""Pending web orders wait for a fleet robot without holding control lock."""
import threading
import time
import unittest

from server.order_demo_app import DemoRuntime


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return False


class Fleet:
    control_ready = True

    def __init__(self):
        self.states = ['moving', 'moving']
        self.inventory_ready = True
        self.calls = []

    def snapshot(self):
        return {'mode': 'fleet', 'status': self.states[0],
                'shelf_inventory_ready': self.inventory_ready,
                'accepting_orders': self.inventory_ready and any(v in ('idle', 'completed') for v in self.states),
                'robots': [{'robot_id': 'sim-r{}'.format(i + 1), 'status': value,
                            'orders_remaining': 10} for i, value in enumerate(self.states)]}

    def create_product_order(self, product, pickup, destination, shelf, slot):
        index = next(i for i, value in enumerate(self.states) if value in ('idle', 'completed'))
        self.states[index] = 'moving'
        self.calls.append(product)
        return {'assigned_robot': 'sim-r{}'.format(index + 1), 'product_type': product}

    def action_unknown(self):
        self.states = ['unknown', 'unknown']


class WaitingFleetOrdersTest(unittest.TestCase):
    def setUp(self):
        self.engine = Fleet()
        self.demo = DemoRuntime(self.engine, 1883, [], auto_recovery=True)
        self.owner = object()
        self.assertTrue(self.demo.actions.attach(self.owner, True))
        self.demo.connected.set()
        self.addCleanup(self.cleanup_demo)

    def cleanup_demo(self):
        self.demo.stop_event.set()
        self.demo.admission.shutdown()
        self.demo.admission.join(3)

    def test_busy_fleet_accepts_fifo_and_waits_without_holding_control_lock(self):
        started = time.monotonic()
        red = self.demo.submit_product_order('red', request_key='one')
        blue = self.demo.submit_product_order('blue', request_key='two')
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((red['status'], blue['status']), ('pending', 'pending'))
        self.assertEqual(self.engine.calls, [])
        self.assertTrue(self.demo.lock.acquire(timeout=1), 'waiting worker must not hold control lock')
        try:
            self.engine.states[0] = 'completed'
        finally:
            self.demo.lock.release()
        self.assertTrue(wait_for(lambda: self.demo.admission.get(red['request_id'])['status'] == 'completed'))
        self.assertEqual(self.engine.calls, ['Red'])
        self.assertEqual(self.demo.admission.get(blue['request_id'])['status'], 'pending')
        with self.demo.lock:
            self.engine.states[1] = 'completed'
        self.assertTrue(wait_for(lambda: self.demo.admission.get(blue['request_id'])['status'] == 'completed'))
        self.assertEqual(self.engine.calls, ['Red', 'Blue'])

    def test_pending_order_waits_for_inventory_and_deduplicates_request_key(self):
        self.engine.states[0] = 'idle'
        self.engine.inventory_ready = False
        first = self.demo.submit_product_order('red', request_key='one')
        again = self.demo.submit_product_order('red', request_key='one')
        self.assertEqual(first['request_id'], again['request_id'])
        self.assertEqual(self.engine.calls, [])
        with self.demo.lock:
            self.engine.inventory_ready = True
        self.assertTrue(wait_for(lambda: self.demo.admission.get(first['request_id'])['status'] == 'completed'))
        self.assertEqual(self.engine.calls, ['Red'])

    def test_disconnected_owner_prevents_pending_order_from_starting(self):
        pending = self.demo.submit_product_order('red', request_key='one')
        self.demo.actions.detach(self.owner)
        self.assertTrue(wait_for(lambda: self.demo.admission.get(pending['request_id'])['status'] == 'error'))
        self.assertEqual(self.engine.calls, [])

    def test_stopping_during_wait_prevents_order_mutation(self):
        pending = self.demo.submit_product_order('red', request_key='one')
        self.demo.stop_event.set()
        self.demo.admission.shutdown()
        self.assertTrue(self.demo.admission.join(3))
        self.assertIn(self.demo.admission.get(pending['request_id'])['status'], ('error', 'unknown'))
        self.assertEqual(self.engine.calls, [])

    def test_snapshot_allows_queued_orders_when_both_robots_are_busy(self):
        self.demo._store_snapshot(self.engine.snapshot(), True)
        observed = self.demo.snapshot()
        self.assertFalse(observed['accepting_orders'])
        self.assertTrue(observed['accepting_queued_orders'])

    def test_snapshot_disables_queue_for_unknown_unready_or_disconnected(self):
        self.demo._store_snapshot(self.engine.snapshot(), True)
        self.demo.connected.clear()
        self.assertFalse(self.demo.snapshot()['accepting_queued_orders'])
        self.demo.connected.set()
        self.engine.inventory_ready = False
        self.demo._store_snapshot(self.engine.snapshot(), True)
        self.assertFalse(self.demo.snapshot()['accepting_queued_orders'])
        self.engine.inventory_ready = True
        self.engine.states[0] = 'unknown'
        self.demo._store_snapshot(self.engine.snapshot(), True)
        self.assertFalse(self.demo.snapshot()['accepting_queued_orders'])

    def test_snapshot_disables_queue_when_admission_is_stopped(self):
        self.demo._store_snapshot(self.engine.snapshot(), True)
        self.demo.admission.shutdown()
        self.assertFalse(self.demo.snapshot()['accepting_queued_orders'])

    def test_pending_order_waits_for_its_destination_to_be_dispatchable(self):
        self.engine.states[0] = 'idle'
        allowed = threading.Event()
        seen = []
        def can_dispatch(destination):
            seen.append(destination)
            return allowed.is_set()
        self.engine.can_dispatch_destination = can_dispatch
        pending = self.demo.submit_product_order('red')
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(self.engine.calls, [])
        self.assertTrue(seen and set(seen) == {'D04'})
        allowed.set()
        self.assertTrue(wait_for(lambda: self.demo.admission.get(pending['request_id'])['status'] == 'completed'))
        self.assertEqual(self.engine.calls, ['Red'])

    def test_full_shelf_is_reported_before_order_side_effects(self):
        from server.order_demo_app import ShelfFull
        class FullInventory:
            ready = True
            def can_reserve(self, shelf, product):
                return False
        self.engine.inventory = FullInventory()
        self.engine.states[0] = 'idle'
        with self.assertRaises(ShelfFull):
            self.demo.submit_product_order('red')
        self.assertEqual(self.engine.calls, [])


if __name__ == '__main__':
    unittest.main()
