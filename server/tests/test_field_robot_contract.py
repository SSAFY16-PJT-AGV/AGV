"""Compatibility checks are pure validation, never a real-robot publisher."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import unittest


def valid_order():
    return {
        'order_id': 'task-1', 'seq': 1, 'map_id': 'mat_ssafy_v2@v1',
        'nodes': ['P_orange', 'S1', 'M1'], 'released_until': 'S1',
        'action_at_end': {'type': 'load'},
    }


class FieldRobotOrderTests(unittest.TestCase):
    def test_validator_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('server.field_robot_contract'),
                             'field robot validator is missing')

    def check(self, order):
        from server.field_robot_contract import validate_field_robot_order
        return validate_field_robot_order(order)

    def test_normal_first_and_later_order(self):
        first = valid_order()
        self.assertIsNone(self.check(first))
        later = dict(first, seq=2, released_until='M1')
        self.assertIsNone(self.check(later))

    def test_rejects_nonpositive_sequence_and_boolean(self):
        for seq in (0, -1, True):
            with self.subTest(seq=seq), self.assertRaises(ValueError):
                self.check(dict(valid_order(), seq=seq))

    def test_requires_two_to_256_nodes_and_no_adjacent_duplicates(self):
        base = valid_order()
        for nodes in (['P_orange'], ['P_orange', 'P_orange'],
                      ['P_orange'] + ['N%d' % n for n in range(256)]):
            candidate = dict(base, nodes=nodes, released_until=nodes[-1])
            with self.subTest(count=len(nodes)), self.assertRaises(ValueError):
                self.check(candidate)
        maximum = ['N%d' % n for n in range(256)]
        self.assertIsNone(self.check(dict(base, nodes=maximum, released_until=maximum[-1])))

    def test_first_release_must_occur_after_start(self):
        with self.assertRaises(ValueError):
            self.check(dict(valid_order(), released_until='P_orange'))
        # A repeated non-adjacent node is allowed by the robot's OrderBook.
        candidate = dict(valid_order(), nodes=['A', 'B', 'A'], released_until='A')
        self.assertIsNone(self.check(candidate))

    def test_ids_count_characters_and_are_nonempty(self):
        base = valid_order()
        self.assertIsNone(self.check(dict(base, order_id='가' * 128)))
        for field in ('order_id', 'map_id'):
            for value in (' ', '가' * 129):
                with self.subTest(field=field, length=len(value)), self.assertRaises(ValueError):
                    self.check(dict(base, **{field: value}))
        with self.assertRaises(ValueError):
            self.check(dict(base, nodes=['A', '나' * 129], released_until='나' * 129))

    def test_wire_payload_utf8_byte_limit(self):
        candidate = valid_order()
        candidate['action_at_end']['note'] = ''
        encoded = json.dumps(candidate, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        candidate['action_at_end']['note'] = 'x' * (65536 - len(encoded))
        self.assertIsNone(self.check(candidate))
        candidate['action_at_end']['note'] += 'x'
        with self.assertRaises(ValueError):
            self.check(candidate)

    def test_does_not_mutate_input(self):
        candidate = valid_order()
        original = copy.deepcopy(candidate)
        self.check(candidate)
        self.assertEqual(candidate, original)


@unittest.skipUnless(os.getenv('AGV_FIELD_ROBOT_ORDER_PATH'),
                     'set AGV_FIELD_ROBOT_ORDER_PATH to the team robot orders.py')
class FieldRobotImplementationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(os.environ['AGV_FIELD_ROBOT_ORDER_PATH'])
        spec = importlib.util.spec_from_file_location('team_robot_orders_for_test', path)
        if spec is None or spec.loader is None:
            raise RuntimeError('team robot orders.py cannot be loaded')
        cls.robot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.robot)

    def test_first_then_next_actual_robot_book(self):
        from server.field_robot_contract import validate_field_robot_order
        from server.order_publisher import _payload
        first = valid_order()
        second = dict(first, seq=2, released_until='M1')
        book = self.robot.OrderBook(first['map_id'])
        for order, expected in ((first, 'new'), (second, 'extend')):
            validate_field_robot_order(order)
            decision = book.check(_payload(order))
            self.assertEqual(decision.kind, expected)
            book.commit(decision)

    def test_known_incompatible_orders_are_rejected_by_both(self):
        from server.field_robot_contract import validate_field_robot_order
        from server.order_publisher import _payload
        first = valid_order()
        cases = [dict(first, seq=0), dict(first, released_until='P_orange'),
                 dict(first, nodes=['A', 'A'], released_until='A'),
                 dict(first, order_id='x' * 129)]
        large = copy.deepcopy(first)
        large['action_at_end']['extra'] = 'x' * 65536
        cases.append(large)
        for order in cases:
            with self.subTest(order_id=order['order_id'], seq=order['seq']):
                with self.assertRaises(ValueError):
                    validate_field_robot_order(order)
                with self.assertRaises(self.robot.OrderRejected):
                    self.robot.OrderBook(first['map_id']).check(_payload(order))

    def test_actual_robot_accepts_exact_wire_byte_limit(self):
        from server.field_robot_contract import validate_field_robot_order
        from server.order_publisher import _payload
        candidate = valid_order()
        candidate['action_at_end']['note'] = ''
        candidate['action_at_end']['note'] = '가' * 100 + 'x' * (65536 - len(_payload(candidate)) - 300)
        self.assertEqual(len(_payload(candidate)), 65536)
        validate_field_robot_order(candidate)
        self.assertEqual(self.robot.OrderBook(candidate['map_id']).check(_payload(candidate)).kind, 'new')


if __name__ == '__main__':
    unittest.main()
