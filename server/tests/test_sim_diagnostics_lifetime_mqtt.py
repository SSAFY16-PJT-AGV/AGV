"""SQLite-blocked diagnostics must not delay runtime shutdown admission."""
from contextlib import closing
import os
import sqlite3
import threading
import unittest
from unittest.mock import patch

from server.sim_runtime import SimRuntime, _DatabaseOwner
from server.tests import test_sim_runtime_command_observations_mqtt as fixture
from server.tests.test_segment_recovery_mqtt import two_segment_map


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class DiagnosticsLifetimeMqttTests(unittest.TestCase):
    setUp = fixture.RuntimeCommandObservationMqttTests.setUp
    wait_for = fixture.RuntimeCommandObservationMqttTests.wait_for

    def exercise_exclusive_wait(self, operation, marker):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='diagnostics-lifetime-test')
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        waiting, status_done, db_closed, release_diagnostic = (threading.Event() for _ in range(4))
        transport_closed, stopped = threading.Event(), threading.Event()
        errors, result = [], []
        original_connect = sqlite3.connect
        original_close = runtime._close_client

        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if marker in sql:
                    waiting.set()
                return super().execute(sql, *args)
            def close(self):
                super().close()
                db_closed.set()
                if not release_diagnostic.wait(10):
                    raise RuntimeError('test did not release diagnostics')

        def connect(*args, **kwargs):
            if threading.current_thread().name == 'diagnostics-reader':
                kwargs['factory'] = ObservedConnection
            return original_connect(*args, **kwargs)

        def close(client):
            original_close(client)
            transport_closed.set()

        def diagnostics():
            try:
                result.append(operation(runtime))
            except BaseException as exc:
                errors.append(exc)

        def status():
            try:
                runtime.status()
                status_done.set()
            except BaseException as exc:
                errors.append(exc)

        def shutdown():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                stopped.set()

        with closing(original_connect(self.path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            with patch('server.sim_runtime.sqlite3.connect', side_effect=connect), \
                 patch.object(runtime, '_close_client', side_effect=close):
                reader = threading.Thread(target=diagnostics, name='diagnostics-reader')
                status_reader = threading.Thread(target=status)
                stopper = threading.Thread(target=shutdown)
                reader.start()
                try:
                    self.assertTrue(waiting.wait(3))
                    status_reader.start()
                    self.assertTrue(status_done.wait(1), 'diagnostics blocks unrelated status')
                    stopper.start()
                    self.wait_for(lambda: runtime._stopped, 'shutdown admission')
                    self.assertTrue(runtime.workflow._stopping)
                    self.assertFalse(stopped.is_set())
                    blocker.execute('ROLLBACK')
                    self.assertTrue(db_closed.wait(5), 'diagnostic connection did not close')
                    self.assertTrue(transport_closed.wait(5), 'transport did not stop')
                    self.assertFalse(stopped.is_set(), 'owner released while diagnostics still running')
                    self.assertIsNotNone(runtime._owner.file)
                    competitor = _DatabaseOwner(self.path)
                    with self.assertRaises(RuntimeError):
                        competitor.acquire()
                finally:
                    if blocker.in_transaction:
                        blocker.execute('ROLLBACK')
                    release_diagnostic.set()
                    for thread in (reader, status_reader, stopper):
                        if thread.ident is not None:
                            thread.join(12)
        self.assertFalse(any(thread.is_alive() for thread in (reader, status_reader, stopper)))
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)
        self.assertTrue(stopped.is_set())
        self.assertIsNone(runtime._owner.file)

    def test_diagnostics_database_wait_does_not_block_runtime_or_release_owner(self):
        self.exercise_exclusive_wait(lambda runtime: runtime.task_diagnostics('T1'), 'FROM tasks WHERE task_id=')

    def test_task_status_database_wait_does_not_block_runtime_or_release_owner(self):
        self.exercise_exclusive_wait(lambda runtime: runtime.task_status('T1'), 'FROM tasks WHERE task_id=')

    def test_pending_events_database_wait_does_not_block_runtime_or_release_owner(self):
        self.exercise_exclusive_wait(lambda runtime: runtime.pending_events(), 'CREATE TEMP TABLE allowed')


if __name__ == '__main__':
    unittest.main()
