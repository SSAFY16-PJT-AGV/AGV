import importlib
import importlib.util
import json
import sqlite3
from contextlib import closing
import tempfile
import threading
import unittest
from pathlib import Path

from server.allocation_store import AllocationStore
from server.dispatch import RobotCandidate
from server.order_publisher import OrderPublisher, validate_order
from server.task_store import SqliteTaskStore
from server.tasks import StateConflictError


def map_data():
    return {
        'map_id': 'layout', 'version': 4, 'unit': 'mm',
        'nodes': [{'id': 'N1', 'pos': [0, 0]}, {'id': 'N2', 'pos': [10, 0]},
                  {'id': 'N3', 'pos': [20, 0]}, {'id': 'N4', 'pos': [30, 0]},
                  {'id': 'N5', 'pos': [0, 10]}, {'id': 'N6', 'pos': [10, 10]}],
        'edges': [
            {'from': 'N1', 'to': 'N2', 'shape': [[0, 0], [10, 0]], 'bidirectional': True, 'lanes': 1},
            {'from': 'N2', 'to': 'N3', 'shape': [[10, 0], [20, 0]], 'bidirectional': True, 'lanes': 1},
            {'from': 'N3', 'to': 'N4', 'shape': [[20, 0], [30, 0]], 'bidirectional': True, 'lanes': 1},
            {'from': 'N5', 'to': 'N6', 'shape': [[0, 10], [10, 10]], 'bidirectional': True, 'lanes': 1},
        ],
        'zones': [
            {'id': 'cross', 'type': 'shared', 'nodes': ['N2']},
            {'id': 'w3', 'type': 'waiting', 'node': 'N3'},
            {'id': 'w4', 'type': 'waiting', 'node': 'N4'},
            {'id': 'w6', 'type': 'waiting', 'node': 'N6'},
        ],
    }


class FakeInfo:
    def __init__(self, acknowledged=True):
        self.acknowledged = acknowledged
        self.waits = []
        self.rc = 0

    def wait_for_publish(self, timeout):
        self.waits.append(timeout)

    def is_published(self):
        return self.acknowledged


class FakePublisher:
    def __init__(self, acknowledged=True, validate_error=None, publish_error=None):
        self.info = FakeInfo(acknowledged)
        self.validate_error = validate_error
        self.publish_error = publish_error
        self.published = []
        self.owner = None
        self.attempted = False
        self.lock = threading.Lock()

    def claim(self, owner):
        with self.lock:
            if self.owner is not None or self.attempted:
                raise RuntimeError('publisher already claimed')
            self.owner = owner

    def release_claim(self, owner):
        with self.lock:
            if self.owner is not owner or self.attempted:
                raise RuntimeError('publisher claim cannot be released')
            self.owner = None

    def validate(self, robot_id, order, *, owner=None):
        validate_order(robot_id, order)
        with self.lock:
            if self.owner is not owner or self.attempted:
                raise RuntimeError('publisher claim is invalid')
        if self.validate_error:
            raise self.validate_error

    def publish(self, robot_id, order, *, owner=None):
        with self.lock:
            if self.owner is not owner or self.attempted:
                raise RuntimeError('publisher claim is invalid')
            self.attempted = True
        self.published.append((robot_id, order))
        if self.publish_error:
            raise self.publish_error
        return self.info


class CoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('server.coordinator'), 'coordinator is not implemented')
        self.module = importlib.import_module('server.coordinator')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'tasks.sqlite3'
        self.store = AllocationStore(self.path)
        self.store.initialize()
        self.store.create_task('T1')
        self.publisher = FakePublisher()
        self.map = map_data()
        self.coordinator = self.module.Coordinator(self.store, self.map, self.publisher, ack_timeout=1.0)
        self.candidates = [RobotCandidate('sim-r1', 'N1', True), RobotCandidate('sim-r2', 'N4', False)]

    def prepare(self):
        return self.coordinator.prepare('T1', 'N4', self.candidates, {'type': 'load'})

    def test_prepare_atomically_persists_nearest_robot_order_and_full_segment_resources(self):
        allocation = self.prepare()
        self.assertEqual(allocation['robot_id'], 'sim-r1')
        self.assertEqual(allocation['nodes'], ['N1', 'N2', 'N3', 'N4'])
        self.assertEqual(allocation['released_until'], 'N3')
        self.assertEqual(self.store.list_occupancy(), {
            ('node', 'N1'): 'sim-r1', ('node', 'N2'): 'sim-r1', ('node', 'N3'): 'sim-r1',
            ('edge', '["N1","N2"]'): 'sim-r1', ('edge', '["N2","N3"]'): 'sim-r1',
            ('zone', 'cross'): 'sim-r1'})
        order = json.loads(self.store.get_order('T1')['payload'])
        self.assertEqual(order, {'order_id': 'T1', 'seq': 1, 'map_id': 'layout@v4',
                                 'nodes': ['N1', 'N2', 'N3', 'N4'], 'released_until': 'N3',
                                 'action_at_end': {'type': 'load'}})
        self.assertEqual(self.publisher.published, [])

    def test_publishes_only_the_persisted_order_then_records_broker_ack(self):
        self.prepare()
        self.coordinator.publish('T1')
        self.assertEqual(self.publisher.published, [('sim-r1', json.loads(self.store.get_order('T1')['payload']))])
        self.assertEqual(self.publisher.info.waits, [1.0])
        self.assertEqual(self.store.get_order('T1')['state'], 'acked')
        self.assertTrue(self.store.list_occupancy())

    def test_preflight_failure_does_not_mark_attempted_or_send(self):
        self.prepare()
        self.publisher.validate_error = ValueError('bad preflight')
        with self.assertRaises(ValueError):
            self.coordinator.publish('T1')
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')
        self.assertEqual(self.publisher.published, [])
        self.assertTrue(self.store.list_occupancy())
        self.publisher.validate_error = None
        with self.assertRaises((RuntimeError, ValueError)):
            self.coordinator.publish('T1')
        self.assertEqual(self.publisher.published, [])
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')

    def test_publish_failure_and_timeout_remain_uncertain_and_preserve_occupancy(self):
        for acknowledged, error in ((False, None), (True, RuntimeError('transport failure'))):
            with self.subTest(acknowledged=acknowledged, error=error):
                task_id = 'T' + str(acknowledged)
                # Use a fresh database to avoid intentional robot conflicts.
                store = AllocationStore(Path(self.tmp.name) / (task_id + '.sqlite3'))
                store.initialize(); store.create_task(task_id)
                publisher = FakePublisher(acknowledged, publish_error=error)
                coordinator = self.module.Coordinator(store, self.map, publisher, ack_timeout=0.1)
                coordinator.prepare(task_id, 'N4', self.candidates, {'type': 'load'})
                with self.assertRaises((RuntimeError, TimeoutError)):
                    coordinator.publish(task_id)
                self.assertEqual(store.get_order(task_id)['state'], 'uncertain')
                self.assertTrue(AllocationStore(store.path).list_occupancy())
                with self.assertRaises((RuntimeError, ValueError)):
                    coordinator.publish(task_id)
                self.assertEqual(len(publisher.published), 1)

    def test_new_coordinator_cannot_publish_persisted_pending_order(self):
        self.prepare()
        reopened = AllocationStore(self.path)
        coordinator = self.module.Coordinator(reopened, self.map, FakePublisher())
        with self.assertRaises((RuntimeError, ValueError)):
            coordinator.publish('T1')
        self.assertEqual(reopened.get_order('T1')['state'], 'pending')
        self.assertTrue(reopened.list_occupancy())

    def test_competing_publications_cause_at_most_one_attempt(self):
        self.prepare()
        gate = threading.Barrier(2)
        successes, failures = [], []

        def run():
            gate.wait()
            try:
                self.coordinator.publish('T1')
                successes.append(True)
            except (RuntimeError, ValueError):
                failures.append(True)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual((len(successes), len(failures), len(self.publisher.published)), (1, 1, 1))

    def test_map_input_is_copied_and_invalid_timeout_rejected(self):
        self.map['nodes'][0]['pos'][0] = 999
        self.assertEqual(self.prepare()['nodes'], ['N1', 'N2', 'N3', 'N4'])
        for timeout in (0, -1, 31, float('nan'), float('inf'), True, '1', 10 ** 1000):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    self.module.Coordinator(self.store, map_data(), self.publisher, ack_timeout=timeout)

    def test_bad_action_and_non_simulated_robot_do_not_persist_allocation(self):
        for candidates, action in ((self.candidates, {}),
                                   ([RobotCandidate('real-robot', 'N1', True)], {'type': 'load'})):
            with self.subTest(candidates=candidates, action=action):
                with self.assertRaises(ValueError):
                    self.coordinator.prepare('T1', 'N4', candidates, action)
                self.assertIsNone(self.store.get_order('T1'))
                self.assertEqual(self.store.list_occupancy(), {})

    def test_bad_route_or_no_available_robot_does_not_persist_allocation(self):
        for candidates, pickup in (([], 'N4'),
                                   ([RobotCandidate('sim-r1', 'N4', True)], 'N4'),
                                   (self.candidates, 'missing')):
            with self.subTest(candidates=candidates, pickup=pickup):
                with self.assertRaises((RuntimeError, ValueError)):
                    self.coordinator.prepare('T1', pickup, candidates, {'type': 'load'})
                self.assertIsNone(self.store.get_order('T1'))
                self.assertEqual(self.store.list_occupancy(), {})

    def test_modified_persisted_payload_is_not_published(self):
        self.prepare()
        with closing(sqlite3.connect(str(self.path))) as db:
            db.execute("UPDATE allocation_outbox SET payload=? WHERE task_id=?", ('{}', 'T1'))
            db.commit()
        with self.assertRaises((RuntimeError, ValueError)):
            self.coordinator.publish('T1')
        self.assertEqual(self.publisher.published, [])
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')
        self.assertTrue(self.store.list_occupancy())

    def test_cancelled_task_is_never_published_and_keeps_reservations(self):
        self.prepare()
        tasks = SqliteTaskStore(self.path)
        tasks.transition('T1', 'ASSIGNED', 'CANCELLED')
        prior = self.store.list_occupancy()
        with self.assertRaises((RuntimeError, ValueError)):
            self.coordinator.publish('T1')
        self.assertEqual(self.publisher.published, [])
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')
        self.assertEqual(self.store.list_occupancy(), prior)

    def test_cancel_racing_after_attempt_marker_is_rejected(self):
        tasks = SqliteTaskStore(self.path)

        class RacingPublisher(FakePublisher):
            def publish(inner, robot_id, order, *, owner=None):
                with self.assertRaises(StateConflictError):
                    tasks.transition('T1', 'ASSIGNED', 'CANCELLED')
                return super().publish(robot_id, order, owner=owner)

        self.coordinator = self.module.Coordinator(self.store, self.map, RacingPublisher())
        self.prepare()
        self.coordinator.publish('T1')
        self.assertEqual(tasks.get('T1').status, 'ASSIGNED')
        self.assertEqual(self.store.get_order('T1')['state'], 'acked')
        self.assertTrue(self.store.list_occupancy())

    def test_missing_shared_zone_refuses_publish_without_releasing_other_resources(self):
        self.prepare()
        with closing(sqlite3.connect(str(self.path))) as db:
            db.execute("DELETE FROM allocation_occupancy WHERE kind=? AND resource_id=?", ('zone', 'cross'))
            db.commit()
        remaining = self.store.list_occupancy()
        with self.assertRaises((RuntimeError, ValueError)):
            self.coordinator.publish('T1')
        self.assertEqual(self.publisher.published, [])
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')
        self.assertEqual(self.store.list_occupancy(), remaining)

    def test_post_publish_db_changes_block_ack_and_keep_uncertain(self):
        for kind in ('payload', 'zone', 'task'):
            with self.subTest(changed=kind):
                store = AllocationStore(Path(self.tmp.name) / (kind + '-race.sqlite3'))
                store.initialize()
                store.create_task('T1')
                publisher = FakePublisher()

                class MutatingInfo(FakeInfo):
                    def wait_for_publish(inner, timeout):
                        super().wait_for_publish(timeout)
                        with closing(sqlite3.connect(store.path)) as db:
                            if kind == 'payload':
                                db.execute('UPDATE allocation_outbox SET payload=? WHERE task_id=?', ('{}', 'T1'))
                            elif kind == 'zone':
                                db.execute('DELETE FROM allocation_occupancy WHERE kind=? AND resource_id=?',
                                           ('zone', 'cross'))
                            else:
                                db.execute('UPDATE tasks SET status=? WHERE task_id=?', ('CANCELLED', 'T1'))
                            db.commit()

                publisher.info = MutatingInfo()
                coordinator = self.module.Coordinator(store, map_data(), publisher)
                coordinator.prepare('T1', 'N4', self.candidates, {'type': 'load'})
                with self.assertRaises((RuntimeError, ValueError)):
                    coordinator.publish('T1')
                self.assertEqual(len(publisher.published), 1)
                self.assertEqual(store.get_order('T1')['state'], 'uncertain')
                self.assertTrue(store.list_occupancy())
                with self.assertRaises((RuntimeError, ValueError)):
                    coordinator.publish('T1')
                self.assertEqual(len(publisher.published), 1)

    def test_default_publisher_is_disabled(self):
        coordinator = self.module.Coordinator(self.store, self.map)
        with self.assertRaises(RuntimeError):
            coordinator.prepare('T1', 'N4', self.candidates, {'type': 'load'})
        self.assertIsNone(self.store.get_order('T1'))
        self.assertEqual(self.store.list_occupancy(), {})
        self.assertEqual(SqliteTaskStore(self.path).get('T1').status, 'PENDING')

    def test_preparation_rejects_disabled_or_disconnected_publisher_without_reservation(self):
        self.publisher.validate_error = RuntimeError('publisher unavailable')
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(self.store.list_occupancy(), {})
        self.assertIsNone(self.store.get_order('T1'))
        self.assertEqual(SqliteTaskStore(self.path).get('T1').status, 'PENDING')

    def test_coordinator_does_not_prepare_a_second_task_with_one_shot_publisher(self):
        self.prepare()
        self.store.create_task('T2')
        with self.assertRaises((RuntimeError, ValueError)):
            self.coordinator.prepare('T2', 'N6', [RobotCandidate('sim-r2', 'N5', True)], {'type': 'load'})
        self.assertIsNone(self.store.get_order('T2'))
        self.assertEqual(SqliteTaskStore(self.path).get('T2').status, 'PENDING')

    def test_distinct_coordinators_cannot_reserve_with_the_same_publisher(self):
        self.prepare()
        self.store.create_task('T2')
        other = self.module.Coordinator(AllocationStore(self.path), map_data(), self.publisher)
        prior = self.store.list_occupancy()
        with self.assertRaises((RuntimeError, ValueError)):
            other.prepare('T2', 'N6', [RobotCandidate('sim-r2', 'N5', True)], {'type': 'load'})
        self.assertIsNone(self.store.get_order('T2'))
        self.assertEqual(self.store.list_occupancy(), prior)

    def test_concurrent_coordinators_share_only_one_publisher_claim(self):
        self.store.create_task('T2')
        other = self.module.Coordinator(AllocationStore(self.path), map_data(), self.publisher)
        barrier = threading.Barrier(2)
        successes, failures = [], []

        def run(coordinator, task, pickup, candidates):
            barrier.wait()
            try:
                coordinator.prepare(task, pickup, candidates, {'type': 'load'})
                successes.append(task)
            except (RuntimeError, ValueError):
                failures.append(task)

        threads = [threading.Thread(target=run, args=(self.coordinator, 'T1', 'N4', self.candidates)),
                   threading.Thread(target=run, args=(other, 'T2', 'N6', [RobotCandidate('sim-r2', 'N5', True)]))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual((len(successes), len(failures)), (1, 1))
        self.assertIsNone(self.store.get_order(failures[0]))
        self.assertEqual(SqliteTaskStore(self.path).get(failures[0]).status, 'PENDING')

    def test_definite_allocation_conflict_releases_publisher_claim_for_retry(self):
        self.store.create_task('T2')
        other = self.module.Coordinator(AllocationStore(self.path), map_data(), FakePublisher())
        other.prepare('T2', 'N4', [RobotCandidate('sim-r2', 'N1', True)], {'type': 'load'})
        with self.assertRaises((RuntimeError, ValueError)):
            self.prepare()
        self.assertIsNone(self.store.get_order('T1'))
        self.assertIsNone(self.publisher.owner)
        result = self.coordinator.prepare('T1', 'N6', [RobotCandidate('sim-r1', 'N5', True)], {'type': 'load'})
        self.assertEqual(result['released_until'], 'N6')

    def test_allocation_error_after_commit_keeps_claim(self):
        from server.allocation_store import AllocationConflict

        class AmbiguousStore:
            def allocate(inner, *args, **kwargs):
                self.store.allocate(*args, **kwargs)
                raise AllocationConflict('simulated post-commit error')

            def get_allocation(inner, task_id):
                return self.store.get_allocation(task_id)

            def get_order(inner, task_id):
                return self.store.get_order(task_id)

        publisher = FakePublisher()
        coordinator = self.module.Coordinator(AmbiguousStore(), self.map, publisher)
        with self.assertRaises(AllocationConflict):
            coordinator.prepare('T1', 'N4', self.candidates, {'type': 'load'})
        self.assertIsNotNone(publisher.owner)
        self.assertTrue(self.store.list_occupancy())
        self.assertEqual(self.store.get_order('T1')['state'], 'pending')

    def test_allocation_error_with_unreadable_db_keeps_claim(self):
        from server.allocation_store import AllocationConflict

        class UnreadableStore:
            def allocate(inner, *args, **kwargs):
                raise AllocationConflict('outcome unknown')

            def get_allocation(inner, task_id):
                raise RuntimeError('database unavailable')

        publisher = FakePublisher()
        coordinator = self.module.Coordinator(UnreadableStore(), self.map, publisher)
        with self.assertRaises(AllocationConflict):
            coordinator.prepare('T1', 'N4', self.candidates, {'type': 'load'})
        self.assertIsNotNone(publisher.owner)

    def test_actual_publisher_claim_and_order_encoding_use_fake_network_client(self):
        class FakeClient:
            _reconnect_on_failure = False

            def __init__(inner):
                inner.sent = []

            def is_connected(inner):
                return True

            def publish(inner, topic, payload, qos, retain):
                inner.sent.append((topic, payload, qos, retain))
                return FakeInfo()

        client = FakeClient()
        publisher = OrderPublisher(client, enabled=True)
        coordinator = self.module.Coordinator(self.store, self.map, publisher)
        coordinator.prepare('T1', 'N4', self.candidates, {'type': 'load'})
        with self.assertRaises(RuntimeError):
            publisher.publish('sim-r1', json.loads(self.store.get_order('T1')['payload']))
        coordinator.publish('T1')
        self.assertEqual(len(client.sent), 1)
        topic, payload, qos, retain = client.sent[0]
        self.assertEqual((topic, qos, retain), ('agv/sim-r1/order', 1, False))
        self.assertEqual(json.loads(payload), json.loads(self.store.get_order('T1')['payload']))
        self.assertEqual(self.store.get_order('T1')['state'], 'acked')

    def test_different_map_content_with_same_version_cannot_acquire_resources(self):
        self.prepare()
        self.store.create_task('T2')
        changed = map_data()
        changed['zones'][0]['id'] = 'different-cross'
        other = self.module.Coordinator(AllocationStore(self.path), changed, FakePublisher())
        prior = self.store.list_occupancy()
        with self.assertRaises((RuntimeError, ValueError)):
            other.prepare('T2', 'N6', [RobotCandidate('sim-r2', 'N5', True)], {'type': 'load'})
        self.assertIsNone(self.store.get_order('T2'))
        self.assertEqual(self.store.list_occupancy(), prior)

    def test_map_with_invalid_extra_json_fields_is_rejected_before_allocation(self):
        for invalid in (float('nan'), '\ud800', {1: 'non-string key'}):
            data = map_data()
            data['extra'] = invalid
            with self.subTest(invalid=invalid):
                with self.assertRaises((RuntimeError, ValueError, TypeError)):
                    self.module.Coordinator(self.store, data, self.publisher)
                self.assertEqual(self.store.list_occupancy(), {})


if __name__ == '__main__':
    unittest.main()
