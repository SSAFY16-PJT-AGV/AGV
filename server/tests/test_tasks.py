import importlib
import importlib.util
import threading
import unittest


class TaskRegistryTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("server.tasks"), "task registry not implemented")
        self.tasks = importlib.import_module("server.tasks")
        self.registry = self.tasks.TaskRegistry()

    def test_full_happy_path(self):
        self.assertEqual(self.registry.create("task1").status, "PENDING")
        states = ["PENDING", "ASSIGNED", "PICKING", "MOVING", "DELIVERING", "VERIFIED", "DONE"]
        for previous, target in zip(states, states[1:]):
            result = self.registry.transition("task1", previous, target)
            self.assertEqual((result.task_id, result.status), ("task1", target))
        self.assertEqual(self.registry.get("task1").status, "DONE")
        with self.assertRaises(self.tasks.InvalidTransitionError):
            self.registry.transition("task1", "DONE", "FAILED")
        self.assertEqual(self.registry.get("task1").status, "DONE")

    def test_skips_or_stale_expected_states_leave_state_unchanged(self):
        self.registry.create("task1")
        with self.assertRaises(self.tasks.InvalidTransitionError):
            self.registry.transition("task1", "PENDING", "MOVING")
        with self.assertRaises(self.tasks.StateConflictError):
            self.registry.transition("task1", "ASSIGNED", "PICKING")
        self.assertEqual(self.registry.get("task1").status, "PENDING")

    def test_failure_and_cancel_record_terminal_state_without_normal_transition(self):
        for terminal in ("FAILED", "CANCELLED"):
            task_id = "task-" + terminal
            self.registry.create(task_id)
            self.registry.transition(task_id, "PENDING", "ASSIGNED")
            self.registry.transition(task_id, "ASSIGNED", terminal)
            with self.assertRaises(self.tasks.InvalidTransitionError):
                self.registry.transition(task_id, terminal, "DONE")
            self.assertEqual(self.registry.get(task_id).status, terminal)

    def test_create_validates_unique_id_and_unknown_task(self):
        for value in (None, "", " ", 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.registry.create(value)
        self.registry.create("task1")
        with self.assertRaises(self.tasks.DuplicateTaskError):
            self.registry.create("task1")
        with self.assertRaises(self.tasks.UnknownTaskError):
            self.registry.get("missing")
        with self.assertRaises(self.tasks.UnknownTaskError):
            self.registry.transition("missing", "PENDING", "ASSIGNED")

    def test_concurrent_transitions_only_one_matches_previous_state(self):
        self.registry.create("task1")
        barrier = threading.Barrier(2)
        outcomes = []

        def run(target):
            barrier.wait()
            try:
                self.registry.transition("task1", "PENDING", target)
                outcomes.append("success")
            except self.tasks.StateConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=run, args=(target,)) for target in ("ASSIGNED", "CANCELLED")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["success", "conflict"])
        self.assertIn(self.registry.get("task1").status, ("ASSIGNED", "CANCELLED"))


if __name__ == "__main__":
    unittest.main()
