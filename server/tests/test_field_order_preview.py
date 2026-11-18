"""Pure field order inspection must never provide a movement permission."""
import copy
import unittest


def order():
    return {'order_id': 'task-1', 'seq': 1, 'map_id': 'mat@v1',
            'nodes': ['A', 'B'], 'released_until': 'B',
            'action_at_end': {'type': 'load'}}


class FieldOrderPreviewTests(unittest.TestCase):
    def test_valid_order_is_inspected_without_modifying_it(self):
        from server.field_order_preview import inspect_field_order
        candidate = order()
        original = copy.deepcopy(candidate)
        preview = inspect_field_order('robot-1', candidate)
        self.assertEqual((preview.robot_id, preview.order_id, preview.sequence, preview.map_id),
                         ('robot-1', 'task-1', 1, 'mat@v1'))
        self.assertEqual(preview.payload_sha256,
                         'ad75761ddf1836d4127fd793ed213f637674726017556e8d20defaff316bd08c')
        self.assertEqual(candidate, original)

    def test_field_robot_contract_is_applied(self):
        from server.field_order_preview import inspect_field_order
        for candidate in (dict(order(), seq=0), dict(order(), nodes=['A'], released_until='A'),
                          dict(order(), extra='not in protocol')):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                inspect_field_order('robot-1', candidate)

    def test_unsafe_robot_ids_are_rejected(self):
        from server.field_order_preview import inspect_field_order
        for robot in ('', ' robot', 'robot ', 'robot/1', 'robot+#', 'bad\x00id', 'x' * 129, '\ud800'):
            with self.subTest(robot=repr(robot)), self.assertRaises(ValueError):
                inspect_field_order(robot, order())

    def test_preview_is_immutable(self):
        from dataclasses import FrozenInstanceError
        from server.field_order_preview import inspect_field_order
        preview = inspect_field_order('robot-1', order())
        with self.assertRaises(FrozenInstanceError):
            preview.robot_id = 'robot-2'

    def test_wire_digest_is_distinct_from_recovery_canonical_digest(self):
        from server.field_order_preview import inspect_field_order
        from server.recovery import _digest_json
        original = order()
        reordered = dict(reversed(list(original.items())))
        wire = inspect_field_order('robot-1', original).payload_sha256
        self.assertEqual(wire, 'ad75761ddf1836d4127fd793ed213f637674726017556e8d20defaff316bd08c')
        self.assertEqual(_digest_json(original),
                         'eab449bc18f401c310cc6a4b05bd967d3aa07e1726b837083405c8d2d7dbf570')
        self.assertEqual(_digest_json(reordered), _digest_json(original))
        self.assertNotEqual(wire, _digest_json(original))
        self.assertNotEqual(inspect_field_order('robot-1', reordered).payload_sha256, wire)

    def test_later_order_preview_names_first_node_as_route_start(self):
        from server.field_order_preview import inspect_field_order
        later = dict(order(), seq=2, nodes=['A', 'B', 'C'], released_until='C')
        preview = inspect_field_order('robot-1', later)
        self.assertEqual(preview.route_start_node, 'A')
        self.assertEqual(preview.sequence, 2)


def observation():
    return {'robot_id': 'robot-1', 'robot_session': 'boot-1', 'map_id': 'mat@v1',
            'node': 'A', 'incoming_edge': 'E1', 'status': 'IDLE',
            'online': True, 'retained': False, 'received_at': 100.0}


class ProposedObservationTests(unittest.TestCase):
    def review(self, item, now=102.0, maximum_age=3.0):
        from server.field_order_preview import inspect_field_order, review_proposed_observation
        preview = inspect_field_order('robot-1', order())
        return review_proposed_observation(preview, item, expected_session='boot-1',
                                           expected_incoming_edge='E1', now=now,
                                           maximum_age=maximum_age)

    def test_matching_fresh_claims_have_no_detected_inconsistencies(self):
        self.assertEqual(self.review(observation()).issues, ())

    def test_missing_claims_are_reported(self):
        candidate = observation()
        del candidate['robot_session']
        self.assertIn('missing:robot_session', self.review(candidate).issues)

    def test_identity_map_session_and_position_mismatch_are_reported(self):
        fields = {'robot_id': 'robot-2', 'map_id': 'other@v1', 'robot_session': 'boot-2',
                  'node': 'B', 'incoming_edge': 'E2'}
        for key, value in fields.items():
            with self.subTest(key=key):
                self.assertIn('mismatch:' + key, self.review(dict(observation(), **{key: value})).issues)

    def test_old_future_retained_offline_and_moving_claims_are_reported(self):
        cases = [('received_at', 98.0, 'stale'), ('received_at', 103.0, 'future'),
                 ('retained', True, 'retained'), ('online', False, 'offline'),
                 ('status', 'MOVING', 'not_idle')]
        for field, value, issue in cases:
            with self.subTest(field=field, value=value):
                self.assertIn(issue, self.review(dict(observation(), **{field: value})).issues)

    def test_invalid_or_nonfinite_time_never_appears_fresh(self):
        for value in (True, '100', None, float('nan'), float('inf'), -1, 10 ** 1000):
            with self.subTest(value=value):
                self.assertIn('invalid:received_at', self.review(dict(observation(), received_at=value)).issues)

    def test_invalid_clock_limits_are_rejected(self):
        for now, age in ((True, 3), (float('nan'), 3), (102, 0), (102, float('inf'))):
            with self.subTest(now=now, age=age), self.assertRaises(ValueError):
                self.review(observation(), now, age)

    def test_repeated_first_order_uses_first_node_as_start(self):
        from server.field_order_preview import inspect_field_order, review_proposed_observation
        repeated = dict(order(), nodes=['A', 'B', 'A'], released_until='A')
        preview = inspect_field_order('robot-1', repeated)
        report = review_proposed_observation(preview, observation(), expected_session='boot-1',
                                              expected_incoming_edge='E1', now=102,
                                              maximum_age=3)
        self.assertEqual(report.issues, ())

    def test_extension_order_is_not_assessed_as_a_new_start(self):
        from server.field_order_preview import inspect_field_order, review_proposed_observation
        preview = inspect_field_order('robot-1', dict(order(), seq=2))
        with self.assertRaises(ValueError):
            review_proposed_observation(preview, observation(), expected_session='boot-1',
                                        expected_incoming_edge='E1', now=102, maximum_age=3)


if __name__ == '__main__':
    unittest.main()
