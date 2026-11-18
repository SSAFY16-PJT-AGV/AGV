"""Reconstruct visit indexes only from a complete, consistent order history."""
import copy
import importlib.util
import os
from pathlib import Path
import unittest

from server.route_occurrence import visit_indices_from_orders


def orders():
    base = {'order_id': 'T1', 'seq': 1, 'map_id': 'm@v1',
            'nodes': ['A', 'B', 'A', 'C', 'B', 'D'],
            'released_until': 'B', 'action_at_end': {'type': 'none'}}
    return [base, dict(base, seq=2, released_until='B'),
            dict(base, seq=3, released_until='D')]


class RouteHistoryTests(unittest.TestCase):
    def test_repeated_release_reconstructs_the_correct_occurrence(self):
        data = orders()
        before = copy.deepcopy(data)
        self.assertEqual(visit_indices_from_orders(data), (1, 4, 5))
        self.assertEqual(data, before)

    def test_incomplete_or_changed_history_is_rejected(self):
        for history in ([], orders()[1:], [orders()[0], orders()[2]],
                        [orders()[0], dict(orders()[1], nodes=['A', 'B'])],
                        [orders()[0], dict(orders()[1], map_id='other@v1')],
                        [orders()[0], dict(orders()[1], action_at_end={'type': 'load'})],
                        [orders()[0], dict(orders()[1], order_id='other')],
                        [dict(orders()[0], released_until='A'),
                         dict(orders()[1], released_until='A'),
                         dict(orders()[2], released_until='A')]):
            with self.subTest(history=history), self.assertRaises(ValueError):
                visit_indices_from_orders(history)

    def test_numeric_and_boolean_json_action_values_are_not_interchangeable(self):
        history = orders()
        for order in history:
            order['action_at_end'] = {'type': 'none', 'option': 1}
        history[1]['action_at_end'] = {'type': 'none', 'option': True}
        with self.assertRaises(ValueError):
            visit_indices_from_orders(history)

    @unittest.skipUnless(os.getenv('AGV_FIELD_REPO'), 'set AGV_FIELD_REPO to team checkout')
    def test_matches_team_orderbook_release_indices(self):
        path = Path(os.environ['AGV_FIELD_REPO']) / 'agv/link/orders.py'
        spec = importlib.util.spec_from_file_location('field_robot_order_history', path)
        team = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(team)
        book = team.OrderBook('m@v1')
        indices = []
        for order in orders():
            decision = book.check(order)
            book.commit(decision)
            indices.append(book.release_i)
        self.assertEqual(visit_indices_from_orders(orders()), tuple(indices))


if __name__ == '__main__':
    unittest.main()
