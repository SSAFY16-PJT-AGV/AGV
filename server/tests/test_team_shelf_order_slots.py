"""Six-shelf simulation uses distinct, durable pick/place slot reservations."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from server.team_shelf_order_slots import TeamShelfOrderSlots, SlotUnavailable


def snapshot():
    result = [{'slot_id': i, 'state': 'empty', 'product_type': None} for i in range(1, 25)]
    result[0].update(state='occupied', product_type='milk')
    result[1].update(state='occupied', product_type='milk')
    result[2].update(state='occupied', product_type='egg')
    result[3].update(state='unknown')
    return result


class TeamShelfOrdersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='team-shelf-slots-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'slots.sqlite3'
        self.store = TeamShelfOrderSlots.create_new(self.path)
        self.session = self.store.begin_session('a' * 32)
        self.store.apply_snapshot(self.session, snapshot(), fence=self.store.begin_snapshot(self.session))

    def test_concurrent_orders_claim_distinct_source_and_destination_slots(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda req: self.store.reserve(self.session, req, 'milk'), ('r1', 'r2')))
        self.assertEqual({r['source_slot_id'] for r in values}, {1, 2})
        self.assertEqual({r['destination_slot_id'] for r in values}, {5, 6})
        self.assertTrue(all(r['source_node'] == 'D01' and r['destination_node'] == 'D02' for r in values))
        with self.assertRaises(SlotUnavailable):
            self.store.reserve(self.session, 'r3', 'milk')

    def test_confirmed_pick_and_place_update_the_named_slots_once(self):
        row = self.store.reserve(self.session, 'r1', 'milk')
        self.assertEqual((row['source_slot_id'], row['destination_slot_id']), (1, 5))
        self.store.mark_dispatched(self.session, 'r1', 'pick', 'pick-1')
        self.store.complete_action(self.session, 'r1', 'pick-1', True)
        self.assertEqual(self.store.get('r1')['state'], 'carried')
        self.store.mark_dispatched(self.session, 'r1', 'place', 'place-1')
        self.store.complete_action(self.session, 'r1', 'place-1', True)
        state = self.store.inspect()
        self.assertEqual(state['orders'][0]['state'], 'completed')
        self.assertEqual(state['claims'], [])
        self.assertEqual(state['slots'][0]['state'], 'empty')
        self.assertEqual(state['slots'][4]['product_type'], 'milk')
        with self.assertRaises(ValueError):
            self.store.complete_action(self.session, 'r1', 'place-1', True)

    def test_unknown_result_survives_restart_without_reusing_either_slot(self):
        self.store.reserve(self.session, 'r1', 'milk')
        self.store.mark_dispatched(self.session, 'r1', 'pick', 'pick-1')
        reopened = TeamShelfOrderSlots.open_existing(self.path)
        token = reopened.begin_session('a' * 32)
        self.assertEqual(reopened.get('r1')['state'], 'unknown')
        self.assertEqual({x['slot_id'] for x in reopened.inspect()['claims']}, {1, 5})
        reopened.apply_snapshot(token, snapshot(), fence=reopened.begin_snapshot(token))
        next_order = reopened.reserve(token, 'r2', 'milk')
        self.assertEqual((next_order['source_slot_id'], next_order['destination_slot_id']), (2, 6))
        with self.assertRaises(ValueError):
            reopened.complete_action(token, 'r1', 'pick-1', True)

    def test_stale_snapshot_and_duplicate_order_identity_do_not_change_state(self):
        fence = self.store.begin_snapshot(self.session)
        first = self.store.reserve(self.session, 'r1', 'milk')
        with self.assertRaises(ValueError):
            self.store.apply_snapshot(self.session, snapshot(), fence=fence)
        self.assertEqual(self.store.reserve(self.session, 'r1', 'milk'), first)
        with self.assertRaises(ValueError):
            self.store.reserve(self.session, 'r1', 'egg')

    def test_failed_pick_releases_only_its_pair_but_failed_place_stays_blocked(self):
        self.store.reserve(self.session, 'r1', 'milk')
        self.store.mark_dispatched(self.session, 'r1', 'pick', 'pick-1')
        self.store.complete_action(self.session, 'r1', 'pick-1', False)
        self.assertEqual(self.store.get('r1')['state'], 'failed')
        self.assertEqual(self.store.inspect()['claims'], [])
        self.store.reserve(self.session, 'r2', 'egg')
        self.store.mark_dispatched(self.session, 'r2', 'pick', 'pick-2')
        self.store.complete_action(self.session, 'r2', 'pick-2', True)
        self.store.mark_dispatched(self.session, 'r2', 'place', 'place-2')
        self.store.complete_action(self.session, 'r2', 'place-2', False)
        self.assertEqual(self.store.get('r2')['state'], 'unknown')
        self.assertEqual({x['slot_id'] for x in self.store.inspect()['claims']}, {3, 13})

    def test_missing_claim_is_detected_before_reopening_corrupt_journal(self):
        self.store.reserve(self.session, 'r1', 'milk')
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute('DELETE FROM claims WHERE slot_id=1')
        with self.assertRaisesRegex(ValueError, 'claims'):
            TeamShelfOrderSlots.open_existing(self.path)

    def test_slot_observation_is_all_or_nothing(self):
        changed = snapshot()
        changed[0]['state'] = 'unknown'
        changed[-1]['slot_id'] = 1
        fence = self.store.begin_snapshot(self.session)
        with self.assertRaises(ValueError):
            self.store.apply_snapshot(self.session, changed, fence=fence)
        self.assertEqual(self.store.inspect()['slots'][0]['product_type'], 'milk')

    def test_navigation_publish_attempt_is_one_shot_and_unknown_keeps_claims(self):
        self.store.reserve(self.session, 'r1', 'milk')
        self.store.begin_navigation(self.session, 'r1', 'T1')
        with self.assertRaises(ValueError):
            self.store.begin_navigation(self.session, 'r1', 'T1')
        self.store.finish_navigation(self.session, 'r1', 'T1', published=False)
        self.assertEqual(self.store.get('r1')['state'], 'unknown')
        self.assertEqual({x['slot_id'] for x in self.store.inspect()['claims']}, {1, 5})
        with self.assertRaises(ValueError):
            self.store.begin_navigation(self.session, 'r1', 'T1')

    def test_observation_invalidation_blocks_new_work_but_accepts_inflight_ack(self):
        self.store.reserve(self.session, 'r1', 'milk')
        self.store.mark_dispatched(self.session, 'r1', 'pick', 'pick-1')
        fence = self.store.begin_snapshot(self.session, invalidate=True)
        with self.assertRaises(SlotUnavailable):
            self.store.reserve(self.session, 'r2', 'milk')
        self.store.complete_action(self.session, 'r1', 'pick-1', True)
        self.assertEqual(self.store.get('r1')['state'], 'carried')
        with self.assertRaises(ValueError):
            self.store.apply_snapshot(self.session, snapshot(), fence=fence)
        with self.assertRaises(SlotUnavailable):
            self.store.reserve(self.session, 'r2', 'milk')
        fence = self.store.begin_snapshot(self.session, invalidate=True)
        self.store.apply_snapshot(self.session, snapshot(), fence=fence)
        self.assertEqual(self.store.reserve(self.session, 'r2', 'milk')['source_slot_id'], 2)


if __name__ == '__main__':
    unittest.main()
