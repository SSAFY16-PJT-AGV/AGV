"""Synthetic shelf frames are never treated as physical inventory."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest
from unittest.mock import patch

from server.sim_runtime import SimRuntime
from server.team_shelf_sim_dispatch import TeamShelfSimDispatch
from server.team_shelf_order_slots import TeamShelfOrderSlots, SlotUnavailable
from server.simulated_shelf_perception import SimulatedShelfPerception


def frame(shelf, rows=(), *, ok=True):
    n = int(shelf[1:])
    return {'frame': 'robot_base', 'shelf': {'ok': ok, 'marker_id': 4 * (n - 1) + 1},
            'slots': list(rows)}


def rows(*states):
    ids = {'milk': 30, 'tissue': 31, 'egg': 32, 'juice': 33}
    return [dict({'slot': i, 'state': state}, **({'marker_id': ids[state]} if state in ids else {}))
            for i, state in enumerate(states, 1)]


class SimulatedShelfPerceptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='team-perception-')
        self.addCleanup(self.tmp.cleanup)
        self.store = TeamShelfOrderSlots.create_new(Path(self.tmp.name) / 'slots.sqlite3')
        self.session = self.store.begin_session('simulated-world')
        self.now = 0.0
        self.bridge = SimulatedShelfPerception(self.store, self.session, clock=lambda: self.now,
                                               max_age_seconds=5.0)

    def capture(self, override=None):
        batch = self.bridge.begin_capture()
        frames = {f'N{i:02d}': frame(f'N{i:02d}', rows('empty', 'empty', 'empty', 'empty'))
                  for i in range(1, 7)}
        frames['N01'] = frame('N01', rows('milk', 'egg', 'empty', 'unknown'))
        frames.update(override or {})
        for shelf, value in frames.items():
            self.bridge.accept_frame(batch, shelf, value)
        return batch

    def test_missing_slots_remain_unknown_and_cannot_trigger_false_shortage(self):
        self.capture({'N02': frame('N02', rows('empty', 'empty', 'empty'))})
        slots = self.store.inspect()['slots']
        self.assertEqual(slots[7]['state'], 'unknown')
        report = self.bridge.shortages(threshold=2)
        self.assertEqual(report['milk']['status'], 'unknown')
        self.assertEqual(report['egg']['status'], 'shortage')

    def test_bad_marker_or_duplicate_conflict_invalidates_capture(self):
        self.capture()
        batch = self.bridge.begin_capture()
        with self.assertRaises(SlotUnavailable):
            self.store.reserve(self.session, 'old-observation', 'milk')
        self.bridge.accept_frame(batch, 'N01', frame('N01', rows('milk')))
        with self.assertRaises(ValueError):
            self.bridge.accept_frame(batch, 'N01', frame('N01', rows('egg')))
        with self.assertRaises(ValueError):
            self.bridge.accept_frame(batch, 'N02', frame('N02', rows('empty')))
        batch = self.bridge.begin_capture()
        bad = frame('N02', rows('milk'))
        bad['shelf']['marker_id'] = 1
        with self.assertRaises(ValueError):
            self.bridge.accept_frame(batch, 'N02', bad)
        with self.assertRaises(SlotUnavailable):
            self.store.reserve(self.session, 'still-stale', 'milk')

    def test_expired_or_replayed_capture_does_not_restore_freshness(self):
        old = self.capture()
        with self.assertRaises(ValueError):
            self.bridge.accept_frame(old, 'N01', frame('N01', rows('milk')))
        batch = self.bridge.begin_capture()
        self.now = 6.0
        with self.assertRaises(ValueError):
            self.bridge.accept_frame(batch, 'N01', frame('N01', rows('milk')))
        with self.assertRaises(SlotUnavailable):
            self.store.reserve(self.session, 'expired', 'milk')

    def test_shelf_mismatch_and_unobserved_shelf_never_become_empty(self):
        self.capture({'N02': frame('N02', rows('egg', 'empty', 'empty', 'empty')),
                      'N03': frame('N03', (), ok=False)})
        report = self.bridge.shortages(threshold=2)
        self.assertEqual(report['milk']['status'], 'mismatch')
        self.assertEqual(report['tissue']['status'], 'unknown')
        self.assertTrue(all(row['state'] == 'unknown' for row in self.store.inspect()['slots'][8:12]))

    def test_claimed_slots_are_never_inferred_from_later_frame(self):
        self.capture()
        self.store.reserve(self.session, 'r1', 'milk')
        self.capture()
        state = self.store.inspect()
        self.assertEqual({claim['slot_id'] for claim in state['claims']}, {1, 5})
        self.assertEqual(state['slots'][0]['state'], 'unknown')
        self.assertEqual(state['slots'][4]['state'], 'unknown')
        self.assertEqual(self.bridge.shortages(threshold=2)['milk']['status'], 'unknown')

    def test_approval_is_explicit_and_replenishment_uses_existing_simulated_dispatch(self):
        self.capture()
        data = json.loads((Path(__file__).resolve().parents[1] / 'examples/mat_ssafy_v3.json')
                          .read_text(encoding='utf-8'))
        runtime = SimRuntime(Path(self.tmp.name) / 'runtime.sqlite3', data,
                             {'sim-r1': {'T1': 'boot'}}, port=18883,
                             client_id='synthetic-replenishment-test', field_simulation=True)
        dispatch = TeamShelfSimDispatch(self.store, runtime)
        candidate = {'robot_id': 'sim-r1', 'node': 'D07', 'incoming_edge': 'E_D07', 'available': True}
        with patch.object(runtime, 'create_task'), patch.object(runtime, 'prepare_field',
                return_value={'nodes': ['D07', 'D01']}), patch.object(runtime, 'publish',
                return_value={'task_id': 'T1', 'state': 'acked'}) as publish:
            with self.assertRaises(ValueError):
                self.bridge.approve_and_dispatch(dispatch, 'r1', 'milk', 'T1', candidate,
                                                 threshold=2, approved=False)
            self.assertEqual(self.store.inspect()['claims'], [])
            result = self.bridge.approve_and_dispatch(dispatch, 'r1', 'milk', 'T1', candidate,
                                                       threshold=2, approved=True)
            self.assertEqual((result['slots']['source_slot_id'], result['slots']['destination_slot_id']), (1, 5))
            self.assertEqual(result['broker_ack']['state'], 'acked')
            self.assertEqual(publish.call_count, 1)
            with self.assertRaises(ValueError):
                self.bridge.approve_and_dispatch(dispatch, 'r1', 'milk', 'T1', candidate,
                                                 threshold=2, approved=True)
            self.assertEqual(publish.call_count, 1)
        self.assertEqual(self.store.get('r1')['state'], 'reserved')
        self.assertEqual({row['slot_id'] for row in self.store.inspect()['claims']}, {1, 5})

    def test_expired_observation_cannot_be_approved_or_reserved(self):
        self.capture()
        self.now = 6.0
        with self.assertRaises(ValueError):
            self.bridge.shortages(threshold=2)
        with self.assertRaises(SlotUnavailable):
            self.store.reserve(self.session, 'expired-approval', 'milk')

    def test_external_snapshot_invalidation_also_blocks_old_shortage(self):
        self.capture()
        self.store.begin_snapshot(self.session, invalidate=True)
        with self.assertRaises(SlotUnavailable):
            self.bridge.shortages(threshold=2)

    def test_capture_racing_action_ack_rejects_stale_snapshot_and_keeps_result(self):
        self.capture()
        self.store.reserve(self.session, 'r1', 'milk')
        self.store.mark_dispatched(self.session, 'r1', 'pick', 'pick-1')
        batch = self.bridge.begin_capture()
        self.store.complete_action(self.session, 'r1', 'pick-1', True)
        self.assertEqual(self.store.get('r1')['state'], 'carried')
        for number in range(1, 6):
            shelf = f'N{number:02d}'
            self.bridge.accept_frame(batch, shelf, frame(shelf))
        with self.assertRaises(ValueError):
            self.bridge.accept_frame(batch, 'N06', frame('N06', rows('empty')))
        self.assertEqual(self.store.get('r1')['state'], 'carried')
        self.assertEqual({r['slot_id'] for r in self.store.inspect()['claims']}, {1, 5})
        with self.assertRaises(SlotUnavailable):
            self.store.reserve(self.session, 'r2', 'egg')

    def test_parallel_approvals_claim_distinct_source_and_destination_slots(self):
        self.capture()
        data = json.loads((Path(__file__).resolve().parents[1] / 'examples/mat_ssafy_v3.json')
                          .read_text(encoding='utf-8'))
        runtime = SimRuntime(Path(self.tmp.name) / 'parallel.sqlite3', data,
                             {'sim-r1': {'T1': 'boot-1'}, 'sim-r2': {'T2': 'boot-2'}},
                             port=18883, client_id='synthetic-parallel-test', field_simulation=True)
        dispatch = TeamShelfSimDispatch(self.store, runtime)
        cases = [('r1', 'milk', 'T1', {'robot_id': 'sim-r1', 'node': 'D07',
                  'incoming_edge': 'E_D07', 'available': True}),
                 ('r2', 'egg', 'T2', {'robot_id': 'sim-r2', 'node': 'D08',
                  'incoming_edge': 'E_D08', 'available': True})]
        with patch.object(runtime, 'create_task'), patch.object(runtime, 'prepare_field',
                return_value={'nodes': ['D07', 'D01']}), patch.object(runtime, 'publish',
                side_effect=lambda task: {'task_id': task, 'state': 'acked'}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda item: self.bridge.approve_and_dispatch(
                    dispatch, *item, threshold=2, approved=True), cases))
        self.assertEqual({item['slots']['source_slot_id'] for item in results}, {1, 2})
        self.assertEqual({item['slots']['destination_slot_id'] for item in results}, {5, 13})
        self.assertEqual({item['slot_id'] for item in self.store.inspect()['claims']}, {1, 2, 5, 13})


if __name__ == '__main__':
    unittest.main()
