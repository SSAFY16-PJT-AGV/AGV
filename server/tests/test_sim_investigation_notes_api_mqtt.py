"""Reference API with the real handoff store and an isolated MQTT broker."""
import asyncio
from contextlib import closing
import json
import os
import sqlite3
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from server.sim_api import router
from server.sim_handoff_store import SimHandoffStore
from server.sim_runtime import SimRuntime
from server.tests.test_sim_investigation_notes_api import request
from server.tests import test_sim_runtime_command_observations_mqtt as fixture


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class InvestigationApiMqttTests(unittest.TestCase):
    setUp = fixture.RuntimeCommandObservationMqttTests.setUp
    peer = fixture.RuntimeCommandObservationMqttTests.peer
    wait_for = fixture.RuntimeCommandObservationMqttTests.wait_for
    prepare = fixture.RuntimeCommandObservationMqttTests.prepare

    def runtime(self, store, enabled=True):
        return SimRuntime(self.path, self.map_data,
                          {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                          port=self.port, client_id='notes-integration', field_simulation=True,
                          field_initial_positions={'sim-r1': 'A', 'sim-r2': 'X'},
                          simulation_store=store, investigation_notes=enabled)

    def call(self, runtime, method, task='T1', request_id='a' * 32, category='connection', query=b''):
        app = FastAPI()
        app.include_router(router)
        app.state.sim_api_token = 'secret-token'
        app.state.sim_runtime = runtime
        payload = json.dumps({'request_id': request_id, 'category': category}).encode()
        return asyncio.run(request(app, method, '/simulation/tasks/' + task + '/investigation-notes',
                                   payload=payload, query=query))[:2]

    def test_handoff_disconnect_restart_and_task_isolation(self):
        _peer, orders = self.peer()
        store = SimHandoffStore.create_new(self.path, simulation_enabled=True,
                                           idle_positions={'sim-r1': 'A', 'sim-r2': 'X'})
        runtime = self.runtime(store)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T2')
        _response, occupied = self.prepare(runtime, orders)
        order = store.get_order('T1')
        self.assertEqual(order['state'], 'acked')
        code, first = self.call(runtime, 'POST')
        self.assertEqual(code, 200)
        self.assertEqual(first['identity'], 'shared_token_not_person')
        self.assertEqual(first['control_effect'], 'none')
        self.assertEqual(self.call(runtime, 'POST')[1], first)
        self.assertEqual(self.call(runtime, 'POST', category='action')[0], 409)
        self.assertEqual(self.call(runtime, 'POST', task='T2', request_id='b' * 32,
                                   category='position')[0], 200)
        self.assertEqual(self.call(runtime, 'POST', task='T2')[0], 409)
        self.assertEqual(self.call(runtime, 'GET', task='unknown')[0], 409)
        code, page = self.call(runtime, 'GET')
        self.assertEqual(code, 200)
        self.assertNotIn('b' * 32, repr(page))
        runtime._client.disconnect()
        self.wait_for(lambda: not runtime.receiver.ready, 'runtime MQTT disconnect')
        blocked = set(runtime.gate._blocked)
        self.assertEqual(self.call(runtime, 'POST', request_id='c' * 32, category='action')[0], 200)
        self.assertEqual(self.call(runtime, 'GET')[0], 200)
        self.assertEqual(set(runtime.gate._blocked), blocked)
        self.assertEqual(store.list_occupancy(), occupied)
        self.assertEqual(store.get_order('T1'), order)
        identity = store.creation_id
        runtime.stop()
        self.assertEqual(self.call(runtime, 'GET')[0], 503)
        reopened = SimHandoffStore.open_existing(self.path, expected_creation_id=identity,
                     expected_idle_enabled=True, simulation_enabled=True)
        resumed = self.runtime(reopened)
        resumed.start()
        self.cleanup.callback(resumed.stop)
        self.assertIn('T1', resumed.gate._blocked)
        self.assertEqual(self.call(resumed, 'POST')[1], first)
        self.assertEqual(self.call(resumed, 'POST', request_id='d' * 32, category='other')[0], 200)
        self.assertEqual(reopened.list_occupancy(), occupied)
        self.assertEqual(reopened.get_order('T1'), order)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE sim_investigation_notes SET created_utc='private-secretZ' "
                       "WHERE request_id=?", ('a' * 32,))
            db.commit()
        for method in ('GET', 'POST'):
            code, response = self.call(resumed, method)
            self.assertEqual(code, 409)
            self.assertNotIn('private-secret', repr(response))

    def test_concurrent_duplicate_and_disabled_default(self):
        store = SimHandoffStore.create_new(self.path, simulation_enabled=True,
                                           idle_positions={'sim-r1': 'A', 'sim-r2': 'X'})
        runtime = self.runtime(store)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        barrier = threading.Barrier(3)
        results = []
        def post():
            barrier.wait()
            results.append(self.call(runtime, 'POST'))
        workers = [threading.Thread(target=post) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 200)
        self.assertEqual(len(self.call(runtime, 'GET')[1]['notes']), 1)
        runtime.stop()
        disabled = self.runtime(store, enabled=False)
        disabled.start()
        self.cleanup.callback(disabled.stop)
        self.assertEqual(self.call(disabled, 'GET')[0], 503)
        self.assertEqual(self.call(disabled, 'POST')[0], 503)

    def test_initialization_failure_releases_owner(self):
        from server.sim_investigation_notes import SimInvestigationNotes
        store = SimHandoffStore.create_new(self.path, simulation_enabled=True,
                                           idle_positions={'sim-r1': 'A', 'sim-r2': 'X'})
        runtime = self.runtime(store)
        with patch.object(SimInvestigationNotes, 'initialize', side_effect=ValueError('injected')):
            with self.assertRaises(ValueError):
                runtime.start()
        self.assertIsNone(runtime._owner.file)
        fallback = self.runtime(store, enabled=False)
        fallback.start()
        self.cleanup.callback(fallback.stop)

    def test_sqlite_writer_wait_does_not_hold_runtime_lock_and_stop_waits_for_reference(self):
        from server.sim_runtime import _DatabaseOwner
        store = SimHandoffStore.create_new(self.path, simulation_enabled=True,
                                           idle_positions={'sim-r1': 'A', 'sim-r2': 'X'})
        runtime = self.runtime(store)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        runtime.create_task('T2')
        entered, read_done, stop_done, second_done, second_started = (threading.Event() for _ in range(5))
        errors, output = [], []
        original_connect = sqlite3.connect

        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE':
                    entered.set()
                return super().execute(sql, *args)

        def connect(*args, **kwargs):
            if threading.current_thread().name == 'note-writer':
                kwargs['factory'] = ObservedConnection
            return original_connect(*args, **kwargs)

        def record():
            try:
                output.append(runtime.record_investigation_note('T1', 'a' * 32, 'connection'))
            except BaseException as exc:
                errors.append(exc)

        def read():
            try:
                runtime.task_status('T2')
                read_done.set()
            except BaseException as exc:
                errors.append(exc)

        def stop_runtime(done):
            if done is second_done:
                second_started.set()
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        with closing(original_connect(self.path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            with patch('server.sim_investigation_notes.sqlite3.connect', side_effect=connect):
                writer = threading.Thread(target=record, name='note-writer')
                reader = threading.Thread(target=read)
                stopper = threading.Thread(target=stop_runtime, args=(stop_done,))
                second_stopper = threading.Thread(target=stop_runtime, args=(second_done,))
                writer.start()
                try:
                    self.assertTrue(entered.wait(3))
                    reader.start()
                    self.assertTrue(read_done.wait(1), 'unrelated task read waited on runtime lock')
                    stopper.start()
                    self.wait_for(lambda: runtime._stopped, 'stop admission fence')
                    second_stopper.start()
                    self.assertTrue(second_started.wait(2))
                    self.assertFalse(stop_done.wait(.2), 'stop released ownership before recording finished')
                    self.assertFalse(second_done.wait(.2), 'concurrent stop returned before ownership release')
                    with self.assertRaises(RuntimeError):
                        runtime.list_investigation_notes('T1')
                    self.assertIsNotNone(runtime._owner.file)
                    competitor = _DatabaseOwner(self.path)
                    with self.assertRaises(RuntimeError):
                        competitor.acquire()
                finally:
                    blocker.execute('ROLLBACK')
                    for thread in (writer, reader, stopper, second_stopper):
                        if thread.ident is not None:
                            thread.join(10)
        self.assertTrue(stop_done.is_set())
        self.assertTrue(second_done.is_set())
        self.assertFalse(any(thread.is_alive() for thread in (writer, reader, stopper, second_stopper)))
        self.assertEqual(errors, [])
        self.assertEqual(len(output), 1)
        self.assertIsNone(runtime._owner.file)
        competitor = _DatabaseOwner(self.path)
        competitor.acquire()
        competitor.release()

    def test_reference_exception_does_not_leak_lifetime_pin(self):
        store = SimHandoffStore.create_new(self.path, simulation_enabled=True,
                                           idle_positions={'sim-r1': 'A', 'sim-r2': 'X'})
        runtime = self.runtime(store)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        with patch.object(runtime.investigation_notes, 'list', side_effect=ValueError('injected')):
            with self.assertRaises(ValueError):
                runtime.list_investigation_notes('T1')
        self.assertEqual(runtime._reference_operations, 0)
        runtime.stop()
        self.assertIsNone(runtime._owner.file)

    def test_stop_waits_for_inflight_reference_list(self):
        store = SimHandoffStore.create_new(self.path, simulation_enabled=True,
                                           idle_positions={'sim-r1': 'A', 'sim-r2': 'X'})
        runtime = self.runtime(store)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = runtime.investigation_notes.list
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('reference read was not released')
            return original(*args, **kwargs)
        def read():
            try:
                runtime.list_investigation_notes('T1')
            except BaseException as exc:
                errors.append(exc)
        def stop_runtime():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                stopped.set()
        with patch.object(runtime.investigation_notes, 'list', side_effect=delayed):
            reader, stopper = threading.Thread(target=read), threading.Thread(target=stop_runtime)
            reader.start()
            try:
                self.assertTrue(entered.wait(3))
                stopper.start()
                self.wait_for(lambda: runtime._stopped, 'stop admission fence')
                self.assertFalse(stopped.wait(.2))
                self.assertIsNotNone(runtime._owner.file)
            finally:
                release.set()
                for thread in (reader, stopper):
                    if thread.ident is not None:
                        thread.join(10)
        self.assertTrue(stopped.is_set())
        self.assertFalse(reader.is_alive() or stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(runtime._owner.file)


if __name__ == '__main__':
    unittest.main()
