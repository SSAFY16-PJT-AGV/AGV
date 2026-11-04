import threading
import unittest

from server import reservations


class ReservationsTest(unittest.TestCase):
    def test_conflicting_set_is_not_partially_acquired(self):
        table = reservations.Reservations()
        self.assertTrue(table.reserve("robot1", [("zone", "Z1")]))
        self.assertFalse(table.reserve("robot2", [("node", "N1"), ("zone", "Z1")]))
        self.assertEqual(table.snapshot(), {("zone", "Z1"): "robot1"})

    def test_release_requires_owner_and_does_not_partially_release(self):
        table = reservations.Reservations()
        lease = table.reserve("robot1", [("node", "N1"), ("zone", "Z1")])
        with self.assertRaises(ValueError):
            table.release_after_rear_clear("robot2", [("node", "N1")], lease)
        self.assertEqual(len(table.snapshot()), 2)
        table.release_after_rear_clear("robot1", [("node", "N1")], lease)
        self.assertEqual(table.snapshot(), {("zone", "Z1"): "robot1"})

    def test_delayed_clear_event_cannot_release_new_lease(self):
        table = reservations.Reservations()
        resource = [("edge", "E1")]
        old_lease = table.reserve("robot1", resource)
        table.release_after_rear_clear("robot1", resource, old_lease)
        new_lease = table.reserve("robot1", resource)
        self.assertNotEqual(old_lease, new_lease)
        with self.assertRaises(ValueError):
            table.release_after_rear_clear("robot1", resource, old_lease)
        self.assertFalse(table.reserve("robot2", resource))

    def test_retry_keeps_lease_and_overlap_does_not_replace_it(self):
        table = reservations.Reservations()
        original = [("node", "N1"), ("zone", "Z1")]
        lease = table.reserve("robot1", original)
        self.assertEqual(table.reserve("robot1", original), lease)
        self.assertFalse(table.reserve("robot1", [("zone", "Z1"), ("node", "N2")]))
        table.release_after_rear_clear("robot1", original, lease)
        self.assertEqual(table.snapshot(), {})

    def test_invalid_and_empty_resources_are_rejected(self):
        table = reservations.Reservations()
        for resources in ([], [("unknown", "N1")], [("node", "")]):
            with self.subTest(resources=resources):
                with self.assertRaises(ValueError):
                    table.reserve("robot1", resources)
        self.assertEqual(table.snapshot(), {})

    def test_competing_requests_have_only_one_winner(self):
        table = reservations.Reservations()
        gate = threading.Barrier(2)
        results = []

        def attempt(robot):
            gate.wait()
            results.append(bool(table.reserve(robot, [("edge", "E1"), ("zone", "Z1")])))

        threads = [threading.Thread(target=attempt, args=(robot,)) for robot in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(set(table.snapshot().values())), 1)


if __name__ == "__main__":
    unittest.main()
