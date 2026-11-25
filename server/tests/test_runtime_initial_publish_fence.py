"""Initial publish queueing, acknowledgement and runtime lifetime races."""
import tempfile
import threading
import unittest
from pathlib import Path

from server.allocation_store import AllocationStore
from server.coordinator import Coordinator, CoordinatorError
from server.dispatch import RobotCandidate
from server.recovery import RecoveryGate
from server.sim_runtime import SimRuntime
from server.tests.test_coordinator import FakeInfo, FakePublisher, map_data


class Receiver:
    def __init__(self, gate):
        self._dispatch_lock = threading.RLock()
        self.gate = gate
        self.ready = True
        self._closed = False
        self.task_ids = ('T1',)
        self.stopped = threading.Event()

    def _close_and_invalidate(self, *_args, **_kwargs):
        self.ready = False
        self._closed = True
        self.gate.invalidate_many(self.task_ids)
        self.stopped.set()


class WaitingInfo(FakeInfo):
    def __init__(self, acknowledged=True):
        super().__init__(acknowledged)
        self.waiting = threading.Event()
        self.resume = threading.Event()

    def wait_for_publish(self, timeout):
        self.waits.append(timeout)
        self.waiting.set()
        if not self.resume.wait(4):
            raise TimeoutError('test did not release the acknowledgement')


class RuntimeInitialPublishFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'db.sqlite3'
        self.store = AllocationStore(self.path)
        self.store.initialize()
        self.store.create_task('T1')
        self.publisher = FakePublisher()
        self.publisher.info = WaitingInfo()
        self.coordinator = Coordinator(self.store, map_data(), self.publisher, ack_timeout=3)
        self.coordinator.prepare('T1', 'N4', [RobotCandidate('sim-r1', 'N1', True)], {'type': 'load'})
        self.runtime = SimRuntime(self.path, map_data(), {'sim-r1': {'T1': 'boot-1'}},
                                  port=1, client_id='publish-race')
        self.gate = RecoveryGate(self.path)
        self.gate.initialize()
        self.receiver = Receiver(self.gate)
        self.runtime.gate = self.gate
        self.runtime.receiver = self.receiver
        self.runtime._started = True
        self.closed = []
        self.client = object()
        self.runtime._close_client = self.closed.append
        self.runtime._prepared['T1'] = (self.coordinator, self.client, 0)
        self.results, self.errors = [], []

    def publish_in_thread(self):
        def run():
            try:
                self.results.append(self.runtime.publish('T1'))
            except BaseException as exc:
                self.errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(lambda: (self.publisher.info.resume.set(), thread.join(5)))
        self.assertTrue(self.publisher.info.waiting.wait(3))
        return thread

    def test_ack_wait_does_not_hold_event_callback_lock(self):
        thread = self.publish_in_thread()
        acquired = self.receiver._dispatch_lock.acquire(timeout=0.5)
        if acquired:
            self.receiver._dispatch_lock.release()
        self.publisher.info.resume.set()
        thread.join(5)
        self.assertTrue(acquired, 'event callbacks cannot process disconnect during PUBACK wait')
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])
        self.assertEqual(self.results, [{'task_id': 'T1', 'state': 'acked'}])
        self.assertEqual(self.closed, [self.client])

    def test_disconnect_after_queue_is_not_reported_as_safe_completion(self):
        thread = self.publish_in_thread()
        acquired = self.receiver._dispatch_lock.acquire(timeout=0.5)
        try:
            if acquired:
                self.gate.invalidate_many(('T1',))
        finally:
            if acquired:
                self.receiver._dispatch_lock.release()
            self.publisher.info.resume.set()
        thread.join(5)
        self.assertTrue(acquired)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.results, [])
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], RuntimeError)
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertIn('T1', self.gate._blocked)
        self.assertTrue(self.store.list_occupancy())
        self.assertEqual(len(self.publisher.published), 1)
        self.assertEqual(self.closed, [self.client])

    def test_timeout_after_queue_stays_uncertain_and_closes_client(self):
        self.publisher.info.acknowledged = False
        thread = self.publish_in_thread()
        self.publisher.info.resume.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.results, [])
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], TimeoutError)
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertTrue(self.store.list_occupancy())
        self.assertEqual(self.closed, [self.client])
        with self.assertRaises((RuntimeError, ValueError)):
            self.runtime.publish('T1')
        self.assertEqual(len(self.publisher.published), 1)

    def test_receiver_replacement_after_queue_leaves_order_uncertain(self):
        thread = self.publish_in_thread()
        with self.runtime._lock:
            self.runtime.receiver = Receiver(self.gate)
        self.publisher.info.resume.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.results, [])
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], RuntimeError)
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertEqual(len(self.publisher.published), 1)
        self.assertEqual(self.closed, [self.client])

    def test_failed_queue_is_uncertain_and_closes_client(self):
        self.publisher.publish_error = RuntimeError('network error')
        with self.assertRaisesRegex(RuntimeError, 'network error'):
            self.runtime.publish('T1')
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
        self.assertTrue(self.store.list_occupancy())
        self.assertEqual(self.closed, [self.client])
        with self.assertRaises((RuntimeError, ValueError)):
            self.runtime.publish('T1')
        self.assertEqual(len(self.publisher.published), 1)

    def test_coordinator_confirmation_is_one_shot(self):
        self.publisher.info.resume.set()
        attempt = self.coordinator._queue_publish('T1')
        self.coordinator._wait_publish(attempt)
        self.assertEqual(self.coordinator._record_publish_ack(attempt)['state'], 'acked')
        with self.assertRaises(CoordinatorError):
            self.coordinator._record_publish_ack(attempt)
        self.assertEqual(len(self.publisher.published), 1)

    def test_shutdown_pins_database_owner_until_publication_finishes(self):
        released = []
        self.runtime._owner.release = lambda: released.append(True)
        thread = self.publish_in_thread()
        stop_errors = []
        def stop():
            try:
                self.runtime.stop()
            except BaseException as exc:
                stop_errors.append(exc)
        stopping = threading.Thread(target=stop)
        stopping.start()
        self.addCleanup(lambda: stopping.join(5))
        # stop() can invalidate without waiting for the publisher's PUBACK.
        self.assertTrue(self.receiver.stopped.wait(2))
        self.assertEqual(released, [])
        self.assertEqual(self.closed, [])
        self.publisher.info.resume.set()
        thread.join(5)
        stopping.join(5)
        self.assertFalse(stopping.is_alive())
        self.assertEqual(stop_errors, [])
        self.assertEqual(released, [True])
        self.assertEqual(self.closed, [self.client])
        self.assertEqual(self.store.get_order('T1')['state'], 'uncertain')
