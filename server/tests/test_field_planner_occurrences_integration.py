"""Experimental team-map planning/resource comparison, not a drive workflow.

The unmodified team map has no waiting zones on these main-line routes. This
test adds synthetic waiting nodes to a private in-memory copy, then manipulates
an in-memory Reservations table with synthetic clearance. It does not test the
actual Coordinator, SQLite, MQTT, robot stopping, or an atomic segment handoff.
"""
import copy
import json
import os
from pathlib import Path
import unittest


@unittest.skipUnless(os.getenv('AGV_FIELD_REPO'), 'set AGV_FIELD_REPO to a team field-run checkout')
class TeamMapPlanningReservationTest(unittest.TestCase):
    def test_repeated_visit_and_shared_crossing_contention(self):
        from server.field_map_planner import plan_field_route
        from server.reservations import Reservations
        from server.route_occurrence import occurrence_clearance, plan_occurrence_segment

        root = Path(os.environ['AGV_FIELD_REPO'])
        data = json.loads((root / 'maps/mat_ssafy_v2.json').read_text(encoding='utf-8'))
        self.assertEqual((data['map_id'], data['version']), ('mat_ssafy_v2', 0))
        # Deliberately artificial main-line wait nodes. Never export this map as
        # a physical map or infer that the real robot can stop at these nodes.
        data = copy.deepcopy(data)
        for node in ('M3', 'P_green', 'P_purple'):
            data['zones'].append({'id': 'test-wait-' + node, 'type': 'waiting', 'node': node})

        route_a = plan_field_route(data, map_ref='mat_ssafy_v2@v0', incoming_edge='E8', goal='P_green')
        route_b = plan_field_route(data, map_ref='mat_ssafy_v2@v0', incoming_edge='E16', goal='P_purple')
        nodes_a, nodes_b = route_a.nodes, route_b.nodes
        self.assertEqual(nodes_a, ('P_yellow', 'X1', 'M3', 'P_red', 'M3', 'X1', 'P_yellow', 'S3', 'P_green'))
        self.assertEqual(nodes_b, ('V_E16', 'P_yellow', 'X2', 'M1', 'P_purple'))

        first = plan_occurrence_segment(data, nodes_a, 0)
        second = plan_occurrence_segment(data, nodes_a, first.end_index)
        third = plan_occurrence_segment(data, nodes_a, second.end_index)
        other = plan_occurrence_segment(data, nodes_b, 0)
        self.assertEqual((first.start_index, first.end_index, first.released_until), (0, 2, 'M3'))
        self.assertEqual((second.start_index, second.end_index, second.released_until), (2, 4, 'M3'))
        self.assertEqual((third.start_index, third.end_index, third.released_until), (4, 8, 'P_green'))
        shared = ('zone', 'Z_X1_X2')
        self.assertIn(shared, first.resources)
        self.assertNotIn(shared, second.resources)
        self.assertIn(shared, third.resources)
        self.assertIn(shared, other.resources)

        table = Reservations()
        first_lease = table.reserve('sim-a', first.resources)
        self.assertTrue(first_lease)
        # X1 and X2 differ, but the mapped shared zone prevents conflict.
        before = table.snapshot()
        self.assertNotIn(('node', 'X2'), before)
        self.assertIsNone(table.reserve('sim-b', [('node', 'X2'), shared]))
        self.assertEqual(before, table.snapshot())
        self.assertIsNone(table.reserve('sim-b', other.resources))
        self.assertEqual(before, table.snapshot())

        # Synthetic proof only. The shared zone is no longer needed by the
        # immediately following segment; keep all intersecting resources.
        clear_first = set(occurrence_clearance(data, nodes_a, 0))
        self.assertIn(shared, clear_first)
        table.release_after_rear_clear('sim-a', clear_first, first_lease)
        self.assertEqual(set(table.snapshot()), set(first.resources) & set(second.resources))
        other_lease = table.reserve('sim-b', other.resources)
        self.assertTrue(other_lease)

        # Reservations is a primitive with independent leases. New resources
        # are acquired separately here; this deliberately does not test or
        # claim an atomic reservation transition in the actual server.
        held_a = {resource for resource, owner in table.snapshot().items() if owner == 'sim-a'}
        added_second = set(second.resources) - held_a
        second_lease = table.reserve('sim-a', added_second)
        self.assertTrue(second_lease)
        clear_second = set(occurrence_clearance(data, nodes_a, first.end_index))
        self.assertTrue(clear_second <= added_second)
        table.release_after_rear_clear('sim-a', clear_second, second_lease)
        held_a = {resource for resource, owner in table.snapshot().items() if owner == 'sim-a'}
        required_third = set(third.resources) - held_a
        self.assertIn(shared, required_third)
        before = table.snapshot()
        self.assertIsNone(table.reserve('sim-a', required_third))
        self.assertEqual(before, table.snapshot())

        # Synthetic second-robot clearance: only then can A acquire the
        # shared zone again on its second visit to the crossing.
        other_clear = occurrence_clearance(data, nodes_b, 0)
        table.release_after_rear_clear('sim-b', other_clear, other_lease)
        self.assertEqual(table.snapshot().get(('node', 'P_purple')), 'sim-b')
        self.assertTrue(table.reserve('sim-a', required_third))
        owners = table.snapshot()
        self.assertTrue(all(owners.get(resource) == 'sim-a' for resource in third.resources))


if __name__ == '__main__':
    unittest.main()
