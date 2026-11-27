"""The simulated slot DB must have a durable, non-interchangeable identity."""
import tempfile
import unittest
from pathlib import Path
import sqlite3
from contextlib import closing

from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.tests.test_team_shelf_order_slots import snapshot


class TeamShelfPersistenceIdentityTest(unittest.TestCase):
    def test_identity_survives_reopen_and_wrong_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            one = Path(folder) / 'one.db'
            two = Path(folder) / 'two.db'
            store = TeamShelfOrderSlots.create_new(one)
            expected = store.creation_id
            self.assertEqual(len(expected), 64)
            other = TeamShelfOrderSlots.create_new(two)
            self.assertNotEqual(expected, other.creation_id)
            reopened = TeamShelfOrderSlots.open_existing(one, expected_creation_id=expected)
            self.assertEqual(expected, reopened.creation_id)
            with self.assertRaises(ValueError):
                TeamShelfOrderSlots.open_existing(two, expected_creation_id=expected)

    def test_identity_mutation_blocks_access(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'slots.db'
            store = TeamShelfOrderSlots.create_new(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute('UPDATE identity SET creation_id=? WHERE singleton=1', ('b' * 64,))
                db.commit()
            with self.assertRaises(ValueError):
                store.inspect()

    def test_legacy_slot_journal_upgrades_without_losing_claims(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.db'
            store = TeamShelfOrderSlots.create_new(path)
            session = store.begin_session('a' * 32)
            store.apply_snapshot(session, snapshot(), fence=store.begin_snapshot(session))
            store.reserve(session, 'order-1', 'milk')
            with closing(sqlite3.connect(path)) as db:
                db.execute('DROP TABLE identity')
                db.execute('DROP TABLE action_bindings')
                db.execute('PRAGMA user_version=1')
                db.commit()
            reopened = TeamShelfOrderSlots.open_existing(path)
            self.assertEqual(len(reopened.creation_id), 64)
            self.assertEqual(reopened.get('order-1')['state'], 'unknown')
            self.assertEqual({row['slot_id'] for row in reopened.inspect()['claims']}, {1, 5})

    def test_restart_keeps_unknown_order_and_claimed_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'slots.db'
            store = TeamShelfOrderSlots.create_new(path)
            session = store.begin_session('a' * 32)
            store.apply_snapshot(session, snapshot(), fence=store.begin_snapshot(session))
            store.reserve(session, 'order-1', 'milk')
            reopened = TeamShelfOrderSlots.open_existing(path, expected_creation_id=store.creation_id)
            self.assertEqual(reopened.get('order-1')['state'], 'unknown')
            self.assertEqual({row['slot_id'] for row in reopened.inspect()['claims']}, {1, 5})

    def test_action_binding_is_durable_and_correlated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'slots.db'
            store = TeamShelfOrderSlots.create_new(path)
            session = store.begin_session('a' * 32)
            store.apply_snapshot(session, snapshot(), fence=store.begin_snapshot(session))
            store.reserve(session, 'order-1', 'milk')
            binding = {'order_id': 'web-pickup', 'action_id': 'engine-1', 'agv_id': 'AGV_01'}
            store.mark_dispatched(session, 'order-1', 'pick', 'slot-1', binding=binding)
            store.complete_action(session, 'order-1', 'slot-1', True)
            reopened = TeamShelfOrderSlots.open_existing(path, expected_creation_id=store.creation_id)
            self.assertEqual(reopened.action_bindings('order-1'), [dict(binding, slot_action_id='slot-1',
                                                                         phase='pick', state='applied')])

    def test_completed_order_retains_proven_source_after_intervening_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TeamShelfOrderSlots.create_new(Path(folder) / 'slots.db')
            session = store.begin_session('a' * 32)
            current = snapshot()
            store.apply_snapshot(session, current, fence=store.begin_snapshot(session))
            store.reserve(session, 'order-1', 'milk')
            store.mark_dispatched(session, 'order-1', 'pick', 'pick')
            store.complete_action(session, 'order-1', 'pick', True)
            current[0] = {'slot_id': 1, 'state': 'empty', 'product_type': None}
            store.apply_snapshot(session, current, fence=store.begin_snapshot(session))
            store.mark_dispatched(session, 'order-1', 'place', 'place')
            store.complete_action(session, 'order-1', 'place', True)
            rows = {row['slot_id']: row for row in store.inspect()['slots']}
            self.assertEqual((rows[1]['state'], rows[1]['product_type']), ('empty', None))
            self.assertEqual((rows[5]['state'], rows[5]['product_type']), ('occupied', 'milk'))


if __name__ == '__main__':
    unittest.main()
