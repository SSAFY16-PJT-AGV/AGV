"""Fresh synthetic pickup attempt storage; no peer, MQTT, or physical evidence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib
import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


CATALOG = [dict(id='red', product_type='Red', pickup_node='D01',
                destination_node='D04', shelf_id='SHELF_RED', slot_count=3)]
SOURCES = {'D01': 'Red'}
WORLD = 'a' * 32


class SyntheticTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session'

    def api(self):
        try:
            return importlib.import_module('server.synthetic_action_transaction').SyntheticActionSession
        except ModuleNotFoundError as exc:
            if exc.name != 'server.synthetic_action_transaction':
                raise
            self.fail('fresh synthetic transaction boundary is not implemented')

    def create(self):
        session = self.api().create_new(self.path, map_id='map-v1', catalog=CATALOG,
                                        pickup_sources=SOURCES, world_id=WORLD,
                                        robot_twins={'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'})
        self.addCleanup(session.close)
        return session

    def seed_test_reservation(self):
        """Only a fixture: these are proposed simulated order/reservation rows."""
        result = {'assigned_robot': 'sim-r1', 'order_id': 'web-pickup',
                  'shelf_id': 'SHELF_RED', 'slot_index': 0,
                  'reservation_id': 'slot-1', 'pickup_reservation_id': 'pickup-1'}
        with closing(sqlite3.connect(self.path / 'orders.sqlite3')) as db:
            db.execute("INSERT INTO orders(request_id,fingerprint,product_id,state,result_json) "
                       "VALUES ('request-1','product:red','red','active',?)", (json.dumps(result),))
            db.execute("INSERT INTO slot_reservations VALUES ('slot-1','SHELF_RED',0,'request-1','reserved')")
            db.commit()
        with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
            db.execute("UPDATE counts SET available=1 WHERE source_id='D01'")
            db.execute("UPDATE metadata SET revision=1 WHERE singleton=1")
            db.execute("INSERT INTO reservations(reservation_id,request_id,source_id,product_type,state) "
                       "VALUES ('pickup-1','request-1','D01','Red','reserved')")
            db.commit()

    def stage(self, session, **overrides):
        args = dict(request_id='request-1', task_id='web-pickup', action_id='action-1',
                    agv_id='AGV_01', action='grab')
        args.update(overrides)
        return session.stage_attempt(**args)

    def result(self, session, outcome, **overrides):
        self.assertTrue(hasattr(session, 'record_result'), 'synthetic result boundary is not implemented')
        self.assertTrue(hasattr(session, 'session_id'), 'synthetic generation is not exposed')
        args = dict(request_id='request-1', task_id='web-pickup', action_id='action-1',
                    agv_id='AGV_01', world_id=WORLD, session_id=session.session_id, outcome=outcome)
        args.update(overrides)
        return session.record_result(**args)

    def seed_extra_reservation(self, *, number, task, robot):
        result = {'assigned_robot': robot, 'order_id': task, 'shelf_id': 'SHELF_RED',
                  'slot_index': number - 1, 'reservation_id': f'slot-{number}',
                  'pickup_reservation_id': f'pickup-{number}'}
        with closing(sqlite3.connect(self.path / 'orders.sqlite3')) as db:
            db.execute("INSERT INTO orders(request_id,fingerprint,product_id,state,result_json) "
                       "VALUES (?,'product:red','red','active',?)", (f'request-{number}', json.dumps(result)))
            db.execute("INSERT INTO slot_reservations VALUES (?, 'SHELF_RED', ?, ?, 'reserved')",
                       (f'slot-{number}', number - 1, f'request-{number}'))
            db.commit()
        with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
            db.execute("UPDATE counts SET available=3 WHERE source_id='D01'")
            db.execute("INSERT INTO reservations(reservation_id,request_id,source_id,product_type,state) "
                       "VALUES (?,?,'D01','Red','reserved')", (f'pickup-{number}', f'request-{number}'))
            db.commit()

    def database_state(self):
        with closing(sqlite3.connect(self.path / 'orders.sqlite3')) as db:
            attempts = db.execute('SELECT request_id,task_id,action_id,agv_id,action,state '
                                  'FROM synthetic_action_attempt').fetchall()
            slot = db.execute('SELECT state FROM slot_reservations').fetchall()
        with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
            pickup = db.execute('SELECT state,order_id,action_id,agv_id FROM reservations').fetchall()
            count = db.execute('SELECT available FROM counts').fetchall()
        return attempts, slot, pickup, count

    def test_grab_attempt_and_inflight_reservation_are_saved_together(self):
        session = self.create()
        self.seed_test_reservation()
        result = self.stage(session)
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(self.database_state(),
                         ([('request-1', 'web-pickup', 'action-1', 'AGV_01', 'grab', 'uncertain')],
                          [('reserved',)], [('inflight', 'web-pickup', 'action-1', 'AGV_01')], [(1,)]))
        with self.assertRaises((ValueError, RuntimeError)):
            self.stage(session)
        self.assertEqual(len(self.database_state()[0]), 1)

    def test_applied_result_consumes_stock_once_and_duplicate_is_idempotent(self):
        session = self.create()
        self.seed_test_reservation()
        self.stage(session)
        self.assertEqual(self.result(session, 'applied')['outcome'], 'applied')
        attempts, slots, pickup, counts = self.database_state()
        self.assertEqual(attempts, [('request-1', 'web-pickup', 'action-1', 'AGV_01', 'grab', 'applied')])
        self.assertEqual(slots, [('reserved',)])
        self.assertEqual(pickup, [('consumed', 'web-pickup', 'action-1', 'AGV_01')])
        self.assertEqual(counts, [(0,)])
        before = self.database_state()
        self.assertEqual(self.result(session, 'applied')['outcome'], 'applied')
        self.assertEqual(self.database_state(), before)
        with self.assertRaises((ValueError, RuntimeError)):
            self.result(session, 'not_applied')
        self.assertEqual(self.database_state(), before)

    def test_not_applied_or_unknown_holds_slot_and_blocks_new_attempts(self):
        for outcome, pickup_state in (('not_applied', 'released'), ('unknown', 'unknown')):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'session'
                session = self.create()
                try:
                    self.seed_test_reservation()
                    self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r2')
                    self.stage(session)
                    self.assertEqual(self.result(session, outcome)['outcome'], outcome)
                    attempts, slots, pickup, counts = self.database_state()
                    self.assertEqual(attempts[0][-1], outcome)
                    self.assertEqual([row[0] for row in slots], ['reserved', 'reserved'])
                    self.assertEqual(pickup[0][0], pickup_state)
                    self.assertEqual(pickup[1][0], 'reserved')
                    self.assertEqual(counts, [(3,)])
                    before = self.database_state()
                    self.assertEqual(self.result(session, outcome)['outcome'], outcome)
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.stage(session, request_id='request-2', task_id='web-pickup-2',
                                   action_id='action-2', agv_id='AGV_02')
                    self.assertEqual(self.database_state(), before)
                    self.assertEqual(self.result(session, outcome)['outcome'], outcome)
                finally:
                    session.close()

    def test_two_independent_applied_results_decrement_stock_once_each(self):
        session = self.create()
        self.seed_test_reservation()
        self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r2')
        self.stage(session)
        self.stage(session, request_id='request-2', task_id='web-pickup-2', action_id='action-2', agv_id='AGV_02')
        self.assertEqual(self.result(session, 'applied')['outcome'], 'applied')
        self.assertEqual(self.result(session, 'applied', request_id='request-2', task_id='web-pickup-2',
                                     action_id='action-2', agv_id='AGV_02')['outcome'], 'applied')
        attempts, slots, pickups, counts = self.database_state()
        self.assertEqual([row[-1] for row in attempts], ['applied', 'applied'])
        self.assertEqual([row[0] for row in pickups], ['consumed', 'consumed'])
        self.assertEqual(slots, [('reserved',), ('reserved',)])
        self.assertEqual(counts, [(1,)])

    def test_unknown_result_blocks_another_inflight_result_but_allows_duplicate(self):
        session = self.create()
        self.seed_test_reservation()
        self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r2')
        self.stage(session)
        self.stage(session, request_id='request-2', task_id='web-pickup-2', action_id='action-2', agv_id='AGV_02')
        self.result(session, 'unknown')
        before = self.database_state()
        with self.assertRaises(RuntimeError):
            self.result(session, 'applied', request_id='request-2', task_id='web-pickup-2',
                        action_id='action-2', agv_id='AGV_02')
        self.assertEqual(self.database_state(), before)
        self.assertTrue(self.result(session, 'unknown')['duplicate'])

    def test_corrupt_resolved_pickup_blocks_other_result_without_second_decrement(self):
        session = self.create()
        self.seed_test_reservation()
        self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r2')
        self.stage(session)
        self.stage(session, request_id='request-2', task_id='web-pickup-2', action_id='action-2', agv_id='AGV_02')
        self.result(session, 'applied')
        with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
            db.execute("UPDATE reservations SET state='unknown' WHERE reservation_id='pickup-1'")
            db.commit()
        before = self.database_state()
        with self.assertRaises((ValueError, RuntimeError)):
            self.result(session, 'applied', request_id='request-2', task_id='web-pickup-2',
                        action_id='action-2', agv_id='AGV_02')
        self.assertEqual(self.database_state(), before)

    def test_freshness_fence_corruption_cannot_reenable_progress(self):
        session = self.create()
        self.seed_test_reservation()
        self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r2')
        self.stage(session)
        self.result(session, 'not_applied')
        with closing(sqlite3.connect(self.path / 'orders.sqlite3')) as db:
            db.execute('UPDATE synthetic_action_session SET progress_blocked=0')
            db.commit()
        before = self.database_state()
        with self.assertRaises((ValueError, RuntimeError)):
            self.stage(session, request_id='request-2', task_id='web-pickup-2',
                       action_id='action-2', agv_id='AGV_02')
        self.assertEqual(self.database_state(), before)

    def test_orphan_resolved_pickup_blocks_other_result(self):
        for state in ('consumed', 'released'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'session'
                session = self.create()
                try:
                    self.seed_test_reservation()
                    self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r2')
                    with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
                        db.execute("UPDATE reservations SET state=?,action_id='orphan-action' "
                                   "WHERE reservation_id='pickup-1'", (state,))
                        db.commit()
                    before = self.database_state()
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.stage(session, request_id='request-2', task_id='web-pickup-2',
                                   action_id='action-2', agv_id='AGV_02')
                    self.assertEqual(self.database_state(), before)
                finally:
                    session.close()

    def test_result_rejects_wrong_identity_or_generation_without_mutation(self):
        scenarios = ({'request_id': 'wrong'}, {'task_id': 'wrong'}, {'action_id': 'wrong'},
                     {'agv_id': 'AGV_02'}, {'world_id': 'b' * 32}, {'session_id': 'f' * 64})
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'session'
                session = self.create()
                try:
                    self.seed_test_reservation()
                    self.stage(session)
                    before = self.database_state()
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.result(session, 'applied', **scenario)
                    self.assertEqual(self.database_state(), before)
                finally:
                    session.close()

    def test_database_error_rolls_back_result_and_latches(self):
        session = self.create()
        self.seed_test_reservation()
        self.stage(session)
        before = self.database_state()
        module = importlib.import_module('server.synthetic_action_transaction')
        original = module._open_connection
        def deny_count_update(pair):
            db = original(pair)
            def authorize(kind, arg1, arg2, name, trigger):
                if kind == sqlite3.SQLITE_UPDATE and name == 'pickup' and arg1 == 'counts':
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            return db
        with patch.object(module, '_open_connection', side_effect=deny_count_update):
            with self.assertRaises((ValueError, RuntimeError)):
                self.result(session, 'applied')
        self.assertEqual(self.database_state(), before)
        with self.assertRaises(RuntimeError):
            self.result(session, 'applied')

    def test_result_commit_then_failure_preserves_confirmed_count_and_blocks(self):
        session = self.create()
        self.seed_test_reservation()
        self.stage(session)
        original = session._pair._validate_live
        calls = []
        def fail_after_commit():
            calls.append(True)
            if len(calls) == 2:
                raise ValueError('post-result commit check failed')
            original()
        with patch.object(session._pair, '_validate_live', side_effect=fail_after_commit):
            with self.assertRaisesRegex(ValueError, 'post-result'):
                self.result(session, 'applied')
        attempts, slots, pickup, counts = self.database_state()
        self.assertEqual(attempts[0][-1], 'applied')
        self.assertEqual(slots, [('reserved',)])
        self.assertEqual(pickup[0][0], 'consumed')
        self.assertEqual(counts, [(0,)])
        with self.assertRaises(RuntimeError):
            self.result(session, 'applied')

    def test_result_transaction_holds_both_database_writer_locks(self):
        session = self.create()
        self.seed_test_reservation()
        self.stage(session)
        entered, release = threading.Event(), threading.Event()
        module = importlib.import_module('server.synthetic_action_transaction')
        original = module._open_connection
        def pause_after_begin(pair):
            db = original(pair)
            def trace(statement):
                if 'FROM main.synthetic_pair_binding' in statement:
                    entered.set()
                    release.wait(5)
            db.set_trace_callback(trace)
            return db
        with patch.object(module, '_open_connection', side_effect=pause_after_begin):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.result, session, 'applied')
                try:
                    self.assertTrue(entered.wait(5))
                    for name in ('orders.sqlite3', 'pickup-inventory.sqlite3'):
                        with closing(sqlite3.connect(self.path / name, isolation_level=None, timeout=0)) as db:
                            with self.assertRaises(sqlite3.OperationalError) as error:
                                db.execute('BEGIN IMMEDIATE')
                            self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=5)['outcome'], 'applied')

    def test_result_conflicting_concurrent_calls_never_consume_twice(self):
        session = self.create()
        self.seed_test_reservation()
        self.stage(session)
        entered, release = threading.Event(), threading.Event()
        module = importlib.import_module('server.synthetic_action_transaction')
        original = module._open_connection
        def pause_after_begin(pair):
            db = original(pair)
            def trace(statement):
                if 'FROM main.synthetic_pair_binding' in statement:
                    entered.set()
                    release.wait(5)
            db.set_trace_callback(trace)
            return db
        with patch.object(module, '_open_connection', side_effect=pause_after_begin):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.result, session, 'applied')
                try:
                    self.assertTrue(entered.wait(5))
                    with self.assertRaises(RuntimeError):
                        self.result(session, 'not_applied')
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=5)['outcome'], 'applied')
        self.assertEqual(self.database_state()[3], [(0,)])

    def test_same_task_or_same_robot_cannot_get_another_uncertain_attempt(self):
        for task, robot, agv in (('web-pickup-2', 'sim-r1', 'AGV_01'),
                                 ('web-pickup', 'sim-r2', 'AGV_02')):
            with self.subTest(task=task, robot=robot), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'session'
                session = self.create()
                try:
                    self.seed_test_reservation()
                    self.seed_extra_reservation(number=2, task=task, robot=robot)
                    self.stage(session)
                    before = self.database_state()
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.stage(session, request_id='request-2', task_id=task,
                                   action_id='action-2', agv_id=agv)
                    self.assertEqual(self.database_state(), before)
                finally:
                    session.close()

    def test_distinct_tasks_and_robots_can_each_record_one_attempt(self):
        session = self.create()
        self.seed_test_reservation()
        self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r2')
        self.assertEqual(self.stage(session)['state'], 'uncertain')
        self.assertEqual(self.stage(session, request_id='request-2', task_id='web-pickup-2',
                                    action_id='action-2', agv_id='AGV_02')['state'], 'uncertain')
        attempts, slots, pickups, counts = self.database_state()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(slots, [('reserved',), ('reserved',)])
        self.assertEqual([row[0] for row in pickups], ['inflight', 'inflight'])
        self.assertEqual(counts, [(3,)])

    def test_preexisting_duplicate_task_or_robot_fails_closed(self):
        for duplicate_task, duplicate_agv in (('web-pickup', 'AGV_02'),
                                               ('web-pickup-2', 'AGV_01')):
            with self.subTest(task=duplicate_task, agv=duplicate_agv), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'session'
                session = self.create()
                try:
                    self.seed_test_reservation()
                    self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r1')
                    self.seed_extra_reservation(number=3, task='web-pickup-3', robot='sim-r2')
                    self.stage(session)
                    with closing(sqlite3.connect(self.path / 'orders.sqlite3')) as db:
                        db.execute("INSERT INTO synthetic_action_attempt "
                                   "SELECT 'injected-2','request-2',?,?,action,state,generation,pair_id,world_id,"
                                   "'pickup-2','slot-2' FROM synthetic_action_attempt WHERE action_id='action-1'",
                                   (duplicate_task, duplicate_agv))
                        db.commit()
                    before = self.database_state()
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.stage(session, request_id='request-3', task_id='web-pickup-3',
                                   action_id='action-3', agv_id='AGV_02')
                    self.assertEqual(self.database_state(), before)
                finally:
                    session.close()

    def test_existing_attempt_must_match_current_order_slot_and_pickup(self):
        scenarios = (
            ('pickup-inventory.sqlite3', "UPDATE reservations SET state='released' WHERE reservation_id='pickup-1'"),
            ('pickup-inventory.sqlite3', "UPDATE reservations SET state='unknown' WHERE reservation_id='pickup-1'"),
            ('pickup-inventory.sqlite3', "UPDATE reservations SET order_id='wrong' WHERE reservation_id='pickup-1'"),
            ('pickup-inventory.sqlite3', "UPDATE reservations SET action_id='wrong' WHERE reservation_id='pickup-1'"),
            ('pickup-inventory.sqlite3', "UPDATE reservations SET agv_id='AGV_02' WHERE reservation_id='pickup-1'"),
            ('pickup-inventory.sqlite3', "UPDATE reservations SET session_id='wrong' WHERE reservation_id='pickup-1'"),
            ('pickup-inventory.sqlite3', "UPDATE reservations SET dispatch_revision=99 WHERE reservation_id='pickup-1'"),
            ('pickup-inventory.sqlite3', "DELETE FROM reservations WHERE reservation_id='pickup-1'"),
            ('orders.sqlite3', "UPDATE slot_reservations SET state='confirmed' WHERE reservation_id='slot-1'"),
            ('orders.sqlite3', "DELETE FROM slot_reservations WHERE reservation_id='slot-1'"),
            ('orders.sqlite3', "UPDATE orders SET state='completed' WHERE request_id='request-1'"),
            ('orders.sqlite3', "UPDATE orders SET result_json=replace(result_json,'web-pickup','wrong') WHERE request_id='request-1'"),
            ('orders.sqlite3', "UPDATE orders SET result_json=replace(result_json,'sim-r1','sim-r2') WHERE request_id='request-1'"),
            ('orders.sqlite3', "UPDATE orders SET result_json=replace(result_json,'pickup-1','pickup-2') WHERE request_id='request-1'"),
            ('orders.sqlite3', "DELETE FROM orders WHERE request_id='request-1'"),
        )
        for filename, mutation in scenarios:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'session'
                session = self.create()
                try:
                    self.seed_test_reservation()
                    self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r2')
                    self.stage(session)
                    with closing(sqlite3.connect(self.path / filename)) as db:
                        db.execute(mutation)
                        db.commit()
                    before = self.database_state()
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.stage(session, request_id='request-2', task_id='web-pickup-2',
                                   action_id='action-2', agv_id='AGV_02')
                    self.assertEqual(self.database_state(), before)
                finally:
                    session.close()

    def test_orphan_inflight_or_unknown_pickup_blocks_another_robot(self):
        for orphan_state in ('inflight', 'unknown'):
            with self.subTest(state=orphan_state), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'session'
                session = self.create()
                try:
                    self.seed_test_reservation()
                    self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r2')
                    with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
                        db.execute('UPDATE reservations SET state=?,order_id=?,action_id=?,agv_id=?,session_id=? '
                                   "WHERE reservation_id='pickup-1'",
                                   (orphan_state, 'web-pickup', 'orphan-action', 'AGV_01', 'orphan-session'))
                        db.commit()
                    before = self.database_state()
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.stage(session, request_id='request-2', task_id='web-pickup-2',
                                   action_id='action-2', agv_id='AGV_02')
                    self.assertEqual(self.database_state(), before)
                    with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
                        db.execute("UPDATE reservations SET state='reserved',order_id=NULL,action_id=NULL, "
                                   "agv_id=NULL,session_id=NULL WHERE reservation_id='pickup-1'")
                        db.commit()
                    with self.assertRaises(RuntimeError):
                        self.stage(session, request_id='request-2', task_id='web-pickup-2',
                                   action_id='action-2', agv_id='AGV_02')
                finally:
                    session.close()

    def test_reserved_pickup_with_any_dispatch_field_blocks_all_new_attempts(self):
        fields = (('order_id', 'old-task'), ('action_id', 'old-action'),
                  ('agv_id', 'AGV_01'), ('session_id', 'old-session'),
                  ('dispatch_revision', 1))
        for reservation in ('pickup-1', 'pickup-2'):
            for column, value in fields:
                with self.subTest(reservation=reservation, column=column), tempfile.TemporaryDirectory() as folder:
                    self.path = Path(folder) / 'session'
                    session = self.create()
                    try:
                        self.seed_test_reservation()
                        self.seed_extra_reservation(number=2, task='web-pickup-2', robot='sim-r2')
                        with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
                            db.execute(f'UPDATE reservations SET {column}=? WHERE reservation_id=?',
                                       (value, reservation))
                            db.commit()
                            before = db.execute('SELECT * FROM reservations ORDER BY reservation_id').fetchall()
                        with self.assertRaises((ValueError, RuntimeError)):
                            self.stage(session)
                        with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
                            self.assertEqual(db.execute('SELECT * FROM reservations ORDER BY reservation_id').fetchall(),
                                             before)
                        self.assertEqual(self.database_state()[0], [])
                    finally:
                        session.close()

    def test_mismatch_or_place_never_releases_or_consumes_reservations(self):
        for wrong in ({'task_id': 'wrong'}, {'agv_id': 'AGV_02'}, {'action': 'place'}):
            with self.subTest(wrong=wrong), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'session'
                session = self.create()
                self.seed_test_reservation()
                before = self.database_state()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.stage(session, **wrong)
                self.assertEqual(self.database_state(), before)
                session.close()

    def test_insert_failure_rolls_back_pickup_update_and_latches_gate(self):
        session = self.create()
        self.seed_test_reservation()
        before = self.database_state()
        module = importlib.import_module('server.synthetic_action_transaction')
        original = module._open_connection
        def deny_attempt_insert(pair):
            db = original(pair)
            def authorize(kind, arg1, arg2, name, trigger):
                if kind == sqlite3.SQLITE_INSERT and arg1 == 'synthetic_action_attempt':
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            return db
        with patch.object(module, '_open_connection', side_effect=deny_attempt_insert):
            with self.assertRaises((ValueError, RuntimeError)):
                self.stage(session)
        self.assertEqual(self.database_state(), before)
        with self.assertRaises(RuntimeError):
            self.stage(session)

    def test_transaction_holds_both_database_writer_locks(self):
        session = self.create()
        self.seed_test_reservation()
        entered, release = threading.Event(), threading.Event()
        module = importlib.import_module('server.synthetic_action_transaction')
        original = module._open_connection
        def pause_after_begin(pair):
            db = original(pair)
            def trace(statement):
                if 'FROM main.synthetic_pair_binding' in statement:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('test barrier timed out')
            db.set_trace_callback(trace)
            return db
        with patch.object(module, '_open_connection', side_effect=pause_after_begin):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.stage, session)
                try:
                    self.assertTrue(entered.wait(5))
                    for name in ('orders.sqlite3', 'pickup-inventory.sqlite3'):
                        with closing(sqlite3.connect(self.path / name, isolation_level=None, timeout=0)) as db:
                            with self.assertRaises(sqlite3.OperationalError) as error:
                                db.execute('BEGIN IMMEDIATE')
                            self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=5)['state'], 'uncertain')

    def test_concurrent_attempt_is_rejected_while_first_holds_transaction(self):
        session = self.create()
        self.seed_test_reservation()
        entered, release = threading.Event(), threading.Event()
        module = importlib.import_module('server.synthetic_action_transaction')
        original = module._open_connection
        def pause_after_begin(pair):
            db = original(pair)
            def trace(statement):
                if 'FROM main.synthetic_pair_binding' in statement:
                    entered.set()
                    release.wait(5)
            db.set_trace_callback(trace)
            return db
        with patch.object(module, '_open_connection', side_effect=pause_after_begin):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.stage, session)
                try:
                    self.assertTrue(entered.wait(5))
                    with self.assertRaises(RuntimeError):
                        self.stage(session, action_id='action-2')
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=5)['state'], 'uncertain')
        self.assertEqual(len(self.database_state()[0]), 1)

    def test_world_mismatch_latches_session_and_restart_never_resumes(self):
        session = self.create()
        self.seed_test_reservation()
        with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
            db.execute("UPDATE metadata SET world_id=?", ('b' * 32,))
            db.commit()
        before = self.database_state()
        with self.assertRaises((ValueError, RuntimeError)):
            self.stage(session)
        self.assertEqual(self.database_state(), before)
        session.close()
        with self.assertRaises(RuntimeError):
            self.api().open_existing(self.path)

    def test_failure_after_commit_preserves_uncertain_and_blocks_session(self):
        session = self.create()
        self.seed_test_reservation()
        original = session._pair._validate_live
        calls = []
        def fail_after_commit():
            calls.append(True)
            if len(calls) == 2:
                raise ValueError('post-commit check failed')
            original()
        with patch.object(session._pair, '_validate_live', side_effect=fail_after_commit):
            with self.assertRaisesRegex(ValueError, 'post-commit'):
                self.stage(session)
        attempts, slot, pickup, counts = self.database_state()
        self.assertEqual(attempts, [('request-1', 'web-pickup', 'action-1', 'AGV_01', 'grab', 'uncertain')])
        self.assertEqual(slot, [('reserved',)])
        self.assertEqual(pickup, [('inflight', 'web-pickup', 'action-1', 'AGV_01')])
        self.assertEqual(counts, [(1,)])
        with self.assertRaises(RuntimeError):
            self.stage(session, action_id='action-2')

    def test_source_change_after_connection_is_rechecked_inside_transaction(self):
        session = self.create()
        self.seed_test_reservation()
        before = self.database_state()
        module = importlib.import_module('server.synthetic_action_transaction')
        original = module._open_connection
        def change_after_open(pair):
            db = original(pair)
            with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as other:
                other.execute('PRAGMA user_version=1')
            return db
        with patch.object(module, '_open_connection', side_effect=change_after_open):
            with self.assertRaises((ValueError, RuntimeError)):
                self.stage(session)
        self.assertEqual(self.database_state(), before)
        with closing(sqlite3.connect(self.path / 'pickup-inventory.sqlite3')) as db:
            db.execute('PRAGMA user_version=2')
        with self.assertRaises(RuntimeError):
            self.stage(session)

    def test_marker_content_change_after_open_rolls_back_before_recording(self):
        session = self.create()
        self.seed_test_reservation()
        before = self.database_state()
        module = importlib.import_module('server.synthetic_action_transaction')
        original = module._open_connection
        def tamper_after_open(pair):
            db = original(pair)
            marker = self.path / 'pair-ready.json'
            data = json.loads(marker.read_text(encoding='utf-8'))
            data['pair_id'] = 'f' * 64
            marker.write_text(json.dumps(data), encoding='utf-8')
            return db
        with patch.object(module, '_open_connection', side_effect=tamper_after_open):
            with self.assertRaises((ValueError, RuntimeError)):
                self.stage(session)
        self.assertEqual(self.database_state(), before)

    def test_wal_or_trigger_blocks_attempt_without_mutating_reservations(self):
        for corruption in ('wal', 'trigger'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'session'
                session = self.create()
                self.seed_test_reservation()
                before = self.database_state()
                with closing(sqlite3.connect(self.path / 'orders.sqlite3')) as db:
                    if corruption == 'wal':
                        self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                    else:
                        db.execute('CREATE TRIGGER unexpected AFTER INSERT ON synthetic_action_attempt '
                                   'BEGIN SELECT 1; END')
                        db.commit()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.stage(session)
                self.assertEqual(self.database_state(), before)
                session.close()

    def test_changed_reservation_schema_latches_without_action(self):
        session = self.create()
        self.seed_test_reservation()
        before = self.database_state()
        with closing(sqlite3.connect(self.path / 'orders.sqlite3')) as db:
            db.execute('DROP INDEX unique_held_slot')
            db.commit()
        with self.assertRaises((ValueError, RuntimeError)):
            self.stage(session)
        self.assertEqual(self.database_state(), before)

    def test_ambiguous_assignment_json_is_rejected(self):
        session = self.create()
        self.seed_test_reservation()
        with closing(sqlite3.connect(self.path / 'orders.sqlite3')) as db:
            row = db.execute("SELECT result_json FROM orders WHERE request_id='request-1'").fetchone()[0]
            ambiguous = row.replace('{', '{"order_id":"wrong",', 1)
            db.execute("UPDATE orders SET result_json=? WHERE request_id='request-1'", (ambiguous,))
            db.commit()
        before = self.database_state()
        with self.assertRaises((ValueError, RuntimeError)):
            self.stage(session)
        self.assertEqual(self.database_state(), before)

    def test_process_death_between_two_database_writes_rolls_back_and_cannot_resume(self):
        self.api()
        code = r'''
import json, sys
import server.synthetic_action_transaction as transaction
from server.synthetic_action_transaction import SyntheticActionSession
data = json.loads(sys.stdin.readline())
session = SyntheticActionSession.create_new(**data)
print('ready', flush=True)
sys.stdin.readline()
old = transaction._open_connection
def wrapped(pair):
    db = old(pair)
    def trace(sql):
        if sql.startswith('INSERT INTO main.synthetic_action_attempt'):
            print('after-pickup-update', flush=True)
            sys.stdin.readline()
    db.set_trace_callback(trace)
    return db
transaction._open_connection = wrapped
session.stage_attempt(request_id='request-1', task_id='web-pickup', action_id='action-1',
                      agv_id='AGV_01', action='grab')
'''
        process = subprocess.Popen([sys.executable, '-u', '-c', code], text=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = queue.Queue()
        def collect():
            for line in process.stdout:
                output.put(line.strip())
        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            data = dict(path=str(self.path), map_id='map-v1', catalog=CATALOG, pickup_sources=SOURCES,
                        world_id=WORLD, robot_twins={'sim-r1': 'AGV_01'})
            process.stdin.write(json.dumps(data) + '\n')
            process.stdin.flush()
            self.assertEqual(output.get(timeout=20), 'ready')
            self.seed_test_reservation()
            process.stdin.write('go\n')
            process.stdin.flush()
            self.assertEqual(output.get(timeout=20), 'after-pickup-update')
            process.kill()
            process.wait(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(self.database_state(),
                             ([], [('reserved',)], [('reserved', None, None, None)], [(1,)]))
            with self.assertRaises(RuntimeError):
                self.api().open_existing(self.path)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            reader.join(timeout=5)

    def test_result_process_death_before_attempt_update_preserves_uncertainty(self):
        self.api()
        code = r'''
import json, sys
import server.synthetic_action_transaction as transaction
from server.synthetic_action_transaction import SyntheticActionSession
data = json.loads(sys.stdin.readline())
session = SyntheticActionSession.create_new(**data)
print('ready', flush=True)
sys.stdin.readline()
session.stage_attempt(request_id='request-1', task_id='web-pickup', action_id='action-1',
                      agv_id='AGV_01', action='grab')
print('staged', flush=True)
sys.stdin.readline()
old = transaction._open_connection
def wrapped(pair):
    db = old(pair)
    def trace(sql):
        if sql.startswith('UPDATE main.synthetic_action_attempt SET state='):
            print('after-pickup-result', flush=True)
            sys.stdin.readline()
    db.set_trace_callback(trace)
    return db
transaction._open_connection = wrapped
session.record_result(request_id='request-1', task_id='web-pickup', action_id='action-1',
                      agv_id='AGV_01', world_id=data['world_id'], session_id=session.session_id,
                      outcome='applied')
'''
        process = subprocess.Popen([sys.executable, '-u', '-c', code], text=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = queue.Queue()
        def collect():
            for line in process.stdout:
                output.put(line.strip())
        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            data = dict(path=str(self.path), map_id='map-v1', catalog=CATALOG, pickup_sources=SOURCES,
                        world_id=WORLD, robot_twins={'sim-r1': 'AGV_01'})
            process.stdin.write(json.dumps(data) + '\n')
            process.stdin.flush()
            self.assertEqual(output.get(timeout=20), 'ready')
            self.seed_test_reservation()
            process.stdin.write('go\n')
            process.stdin.flush()
            self.assertEqual(output.get(timeout=20), 'staged')
            process.stdin.write('go\n')
            process.stdin.flush()
            self.assertEqual(output.get(timeout=20), 'after-pickup-result')
            process.kill()
            process.wait(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(self.database_state(),
                             ([('request-1', 'web-pickup', 'action-1', 'AGV_01', 'grab', 'uncertain')],
                              [('reserved',)], [('inflight', 'web-pickup', 'action-1', 'AGV_01')], [(1,)]))
            with self.assertRaises(RuntimeError):
                self.api().open_existing(self.path)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            reader.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
