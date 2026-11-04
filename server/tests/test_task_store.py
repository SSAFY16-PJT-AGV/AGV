import importlib
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest

from server.tasks import (
    DuplicateTaskError, InvalidTransitionError, StateConflictError, UnknownTaskError,
)


class SqliteTaskStoreTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("server.task_store"), "task store not implemented")
        self.module = importlib.import_module("server.task_store")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "tasks.sqlite"
        self.store = self.module.SqliteTaskStore(self.path)
        self.store.initialize()

    def test_data_survives_new_store_and_schema_init_is_idempotent(self):
        self.store.create("task-1")
        self.store.transition("task-1", "PENDING", "ASSIGNED")
        after_restart = self.module.SqliteTaskStore(self.path)
        after_restart.initialize()
        self.assertEqual(after_restart.get("task-1").status, "ASSIGNED")

    def test_duplicate_unknown_invalid_and_stale_updates_fail(self):
        self.store.create("task-1")
        with self.assertRaises(DuplicateTaskError):
            self.store.create("task-1")
        with self.assertRaises(UnknownTaskError):
            self.store.get("missing")
        with self.assertRaises(UnknownTaskError):
            self.store.transition("missing", "PENDING", "ASSIGNED")
        with self.assertRaises(InvalidTransitionError):
            self.store.transition("task-1", "PENDING", "MOVING")
        with self.assertRaises(StateConflictError):
            self.store.transition("task-1", "ASSIGNED", "PICKING")
        self.assertEqual(self.store.get("task-1").status, "PENDING")

    def test_full_state_sequence_and_exception_are_persistent(self):
        self.store.create("normal")
        sequence = ["PENDING", "ASSIGNED", "PICKING", "MOVING", "DELIVERING", "VERIFIED", "DONE"]
        for current, target in zip(sequence, sequence[1:]):
            self.assertEqual(self.store.transition("normal", current, target).status, target)
        with self.assertRaises(InvalidTransitionError):
            self.store.transition("normal", "DONE", "FAILED")
        self.store.create("failed")
        self.store.transition("failed", "PENDING", "FAILED")
        self.assertEqual(self.module.SqliteTaskStore(self.path).get("failed").status, "FAILED")

    def test_task_id_is_treated_as_data_not_sql(self):
        odd_id = "x'); DROP TABLE tasks;--"
        self.store.create(odd_id)
        self.store.transition(odd_id, "PENDING", "ASSIGNED")
        self.assertEqual(self.store.get(odd_id).status, "ASSIGNED")
        self.assertEqual(self.store.create("another").status, "PENDING")

    def test_two_separate_instances_cannot_both_transition_same_version(self):
        self.store.create("task-1")
        barrier = threading.Barrier(2)
        outcomes = []

        def change(target):
            store = self.module.SqliteTaskStore(self.path)
            barrier.wait(timeout=5)
            try:
                store.transition("task-1", "PENDING", target)
                outcomes.append("success")
            except StateConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=change, args=(target,)) for target in ("ASSIGNED", "CANCELLED")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["success", "conflict"])
        self.assertIn(self.store.get("task-1").status, ("ASSIGNED", "CANCELLED"))


if __name__ == "__main__":
    unittest.main()
