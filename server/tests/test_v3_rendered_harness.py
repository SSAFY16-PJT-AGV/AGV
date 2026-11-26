"""Evidence checks for the visible v3 Unreal integration runner."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from server.tools.v3_rendered_test import verify_outcome, server_command, parse_speed


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


class RenderedSpeedTest(unittest.TestCase):
    def test_speed_requires_a_finite_value_in_the_simulation_range(self):
        self.assertEqual(parse_speed('100'), 100.0)
        for value in ('0', '-1', 'inf', 'nan', '1001', 'not-a-number'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_speed(value)

    def test_default_uses_the_original_uvicorn_entrypoint(self):
        self.assertEqual(server_command('python', None), ['python', '-m', 'uvicorn',
            'server.order_demo_app:app', '--host', '127.0.0.1', '--port', '8000',
            '--workers', '1', '--no-proxy-headers'])

    def test_optional_speed_is_applied_only_inside_the_child(self):
        from sim import order_executor
        old = order_executor.SPEED_MM_PER_SECOND
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix='v3-speed-') as folder:
            Path(folder, 'uvicorn.py').write_text(
                'def run(*args, **kwargs):\n'
                '    from sim.order_executor import SPEED_MM_PER_SECOND\n'
                '    print(SPEED_MM_PER_SECOND, kwargs["port"], kwargs["proxy_headers"])\n', encoding='utf-8')
            env = os.environ.copy()
            env['PYTHONPATH'] = str(root)
            completed = subprocess.run(server_command(sys.executable, 50.0), cwd=folder, env=env,
                                       text=True, encoding='utf-8', capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), '50.0 8000 False')
        self.assertEqual(order_executor.SPEED_MM_PER_SECOND, old)

    def test_speed_is_passed_as_an_argument_not_code(self):
        with self.assertRaises((TypeError, ValueError)):
            server_command('python', '50; print("injected")')


if __name__ == '__main__':
    unittest.main()
