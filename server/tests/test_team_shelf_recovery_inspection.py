"""Investigate a stopped team-shelf demo without updating its databases."""
from pathlib import Path
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
import io
from contextlib import redirect_stdout, redirect_stderr

from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_persistent_orders import TeamShelfPersistentOrders
from server.tools.inspect_team_shelf_recovery import inspect_state, InspectionUnavailable, main
from server.tests.test_team_shelf_order_slots import snapshot


PRODUCTS = [{'id': 'milk', 'product_type': 'milk', 'pickup_node': 'D01',
             'destination_node': 'D02', 'shelf_id': 'N02', 'slot_count': 4}]


class TeamShelfRecoveryInspectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='team-inspect-')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.slots = TeamShelfOrderSlots.create_new(self.folder / 'team-slots.sqlite3')
        self.session = self.slots.begin_session('a' * 32)
        self.slots.apply_snapshot(self.session, snapshot(), fence=self.slots.begin_snapshot(self.session))
        self.orders = TeamShelfPersistentOrders(self.folder / 'orders.sqlite3', 'v3', PRODUCTS, self.slots)
        self.order = self.orders.submit('milk', 'TOP_SECRET_REQUEST_KEY')
        self.slots.reserve(self.session, self.order['request_id'], 'milk')

    def contents(self):
        return {p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}

    def test_inspection_is_read_only_and_never_reports_uncertain_as_completed_or_empty(self):
        before = self.contents()
        result = inspect_state(self.folder)
        self.assertEqual(before, self.contents())
        self.assertEqual(result['scope'], 'stopped_team_shelf_record_inspection')
        self.assertFalse(result['motion_authorized'])
        self.assertEqual(result['orders'][0]['request_id'], self.order['request_id'])
        self.assertEqual(result['orders'][0]['status'], 'unknown')
        self.assertEqual(result['orders'][0]['recorded_state'], 'queued')
        self.assertEqual(len(result['slots']), 24)
        self.assertTrue(all(x['state'] == 'unknown' for x in result['slots']))
        self.assertTrue(result['slots'][0]['claim_recorded'])
        encoded = json.dumps(result)
        self.assertNotIn('TOP_SECRET_REQUEST_KEY', encoded)
        self.assertNotIn('a' * 32, encoded)

    def test_active_or_crash_journal_is_not_read_or_recovered(self):
        for suffix in ('-wal', '-shm', '-journal'):
            with self.subTest(suffix=suffix):
                journal = self.folder / ('orders.sqlite3' + suffix)
                journal.write_bytes(b'do not interpret')
                before = self.contents()
                with self.assertRaises(InspectionUnavailable):
                    inspect_state(self.folder)
                self.assertEqual(before, self.contents())
                journal.unlink()

    def test_corrupt_database_and_mismatched_slot_identity_do_not_yield_trusted_inventory(self):
        with closing(sqlite3.connect(self.orders.path)) as db:
            db.execute('UPDATE metadata SET pickup_id=? WHERE singleton=1', ('b' * 64,))
            db.commit()
        result = inspect_state(self.folder)
        self.assertEqual(result['binding'], 'mismatch')
        self.assertTrue(all(x['state'] == 'unknown' for x in result['slots']))
        (self.folder / 'orders.sqlite3').write_bytes(b'not sqlite')
        with self.assertRaises(InspectionUnavailable):
            inspect_state(self.folder)

    def test_cli_never_prints_request_key_path_or_raw_database_errors(self):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(main(['--state-dir', str(self.folder)]), 0)
        self.assertNotIn('TOP_SECRET_REQUEST_KEY', output.getvalue())
        self.assertNotIn(str(self.folder), output.getvalue())
        (self.folder / 'orders.sqlite3').write_bytes(b'invalid SECRET_DB_CONTENT')
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(main(['--state-dir', str(self.folder)]), 1)
        self.assertNotIn(str(self.folder), error.getvalue())
        self.assertNotIn('SECRET_DB_CONTENT', error.getvalue())


if __name__ == '__main__':
    unittest.main()
