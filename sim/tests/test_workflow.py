import threading
import sqlite3
from types import SimpleNamespace
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from sim.event_inbox import SimEventInbox
import server.tests.test_segment_progress as fixture_module


class Receiver:
    def __init__(self, progress, inbox):
        self.progress, self.gate, self.inbox = progress, progress._gate, inbox
        self.client = object()
        self.robot_tasks = {'sim-r1': frozenset({'T1'})}
        self._dispatch_lock = threading.RLock()
        self.on_result = None
        self.ready = True
        self._closed = False


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        from sim.workflow import SimProgressWorkflow
        self.Workflow = SimProgressWorkflow
        self.fixture = fixture_module.ProgressTests('test_advance_records_event_and_reserves_next_segment_atomically')
        self.fixture.setUp()
        self.fixture.gate.path = self.fixture.store.path
        self.addCleanup(self.fixture.doCleanups)
        self.inbox = SimEventInbox(self.fixture.store.path, robot_tasks={'sim-r1': ['T1']}, simulation_enabled=True)
        self.inbox.initialize()
        self.receiver = Receiver(self.fixture.progress, self.inbox)
        self.results, self.errors, self.calls, self.closed = [], [], [], []
        self.done = threading.Event()

        @contextmanager
        def factory(robot, task, seq):
            self.calls.append((robot, task, seq))
            try:
                yield object()
            finally:
                self.closed.append((robot, task, seq))
        self.factory = factory

    def workflow(self, **kwargs):
        options = dict(robot_sessions={'sim-r1': {'T1': 'boot-1'}}, publisher_factory=self.factory,
                       simulation_enabled=True, on_result=self.report, on_error=self.report_error)
        options.update(kwargs)
        workflow = self.Workflow(self.receiver, **options)
        self.addCleanup(workflow.stop)
        return workflow

    def report(self, result):
        self.results.append(result)
        self.done.set()

    def report_error(self, error):
        self.errors.append(error)
        self.done.set()

    def applied(self):
        self.inbox.store('sim-r1', self.fixture.event)
        return self.inbox.retry('sim-r1', 'event-1', self.fixture.progress)

    def publisher(self):
        class Publisher:
            def __init__(self, progress, publisher): pass
            def publish(self, task, seq, session):
                return {'task_id': task, 'seq': seq, 'state': 'acked'}
        return Publisher

    def test_disabled_and_observer_hijacking_rejected(self):
        with self.assertRaises(ValueError):
            self.Workflow(self.receiver, robot_sessions={'sim-r1': {'T1': 'boot-1'}},
                          publisher_factory=self.factory)
        old = lambda result: None
        self.receiver.on_result = old
        with self.assertRaises(ValueError):
            self.workflow().start()
        self.assertIs(self.receiver.on_result, old)

    def test_auto_advanced_is_sent_on_worker_and_factory_is_closed(self):
        result = self.applied()
        with patch('sim.workflow.ProgressPublisher', self.publisher()):
            wf = self.workflow()
            wf.start()
            self.receiver.on_result(result)
            self.assertTrue(self.done.wait(3))
            self.assertEqual(self.calls, [('sim-r1', 'T1', 2)])
            self.assertEqual(self.closed, self.calls)
            self.assertEqual(self.results[0]['state'], 'acked')

    def test_wrong_event_session_and_payload_do_not_create_publisher(self):
        result = self.applied()
        with patch('sim.workflow.ProgressPublisher', self.publisher()):
            wf = self.workflow(robot_sessions={'sim-r1': {'T1': 'boot-other'}})
            wf.start()
            self.receiver.on_result(result)
            self.assertTrue(self.done.wait(3))
            self.assertFalse(self.calls)
            self.assertTrue(self.errors)
            self.assertEqual(self.fixture.progress.get_order('T1', 2)['state'], 'pending')

    def test_changed_journal_identity_rejects_auto_submission(self):
        result = self.applied()
        with sqlite3.connect(self.fixture.store.path) as db:
            db.execute("UPDATE sim_event_inbox SET event_id='different'")
            db.execute("UPDATE progress_events SET event_id='different'")
        with patch('sim.workflow.ProgressPublisher', self.publisher()):
            wf = self.workflow()
            wf.start()
            self.receiver.on_result(result)
            self.assertTrue(self.done.wait(3))
            self.assertFalse(self.calls)
            self.assertTrue(self.errors)

    def test_disconnect_while_factory_is_open_stops_before_publishing(self):
        opened, release = threading.Event(), threading.Event()
        @contextmanager
        def slow_factory(robot, task, seq):
            opened.set()
            release.wait(3)
            try:
                yield object()
            finally:
                self.closed.append((robot, task, seq))
        with patch('sim.workflow.ProgressPublisher', self.publisher()):
            wf = self.workflow(publisher_factory=slow_factory)
            wf.start()
            wf.submit_pending('T1', 2, 'boot-1')
            self.assertTrue(opened.wait(3))
            self.receiver.ready = False
            release.set()
            self.assertTrue(self.done.wait(3))
            self.assertTrue(self.errors)
            self.assertFalse(self.results)
            self.assertEqual(self.closed, [('sim-r1', 'T1', 2)])

    def test_factory_cannot_publish_using_receiver_client(self):
        @contextmanager
        def wrong_factory(robot, task, seq):
            yield SimpleNamespace(_client=self.receiver.client)
        with patch('sim.workflow.ProgressPublisher', self.publisher()):
            wf = self.workflow(publisher_factory=wrong_factory)
            wf.start()
            wf.submit_pending('T1', 2, 'boot-1')
            self.assertTrue(self.done.wait(3))
            self.assertFalse(self.results)
            self.assertTrue(self.errors)

    def test_restart_does_not_automatically_send_and_manual_requires_ready(self):
        self.applied()
        with patch('sim.workflow.ProgressPublisher', self.publisher()):
            wf = self.workflow()
            wf.start()
            self.assertFalse(self.done.wait(0.1))
            self.receiver.ready = False
            with self.assertRaises(ValueError):
                wf.submit_pending('T1', 2, 'boot-1')
            self.receiver.ready = True
            wf.submit_pending('T1', 2, 'boot-1')
            self.assertTrue(self.done.wait(3))
            self.assertEqual(self.calls, [('sim-r1', 'T1', 2)])

    def test_duplicate_finished_and_not_ready_do_not_send(self):
        with patch('sim.workflow.ProgressPublisher', self.publisher()):
            wf = self.workflow()
            wf.start()
            self.receiver.on_result({'status': 'duplicate'})
            self.receiver.on_result({'status': 'finished'})
            self.receiver.ready = False
            self.receiver.on_result({'status': 'advanced', 'task_id': 'T1', 'next_seq': 2, 'payload': '{}'})
            self.assertFalse(self.calls)
            self.assertTrue(self.done.wait(3))
            self.assertTrue(self.errors)

    def test_duplicate_task_is_rejected_and_stop_waits_for_inflight_send(self):
        entered, release = threading.Event(), threading.Event()
        class Publisher:
            def __init__(self, progress, publisher): pass
            def publish(self, task, seq, session):
                entered.set()
                release.wait(3)
                return {'task_id': task, 'seq': seq, 'state': 'acked'}
        with patch('sim.workflow.ProgressPublisher', Publisher):
            wf = self.workflow()
            wf.start()
            wf.submit_pending('T1', 2, 'boot-1')
            self.assertTrue(entered.wait(3))
            with self.assertRaises(ValueError):
                wf.submit_pending('T1', 2, 'boot-1')
            stopped = threading.Event()
            thread = threading.Thread(target=lambda: (wf.stop(), stopped.set()))
            thread.start()
            self.assertFalse(stopped.wait(0.1))
            release.set()
            thread.join(3)
            self.assertTrue(stopped.is_set())
            self.assertIsNone(self.receiver.on_result)
            with self.assertRaises(ValueError):
                wf.submit_pending('T1', 2, 'boot-1')

    def test_stop_during_publisher_construction_forbids_new_publish(self):
        constructing, release, published = threading.Event(), threading.Event(), threading.Event()
        class Publisher:
            def __init__(self, progress, publisher):
                constructing.set()
                release.wait(3)
            def publish(self, task, seq, session):
                published.set()
                return {'task_id': task, 'seq': seq, 'state': 'acked'}
        with patch('sim.workflow.ProgressPublisher', Publisher):
            wf = self.workflow()
            wf.start()
            wf.submit_pending('T1', 2, 'boot-1')
            self.assertTrue(constructing.wait(3))
            stopped = threading.Event()
            thread = threading.Thread(target=lambda: (wf.stop(), stopped.set()))
            thread.start()
            for _ in range(200):
                with wf._lock:
                    if wf._stopping:
                        break
                threading.Event().wait(0.005)
            with wf._lock:
                self.assertTrue(wf._stopping)
            release.set()
            thread.join(3)
            self.assertTrue(stopped.is_set())
            self.assertFalse(published.is_set())

    def test_queue_full_and_stop_discard_waiting_work(self):
        self.receiver.robot_tasks = {'sim-r1': frozenset({'T1', 'T2', 'T3'})}
        self.inbox.robot_tasks = self.receiver.robot_tasks
        entered, release = threading.Event(), threading.Event()
        class Publisher:
            def __init__(self, progress, publisher): pass
            def publish(self, task, seq, session):
                entered.set()
                release.wait(3)
                return {'task_id': task, 'seq': seq, 'state': 'acked'}
        with patch('sim.workflow.ProgressPublisher', Publisher):
            wf = self.workflow(queue_size=1, robot_sessions={'sim-r1': {'T1': 's1', 'T2': 's2', 'T3': 's3'}})
            wf.start()
            wf.submit_pending('T1', 2, 's1')
            self.assertTrue(entered.wait(3))
            wf.submit_pending('T2', 2, 's2')
            with self.assertRaises(ValueError):
                wf.submit_pending('T3', 2, 's3')
            self.receiver.on_result({'status': 'advanced', 'task_id': 'T3',
                                     'next_seq': 2, 'payload': '{}'})
            self.assertEqual(wf.last_error, 'submission rejected')
            stopped = threading.Event()
            thread = threading.Thread(target=lambda: (wf.stop(), stopped.set()))
            thread.start()
            self.assertFalse(stopped.wait(0.1))
            release.set()
            thread.join(3)
            self.assertTrue(stopped.is_set())
            self.assertEqual(self.calls, [('sim-r1', 'T1', 2)])

    def test_publisher_error_is_sanitized_and_client_closed(self):
        self.applied()
        class Publisher:
            def __init__(self, progress, publisher): pass
            def publish(self, task, seq, session): raise RuntimeError('secret test payload')
        with patch('sim.workflow.ProgressPublisher', Publisher):
            wf = self.workflow()
            wf.start()
            wf.submit_pending('T1', 2, 'boot-1')
            self.assertTrue(self.done.wait(3))
            self.assertEqual(self.closed, self.calls)
            self.assertNotIn('secret', str(self.errors[0]))
            self.assertEqual(self.fixture.progress.get_order('T1', 2)['state'], 'pending')

    def test_timeout_reports_uncertainty_without_exception_details(self):
        class Publisher:
            def __init__(self, progress, publisher): pass
            def publish(self, task, seq, session): raise TimeoutError('sensitive payload')
        with patch('sim.workflow.ProgressPublisher', Publisher):
            wf = self.workflow()
            wf.start()
            wf.submit_pending('T1', 2, 'boot-1')
            self.assertTrue(self.done.wait(3))
            self.assertEqual(self.errors, ['broker acknowledgement timeout; outcome uncertain'])
            self.assertEqual(self.closed, self.calls)


if __name__ == '__main__':
    unittest.main()
