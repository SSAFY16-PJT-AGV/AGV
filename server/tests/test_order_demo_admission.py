"""Bounded at-most-once admission for the isolated web order demo."""
import threading
import time
import unittest

from server.order_demo_admission import (AdmissionBusy, AdmissionConflict, AdmissionOutcomeUnknown,
                                         AdmissionStopped, OrderAdmission)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.admission = OrderAdmission(wait_seconds=0.03, max_records=4)
        self.addCleanup(self.admission.shutdown)

    def test_slow_operation_returns_pending_then_result_without_duplicate(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow():
            calls.append('run')
            entered.set()
            self.assertTrue(release.wait(3))
            return {'assigned_robot': 'sim-r1', 'route': ['a', 'b']}
        first = self.admission.submit(slow, 'red', request_key='key-1')
        self.assertTrue(entered.is_set())
        self.assertEqual(first['status'], 'pending')
        request_id = first['request_id']
        duplicate = self.admission.submit(slow, 'red', request_key='key-1')
        self.assertEqual(duplicate['request_id'], request_id)
        self.assertEqual(self.admission.get(request_id)['status'], 'pending')
        self.assertEqual(calls, ['run'])
        release.set()
        deadline = time.monotonic() + 2
        while self.admission.get(request_id)['status'] == 'pending' and time.monotonic() < deadline:
            time.sleep(.005)
        result = self.admission.get(request_id)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['result']['assigned_robot'], 'sim-r1')
        result['result']['route'].clear()
        self.assertEqual(self.admission.get(request_id)['result']['route'], ['a', 'b'])
        self.assertEqual(self.admission.submit(slow, 'red', request_key='key-1')['route'], ['a', 'b'])
        self.assertEqual(calls, ['run'])

    def test_busy_and_conflicting_key_do_not_start_a_second_operation(self):
        entered, release = threading.Event(), threading.Event()
        called = []
        def slow():
            entered.set()
            release.wait(3)
            return 'done'
        first = self.admission.submit(slow, 'red', request_key='one')
        self.assertTrue(entered.is_set())
        try:
            with self.assertRaises(AdmissionConflict):
                self.admission.submit(lambda: called.append(1), 'blue', request_key='one')
            with self.assertRaises(AdmissionBusy):
                self.admission.submit(lambda: called.append(2), 'blue', request_key='two')
            self.assertEqual(called, [])
            self.assertEqual(self.admission.get(first['request_id'])['status'], 'pending')
        finally:
            release.set()

    def test_fast_operation_returns_value_and_error_is_not_reexecuted(self):
        self.assertEqual(self.admission.submit(lambda: {'ok': True}, 'ok'), {'ok': True})
        called = []
        def bad():
            called.append(1)
            raise ValueError('expected')
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'expected'):
                self.admission.submit(bad, 'bad', request_key='err')
        self.assertEqual(called, [1])

    def test_async_error_is_recorded_without_retry(self):
        entered, release = threading.Event(), threading.Event()
        def bad():
            entered.set()
            release.wait(3)
            raise ValueError('expected')
        pending = self.admission.submit(bad, 'bad', request_key='err')
        self.assertTrue(entered.is_set())
        release.set()
        deadline = time.monotonic() + 2
        while self.admission.get(pending['request_id'])['status'] == 'pending' and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.admission.get(pending['request_id'])['error_type'], 'ValueError')
        with self.assertRaises(ValueError):
            self.admission.submit(bad, 'bad', request_key='err')

    def test_uncertain_operation_returns_unknown_id_and_is_not_reexecuted(self):
        calls = []
        def uncertain():
            calls.append(1)
            raise AdmissionOutcomeUnknown('outcome not known')
        first = self.admission.submit(uncertain, 'red', request_key='k')
        self.assertEqual(first['status'], 'unknown')
        self.assertEqual(self.admission.get(first['request_id'])['status'], 'unknown')
        second = self.admission.submit(uncertain, 'red', request_key='k')
        self.assertEqual(second, first)
        self.assertEqual(calls, [1])

    def test_pending_operation_that_becomes_unknown_is_observable(self):
        entered, release = threading.Event(), threading.Event()
        def uncertain():
            entered.set()
            release.wait(3)
            raise AdmissionOutcomeUnknown('outcome not known')
        first = self.admission.submit(uncertain, 'red', request_key='k')
        self.assertTrue(entered.is_set())
        self.assertEqual(first['status'], 'pending')
        release.set()
        self.assertTrue(self.admission.join(2))
        self.assertEqual(self.admission.get(first['request_id'])['status'], 'unknown')
        self.assertEqual(self.admission.submit(uncertain, 'red', request_key='k')['request_id'],
                         first['request_id'])

    def test_shutdown_marks_inflight_unknown_and_never_accepts_new_work(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(3)
            return 'late result'
        pending = self.admission.submit(slow, 'red', request_key='k')
        self.assertTrue(entered.is_set())
        self.admission.shutdown()
        self.assertEqual(self.admission.get(pending['request_id'])['status'], 'unknown')
        with self.assertRaises(AdmissionStopped):
            self.admission.submit(lambda: None, 'blue')
        release.set()
        time.sleep(.05)
        self.assertEqual(self.admission.get(pending['request_id'])['status'], 'unknown')

    def test_record_limit_does_not_evict_deduplication_entries(self):
        limited = OrderAdmission(wait_seconds=.1, max_records=1)
        self.addCleanup(limited.shutdown)
        self.assertEqual(limited.submit(lambda: 1, 'one', request_key='a'), 1)
        with self.assertRaises(AdmissionBusy):
            limited.submit(lambda: 2, 'two', request_key='b')
        self.assertEqual(limited.submit(lambda: 3, 'one', request_key='a'), 1)

    def test_concurrent_same_key_starts_exactly_one_operation(self):
        entered, release = threading.Event(), threading.Event()
        barrier = threading.Barrier(8)
        results, calls, failures = [], [], []
        def slow():
            calls.append(1)
            entered.set()
            release.wait(3)
            return {'ok': True}
        def submit():
            try:
                barrier.wait(2)
                results.append(self.admission.submit(slow, 'same', request_key='same'))
            except BaseException as exc:
                failures.append(exc)
        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(entered.wait(2))
            for thread in threads:
                thread.join(2)
            self.assertFalse(failures)
            self.assertEqual(len(results), 8)
            self.assertEqual(len({result['request_id'] for result in results}), 1)
            self.assertEqual(calls, [1])
        finally:
            release.set()
            for thread in threads:
                thread.join(2)

    def test_join_reports_inflight_operation_before_resource_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(3)
            return 'done'
        self.admission.submit(slow, 'slow')
        self.assertTrue(entered.is_set())
        self.admission.shutdown()
        self.assertFalse(self.admission.join(.01))
        release.set()
        self.assertTrue(self.admission.join(2))


if __name__ == '__main__':
    unittest.main()
