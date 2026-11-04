import importlib
import importlib.util
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest


def message(seq=1, released="N3", order_id="T1"):
    return {"order_id": order_id, "seq": seq, "map_id": "layout@v1",
            "nodes": ["N1", "N2", "N3", "N4", "N5"],
            "released_until": released, "action_at_end": {"type": "load"}}


def encode(order):
    return json.dumps(order, ensure_ascii=False).encode("utf-8")


class SimOrderInboxTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("sim.order_inbox"), "simulator inbox is not implemented")
        self.module = importlib.import_module("sim.order_inbox")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inbox.sqlite3"
        self.inbox = self.module.SimOrderInbox(self.path, "sim-r1", "layout@v1")
        self.inbox.initialize()

    def test_duplicate_is_idempotent_and_changed_content_is_rejected(self):
        first = self.inbox.accept(encode(message()))
        self.assertTrue(first.new)
        self.assertEqual(first.state, "accepted")
        duplicate = self.inbox.accept(json.dumps(message(), sort_keys=True).encode())
        self.assertFalse(duplicate.new)
        self.assertEqual(first.digest, duplicate.digest)
        with self.assertRaises(self.module.OrderRejected):
            self.inbox.accept(encode(message(released="N5")))

    def test_sequence_needs_completion_and_must_preserve_route(self):
        first = self.inbox.accept(encode(message()))
        with self.assertRaises(self.module.OrderRejected):
            self.inbox.accept(encode(message(seq=2, released="N5")))
        with self.assertRaises(self.module.OrderRejected):
            self.inbox.accept(encode(message(seq=3, released="N5")))
        self.inbox.mark_completed("T1", 1, first.digest, "N3", rear_clear_confirmed=True)
        invalid = message(seq=2, released="N5")
        invalid["nodes"] = ["N1", "N3", "N5"]
        with self.assertRaises(self.module.OrderRejected):
            self.inbox.accept(encode(invalid))
        with self.assertRaises(self.module.OrderRejected):
            self.inbox.accept(encode(message(seq=2, released="N3")))
        self.assertTrue(self.inbox.accept(encode(message(seq=2, released="N5"))).new)

    def test_restart_never_reexecutes_and_requires_explicit_reconciliation(self):
        first = self.inbox.accept(encode(message()))
        reopened = self.module.SimOrderInbox(self.path, "sim-r1", "layout@v1")
        reopened.initialize()
        self.assertFalse(reopened.accept(encode(message())).new)
        with self.assertRaises(self.module.OrderRejected):
            reopened.mark_completed("T1", 1, first.digest, "N3", rear_clear_confirmed=True)
        with self.assertRaises(self.module.OrderRejected):
            reopened.reconcile_completed("T1", 1, first.digest, "N3", rear_clear_confirmed=False)
        with self.assertRaises(self.module.OrderRejected):
            reopened.reconcile_completed("T1", 1, first.digest, "N2", rear_clear_confirmed=True)
        reopened.reconcile_completed("T1", 1, first.digest, "N3", rear_clear_confirmed=True)
        self.assertTrue(reopened.accept(encode(message(seq=2, released="N5"))).new)

    def test_new_job_cannot_replace_unfinished_route(self):
        first = self.inbox.accept(encode(message()))
        self.inbox.mark_completed("T1", 1, first.digest, "N3", rear_clear_confirmed=True)
        with self.assertRaises(self.module.OrderRejected):
            self.inbox.accept(encode(message(order_id="T2")))
        second = self.inbox.accept(encode(message(seq=2, released="N5")))
        self.inbox.mark_completed("T1", 2, second.digest, "N5", rear_clear_confirmed=True)
        self.assertTrue(self.inbox.accept(encode(message(order_id="T2"))).new)

    def test_invalid_messages_and_different_map_or_robot_are_rejected(self):
        bad = message(); bad["map_id"] = "other@v1"
        extra = message(); extra["extra"] = 1
        for raw in (b'\xff', b'[]', b'{"seq":1,"seq":2}', encode(bad), encode(extra), b'{}'):
            with self.subTest(raw=raw):
                with self.assertRaises(self.module.OrderRejected):
                    self.inbox.accept(raw)
        with self.assertRaises(self.module.OrderRejected):
            self.module.SimOrderInbox(self.path, "robot1", "layout@v1")
        with self.assertRaises(self.module.OrderRejected):
            self.module.SimOrderInbox(self.path, "sim-r2", "layout@v1").initialize()

    def test_simultaneous_duplicate_only_one_new(self):
        other = self.module.SimOrderInbox(self.path, "sim-r1", "layout@v1")
        other.initialize()
        gate = threading.Barrier(2)
        results, errors = [], []
        def receive(inbox):
            try:
                gate.wait()
                results.append(inbox.accept(encode(message())).new)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=receive, args=(item,)) for item in (self.inbox, other)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])

    def test_corrupt_persisted_payload_cannot_be_confirmed_or_advanced(self):
        first = self.inbox.accept(encode(message()))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE sim_inbox_orders SET payload=? WHERE order_id=? AND seq=?",
                       (json.dumps(message(released="N5")), "T1", 1))
            db.commit()
        with self.assertRaises(self.module.OrderRejected):
            self.inbox.mark_completed("T1", 1, first.digest, "N5", rear_clear_confirmed=True)
        with self.assertRaises(self.module.OrderRejected):
            self.inbox.accept(encode(message(seq=2, released="N5")))

    def test_identity_loss_does_not_allow_rebinding_an_existing_inbox(self):
        self.inbox.accept(encode(message()))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM sim_inbox_identity")
            db.commit()
        with self.assertRaises(self.module.OrderRejected):
            self.module.SimOrderInbox(self.path, "sim-r2", "layout@v1").initialize()

    def test_reinitializing_same_instance_revokes_old_execution_authority(self):
        first = self.inbox.accept(encode(message()))
        self.inbox.initialize()
        with self.assertRaises(self.module.OrderRejected):
            self.inbox.mark_completed("T1", 1, first.digest, "N3", rear_clear_confirmed=True)
        self.assertFalse(self.inbox.accept(encode(message())).new)


if __name__ == "__main__":
    unittest.main()
