import hashlib
import json
import tempfile
import threading
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path

from server.allocation_store import AllocationStore
from server.order_publisher import OrderPublisher, OrderPublishingDisabled
from server.progress_publisher import ProgressPublisher, ProgressPublishingError
from server.recovery import RecoveryGate, RecoveryError
from server.segment_progress import SegmentProgress
from server.segments import plan_segment
from server.tests.test_segment_progress import map_data


class FakeInfo:
    def __init__(self, acknowledged=True, on_wait=None):
        self.rc = 0
        self.acknowledged = acknowledged
        self.on_wait = on_wait

    def wait_for_publish(self, timeout):
        if self.on_wait:
            self.on_wait()

    def is_published(self):
        return self.acknowledged


class FakeClient:
    _reconnect_on_failure = False
    def __init__(self):
        self.connected = True
        self.publications = []
        self.info = FakeInfo()
        self.error = None
        self.on_publish = None

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos, retain):
        self.publications.append((topic, json.loads(payload), qos, retain))
        if self.on_publish:
            self.on_publish()
        if self.error:
            raise self.error
        return self.info


class ProgressPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fleet.sqlite3'
        self.store = AllocationStore(self.path)
        self.store.initialize()
        self.map = map_data()
        self.nodes = ['N1', 'N2', 'N3', 'N4', 'N5']
        first = plan_segment(self.map, self.nodes)
        self.next = plan_segment(self.map, self.nodes[2:])
        fingerprint = hashlib.sha256(json.dumps(self.map, sort_keys=True, ensure_ascii=True,
                                 allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.store.create_task('T1')
        allocation = self.store.allocate('T1', 'sim-r1', 'layout@v4', self.nodes,
                    first.released_until, first.resources, {'type': 'load'}, map_fingerprint=fingerprint)
        first_record = self.store.get_order('T1')
        self.store.mark_attempted('T1', first_record['payload'], allocation, first.resources)
        self.store.mark_broker_acked('T1', first_record['payload'], allocation, first.resources)
        self.now = [100.0]
        self.gate = RecoveryGate(self.path, clock=lambda: self.now[0], ttl=30)
        self.gate.initialize()
        token = self.gate.begin('T1', 'boot-1')
        digest = hashlib.sha256(json.dumps(json.loads(first_record['payload']), sort_keys=True,
                                  ensure_ascii=True, allow_nan=False,
                                  separators=(',', ':')).encode('ascii')).hexdigest()
        self.gate.complete('T1', 'boot-1', token, robot_id='sim-r1', order_id='T1',
                           seq=1, map_id='layout@v4', stopped_at='N3',
                           order_digest=digest, stopped=True)
        self.progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=self.gate)
        self.progress.initialize()
        event = {'type': 'rear_clear', 'event_id': 'E1', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'layout@v4', 'stopped_at': 'N3',
                 'cleared_resources': [list(x) for x in sorted(set(first.resources) - set(self.next.resources))]}
        self.progress.advance('sim-r1', event)
        self.client = FakeClient()
        self.publisher = OrderPublisher(self.client, enabled=True)
        self.runner = ProgressPublisher(self.progress, self.publisher, ack_timeout=1)

    def test_success_publishes_stored_seq2_once_and_records_broker_ack(self):
        result = self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(result['state'], 'acked')
        topic, order, qos, retain = self.client.publications[0]
        self.assertEqual((topic, qos, retain), ('agv/sim-r1/order', 1, False))
        self.assertEqual((order['seq'], order['released_until']), (2, 'N5'))
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'acked')
        self.assertEqual(set(self.store.list_occupancy()), set(self.next.resources))
        with self.assertRaises(Exception):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(len(self.client.publications), 1)

    def test_default_disabled_does_not_change_pending_or_publish(self):
        runner = ProgressPublisher(self.progress)
        with self.assertRaises(OrderPublishingDisabled):
            runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'pending')
        self.assertEqual(self.client.publications, [])

    def test_timeout_or_publish_error_preserves_uncertain_and_occupancy(self):
        self.client.info = FakeInfo(acknowledged=False)
        with self.assertRaises(TimeoutError):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')
        self.assertEqual(set(self.store.list_occupancy()), set(self.next.resources))
        with self.assertRaises(Exception):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(len(self.client.publications), 1)

    def test_expired_or_invalidated_proof_never_publishes(self):
        self.now[0] += 31
        with self.assertRaises(RecoveryError):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'pending')
        self.assertEqual(self.client.publications, [])

    def test_disconnect_after_enqueue_prevents_ack_and_retains_uncertainty(self):
        self.client.info = FakeInfo(on_wait=lambda: self.gate.invalidate('T1'))
        with self.assertRaises(RecoveryError):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')
        self.assertEqual(len(self.client.publications), 1)
        self.assertEqual(set(self.store.list_occupancy()), set(self.next.resources))

    def test_competing_publishes_send_at_most_once(self):
        barrier = threading.Barrier(2)
        results = []
        def send():
            barrier.wait()
            try:
                self.runner.publish('T1', 2, 'boot-1')
                results.append(True)
            except Exception:
                results.append(False)
        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.client.publications), 1)

    def test_enqueue_exception_is_uncertain_and_never_retried(self):
        self.client.error = OSError('connection lost')
        with self.assertRaises(Exception):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')
        with self.assertRaises(Exception):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(len(self.client.publications), 1)
        self.assertEqual(set(self.store.list_occupancy()), set(self.next.resources))

    def test_disconnected_before_attempt_keeps_order_pending(self):
        self.client.connected = False
        with self.assertRaises(Exception):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'pending')
        self.assertEqual(self.client.publications, [])
        self.client.connected = True
        with self.assertRaises(ProgressPublishingError):
            self.runner.publish('T1', 2, 'boot-1')
        new_client = FakeClient()
        new_runner = ProgressPublisher(self.progress, OrderPublisher(new_client, enabled=True))
        with self.assertRaises(RecoveryError):
            new_runner.publish('T1', 2, 'boot-1')
        self.assertEqual(new_client.publications, [])

    def test_tampered_occupancy_blocks_send_and_keeps_pending(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM allocation_occupancy WHERE kind='zone' AND resource_id='cross2'")
            db.commit()
        with self.assertRaises(Exception):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'pending')
        self.assertEqual(self.client.publications, [])

    def test_commit_then_exception_does_not_reuse_publisher(self):
        original = self.progress.mark_attempted
        def commit_then_fail(*args):
            original(*args)
            raise OSError('result lost after commit')
        self.progress.mark_attempted = commit_then_fail
        with self.assertRaises(OSError):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')
        with self.assertRaises(Exception):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.client.publications, [])

    def test_occupancy_tamper_after_queue_fails_ack_without_releasing(self):
        def tamper():
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("DELETE FROM allocation_occupancy WHERE kind='zone' AND resource_id='cross2'")
                db.commit()
        self.client.info = FakeInfo(on_wait=tamper)
        with self.assertRaises(Exception):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')
        self.assertEqual(len(self.client.publications), 1)
        self.assertIn(('node', 'N5'), self.store.list_occupancy())

    def test_invalidation_waits_for_queue_call_then_blocks_ack(self):
        started, finished = threading.Event(), threading.Event()
        worker = []
        def invalidate():
            started.set()
            self.gate.invalidate('T1')
            finished.set()
        def enqueue_hook():
            thread = threading.Thread(target=invalidate)
            worker.append(thread)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.1))
        self.client.on_publish = enqueue_hook
        self.client.info = FakeInfo(on_wait=lambda: self.assertTrue(finished.wait(3)))
        with self.assertRaises((RecoveryError, ProgressPublishingError)):
            self.runner.publish('T1', 2, 'boot-1')
        worker[0].join(timeout=3)
        self.assertFalse(worker[0].is_alive())
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')
        self.assertEqual(len(self.client.publications), 1)

    def test_new_gate_epoch_waits_for_database_fence_through_queue_call(self):
        started, finished = threading.Event(), threading.Event()
        worker = []
        def restart():
            started.set()
            newer = RecoveryGate(self.path, clock=lambda: self.now[0], ttl=30)
            newer.initialize()
            newer.invalidate('T1')
            finished.set()
        def enqueue_hook():
            thread = threading.Thread(target=restart)
            worker.append(thread)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.1))
        self.client.on_publish = enqueue_hook
        self.client.info = FakeInfo(on_wait=lambda: self.assertTrue(finished.wait(3)))
        with self.assertRaises((RecoveryError, ProgressPublishingError)):
            self.runner.publish('T1', 2, 'boot-1')
        worker[0].join(timeout=3)
        self.assertFalse(worker[0].is_alive())
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')
        self.assertEqual(len(self.client.publications), 1)

    def test_database_writer_timeout_blocks_proof_and_consumes_publisher(self):
        self.gate._connect = lambda: sqlite3.connect(self.path, timeout=0.1, isolation_level=None)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            try:
                with self.assertRaises(ProgressPublishingError):
                    self.runner.publish('T1', 2, 'boot-1')
                self.assertEqual(self.client.publications, [])
                self.assertEqual(self.progress.get_order('T1', 2)['state'], 'pending')
            finally:
                blocker.execute('ROLLBACK')
        with self.assertRaises(ProgressPublishingError):
            self.runner.publish('T1', 2, 'boot-1')
        new_client = FakeClient()
        new_runner = ProgressPublisher(self.progress, OrderPublisher(new_client, enabled=True))
        with self.assertRaises(RecoveryError):
            new_runner.publish('T1', 2, 'boot-1')
        self.assertEqual(new_client.publications, [])

    def test_reentrant_disconnect_during_queue_fails_without_deadlock(self):
        self.client.on_publish = lambda: self.gate.invalidate('T1')
        with self.assertRaises(Exception):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'uncertain')
        self.assertEqual(len(self.client.publications), 1)
        self.assertEqual(set(self.store.list_occupancy()), set(self.next.resources))

    def test_restart_requires_fresh_challenge_and_explicit_publication(self):
        new_gate = RecoveryGate(self.path, clock=lambda: self.now[0], ttl=30)
        new_gate.initialize()
        with self.assertRaises((RecoveryError, ProgressPublishingError)):
            self.runner.publish('T1', 2, 'boot-1')
        self.assertEqual(self.progress.get_order('T1', 2)['state'], 'pending')
        self.assertEqual(self.client.publications, [])

        first = self.progress.get_order('T1', 1)['payload']
        digest = hashlib.sha256(json.dumps(json.loads(first), sort_keys=True, ensure_ascii=True,
                                allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        token = new_gate.begin('T1', 'boot-2')
        new_gate.complete('T1', 'boot-2', token, robot_id='sim-r1', order_id='T1',
                          seq=1, map_id='layout@v4', stopped_at='N3',
                          order_digest=digest, stopped=True)
        progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=new_gate)
        progress.initialize()
        client = FakeClient()
        sender = ProgressPublisher(progress, OrderPublisher(client, enabled=True), ack_timeout=1)
        self.assertEqual(client.publications, [])
        result = sender.publish('T1', 2, 'boot-2')
        self.assertEqual(result['state'], 'acked')
        self.assertEqual(len(client.publications), 1)


if __name__ == '__main__':
    unittest.main()
