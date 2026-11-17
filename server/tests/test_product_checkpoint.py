import copy
import unittest
from unittest.mock import Mock

from server.order_product_control import ProductControlEngine
from server.tests import test_order_product_control as fixture


class ProductCheckpointTests(fixture.ProductControlTests):
    def completed(self):
        self.product_order()
        self.ack(self.finish_pickup()['pending_action'])
        self.ack(self.finish_delivery()['pending_action'])

    def test_checkpoint_requires_completed_and_restores_validated_executor(self):
        with self.assertRaises(ValueError):
            self.engine.export_completed_checkpoint()
        self.completed()
        checkpoint = self.engine.export_completed_checkpoint()
        state = self.robot.executor.snapshot()
        new_robot, new_runtime = fixture.Robot(), fixture.Runtime()
        def restore(executor, **kwargs):
            self.assertEqual(kwargs['task_id'], 'web-delivery')
            self.assertEqual(kwargs['action_ack'], checkpoint['action_ack'])
            executor.state = copy.deepcopy(state)
            executor.state['receipt']['new'] = False
            executor.state['recovered'] = True
            executor.matches_recovered = lambda evidence: evidence == checkpoint['evidence']
            return copy.deepcopy(checkpoint['evidence'])
        new_runtime.restore_completed_executor = Mock(side_effect=restore)
        restarted = ProductControlEngine(new_runtime, new_robot, fixture.map_data(), fixture.PRODUCTS)
        restarted.restore_completed_checkpoint(checkpoint)
        self.assertEqual(restarted.snapshot()['status'], 'completed')
        self.assertEqual(restarted.snapshot()['orders_remaining'], 15)
        self.assertFalse(new_robot.executor.snapshot()['receipt']['new'])
        self.assertEqual(restarted.export_completed_checkpoint(), checkpoint)

    def test_bad_checkpoint_never_attempts_executor_recovery(self):
        self.completed()
        checkpoint = self.engine.export_completed_checkpoint()
        for field, value in [('cycle', 0), ('robot_id', 'sim-r2'), ('destination', 'P')]:
            with self.subTest(field=field):
                bad = dict(checkpoint, **{field: value})
                robot, runtime = fixture.Robot(), fixture.Runtime()
                runtime.restore_completed_executor = Mock()
                restarted = ProductControlEngine(runtime, robot, fixture.map_data(), fixture.PRODUCTS)
                with self.assertRaises(ValueError):
                    restarted.restore_completed_checkpoint(bad)
                runtime.restore_completed_executor.assert_not_called()


if __name__ == '__main__':
    unittest.main()
