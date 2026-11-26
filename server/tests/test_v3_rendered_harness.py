"""Evidence checks for the visible v3 Unreal integration runner."""
import copy
import unittest

from server.tools.v3_rendered_test import verify_outcome


def example():
    products = {'red': {'pickup_node': 'N01', 'shelf_id': 'SHELF_RED'},
                'blue': {'pickup_node': 'N02', 'shelf_id': 'SHELF_BLUE'}}
    before = {'fresh': True, 'pickup': {'fresh': True, 'sources': {
        'N01': {'available': 3}, 'N02': {'available': 3}}},
        'shelves': {'SHELF_RED': {'slots': [False, False]}, 'SHELF_BLUE': {'slots': [False, False]}}}
    after = copy.deepcopy(before)
    for source in ('N01', 'N02'):
        after['pickup']['sources'][source]['available'] -= 1
    for shelf in ('SHELF_RED', 'SHELF_BLUE'):
        after['shelves'][shelf]['slots'][0] = True
    orders = [{'product_id': product, 'assigned_robot': robot, 'status': 'completed',
               'shelf_id': shelf, 'slot_index': 0} for product, robot, shelf in (
        ('red', 'sim-r1', 'SHELF_RED'), ('blue', 'sim-r2', 'SHELF_BLUE'))]
    messages = []
    for robot in ('AGV_01', 'AGV_02'):
        messages.extend({'type': 'robot.state', 'data': {'agv_id': robot, 'x': x, 'y': 0.0}}
                        for x in (0.0, .2))
        messages.extend({'type': 'robot.action', 'data': {'agv_id': robot, 'action': action,
                        'action_id': robot + action, 'result': 'success'}} for action in ('grab', 'place'))
    return before, after, products, orders, messages


class RenderedEvidenceTest(unittest.TestCase):
    def test_success_requires_two_moving_robots_and_inventory_effects(self):
        result = verify_outcome(*example())
        self.assertEqual(set(result), {'AGV_01', 'AGV_02'})

    def test_unchanged_robot_positions_do_not_count_as_movement(self):
        data = list(example())
        for message in data[-1]:
            if message['type'] == 'robot.state':
                message['data']['x'] = 0.0
        with self.assertRaisesRegex(AssertionError, 'change of position'):
            verify_outcome(*data)

    def test_missing_action_and_inconsistent_inventory_are_rejected(self):
        data = list(example())
        data[-1] = [item for item in data[-1] if not (item['type'] == 'robot.action'
                    and item['data']['action'] == 'grab' and item['data']['agv_id'] == 'AGV_01')]
        with self.assertRaisesRegex(AssertionError, 'successful grab'):
            verify_outcome(*data)
        data = list(example())
        data[1]['pickup']['sources']['N01']['available'] = 3
        with self.assertRaisesRegex(AssertionError, 'pickup inventory'):
            verify_outcome(*data)


if __name__ == '__main__':
    unittest.main()
