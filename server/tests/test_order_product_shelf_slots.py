import copy
import unittest
from unittest.mock import patch

from server.order_product_control import ProductControlEngine
from server.tests.test_order_product_control import PRODUCTS, Robot, Runtime, Workflow, map_data


class ProductShelfSlotsTests(unittest.TestCase):
    def test_configured_shelf_accepts_multiple_bounded_slots(self):
        products = copy.deepcopy(PRODUCTS)
        products['Red']['slot_count'] = 6
        with patch('server.order_product_control.PickupWorkflow', Workflow):
            engine = ProductControlEngine(Runtime(), Robot(), map_data(), products)
            state = engine.create_product_order('Red', 'P', 'D', 'SHELF_RED', 4)
        self.assertEqual(state['status'], 'moving')
        self.assertEqual(state['slot_index'], 4)

    def test_out_of_range_slot_is_rejected(self):
        products = copy.deepcopy(PRODUCTS)
        products['Red']['slot_count'] = 6
        engine = ProductControlEngine(Runtime(), Robot(), map_data(), products)
        with self.assertRaises(ValueError):
            engine.create_product_order('Red', 'P', 'D', 'SHELF_RED', 6)


if __name__ == '__main__':
    unittest.main()
