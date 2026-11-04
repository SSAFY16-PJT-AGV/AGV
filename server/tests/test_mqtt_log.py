import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from server.mqtt_log import JsonlRecorder, read_log, replay


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "mqtt.jsonl"

    def record_pair(self):
        ticks = iter([10.0, 10.0, 11.25])
        clock = lambda: next(ticks)
        now = lambda: datetime(2026, 11, 4, 1, 2, 3, tzinfo=timezone.utc)
        with JsonlRecorder(self.path, clock=clock, now=now) as recorder:
            recorder.record("agv/a/conn", b"offline", qos=1, retain=True)
            recorder.record("agv/a/state", b"\x00\xff{}", qos=0, retain=False)

    def test_records_utc_binary_payload_and_metadata_without_overwriting(self):
        self.record_pair()
        rows = read_log(self.path)
        self.assertEqual(rows[0]["received_at"], "2026-11-04T01:02:03+00:00")
        self.assertEqual(rows[0]["payload"], b"offline")
        self.assertEqual((rows[0]["qos"], rows[0]["retain"]), (1, True))
        self.assertEqual(rows[1]["payload"], b"\x00\xff{}")
        self.assertEqual(rows[1]["elapsed"], 1.25)
        with self.assertRaises(FileExistsError):
            JsonlRecorder(self.path)

    def test_dry_run_does_not_publish_or_sleep(self):
        self.record_pair()
        calls = []
        plan = replay(self.path, publish=lambda *args: calls.append(args),
                      sleep=lambda delay: calls.append(delay))
        self.assertEqual(calls, [])
        self.assertEqual(plan[0]["topic"], "replay/agv/a/conn")
        self.assertEqual(plan[1]["delay"], 1.25)

    def test_explicit_replay_stays_in_replay_namespace_and_preserves_metadata(self):
        self.record_pair()
        events = []
        replay(self.path, allow_publish=True,
               publish=lambda *args: events.append(("publish", args)),
               sleep=lambda delay: events.append(("sleep", delay)),
               clock=lambda: 100.0)
        self.assertEqual(events, [
            ("publish", ("replay/agv/a/conn", b"offline", 1, True)),
            ("sleep", 1.25),
            ("publish", ("replay/agv/a/state", b"\x00\xff{}", 0, False)),
        ])

    def test_publishing_time_does_not_accumulate_in_replay_schedule(self):
        self.record_pair()
        now = [100.0]
        slept = []
        def publish(*args):
            now[0] += 0.5
        def sleep(delay):
            slept.append(delay)
            now[0] += delay
        replay(self.path, allow_publish=True, publish=publish, sleep=sleep,
               clock=lambda: now[0])
        self.assertEqual(slept, [0.75])

    def test_corrupt_later_record_prevents_all_publication(self):
        self.record_pair()
        with self.path.open("a", encoding="utf-8") as file:
            file.write('{"topic": "agv/a/order"}\n')
        calls = []
        with self.assertRaises(ValueError):
            replay(self.path, allow_publish=True,
                   publish=lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_replay_rejects_reversed_time_and_invalid_base64(self):
        self.record_pair()
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        rows[1]["elapsed"] = -1
        self.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_log(self.path)
        rows[1]["elapsed"] = 2
        rows[1]["payload_b64"] = "not-base64!"
        self.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_log(self.path)

    def test_wildcard_topics_and_invalid_qos_are_rejected(self):
        with JsonlRecorder(self.path) as recorder:
            with self.assertRaises(ValueError):
                recorder.record("agv/+/state", b"{}", 0, False)
            with self.assertRaises(ValueError):
                recorder.record("agv/a/state", b"{}", 3, False)

    def test_invalid_mqtt_topic_utf8_and_lengths_rejected(self):
        with JsonlRecorder(self.path) as recorder:
            for topic in ("bad\ud800topic", "a" * 65536):
                with self.subTest(topic_length=len(topic)):
                    with self.assertRaises(ValueError):
                        recorder.record(topic, b"{}", 0, False)

    def test_replay_checks_prefixed_topic_lengths_before_any_publication(self):
        with JsonlRecorder(self.path) as recorder:
            recorder.record("agv/a/state", b"{}", 0, False)
            recorder.record("a" * 65529, b"{}", 0, False)
        calls = []
        with self.assertRaises(ValueError):
            replay(self.path, allow_publish=True, publish=lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_replay_rejects_invalid_utf8_topic_before_any_publication(self):
        self.record_pair()
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        rows[1]["topic"] = "invalid\ud800topic"
        self.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        calls = []
        with self.assertRaises(ValueError):
            replay(self.path, allow_publish=True, publish=lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_immediate_publish_failure_stops_further_messages(self):
        self.record_pair()
        calls = []
        def disconnected(*args):
            calls.append(args)
            return SimpleNamespace(rc=4)
        with self.assertRaises(RuntimeError):
            replay(self.path, allow_publish=True, publish=disconnected, sleep=lambda _: None)
        self.assertEqual(len(calls), 1)

    def test_successful_paho_result_is_accepted(self):
        self.record_pair()
        calls = []
        def publisher(*args):
            calls.append(args)
            return SimpleNamespace(rc=0)
        replay(self.path, allow_publish=True, publish=publisher, sleep=lambda _: None)
        self.assertEqual(len(calls), 2)

    def test_unknown_publish_result_is_not_assumed_successful(self):
        self.record_pair()
        with self.assertRaises(RuntimeError):
            replay(self.path, allow_publish=True, publish=lambda *args: object())


if __name__ == "__main__":
    unittest.main()
