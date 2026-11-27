"""A slot result must be durable before a new simulated leg can be sent."""
import unittest
from server.tests.test_order_product_control import ProductControlTests, ProductControlEngine, Workflow, PRODUCTS, map_data


class DeferredHandoffTest(unittest.TestCase):
    def setUp(self):
        fixture = ProductControlTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.engine = ProductControlEngine(fixture.runtime, fixture.robot, map_data(), PRODUCTS,
                                               defer_delivery_handoff=True)
        self.engine, self.handoff = fixture.engine, fixture.handoff
        self.product_order, self.finish_pickup, self.ack = fixture.product_order, fixture.finish_pickup, fixture.ack

    def test_grab_result_does_not_start_delivery_before_next_guarded_tick(self):
        self.product_order()
        grab = self.finish_pickup()['pending_action']
        self.assertTrue(self.ack(grab))
        self.assertEqual(len(Workflow.created), 1)
        self.handoff.assert_not_called()
        self.assertEqual(self.engine.snapshot()['phase'], 'delivery_pending')
        self.engine.tick(.1)
        self.assertEqual(len(Workflow.created), 2)
        self.assertEqual(self.engine.snapshot()['phase'], 'to_destination')

    def test_unknown_before_tick_never_publishes_delivery(self):
        self.product_order()
        grab = self.finish_pickup()['pending_action']
        self.assertTrue(self.ack(grab))
        self.engine.action_unknown()
        self.engine.tick(.1)
        self.assertEqual(self.engine.snapshot()['status'], 'unknown')
        self.assertEqual(len(Workflow.created), 1)
        self.handoff.assert_not_called()
