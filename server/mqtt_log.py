"""Local, opt-in MQTT recording and isolated replay; opens no network connections."""

import base64
import binascii
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone


def _valid_topic(topic):
    if (not isinstance(topic, str) or not topic or "\x00" in topic
            or "+" in topic or "#" in topic):
        return False
    try:
        return len(topic.encode("utf-8", errors="strict")) <= 65535
    except UnicodeEncodeError:
        return False


def _check_message(topic, payload, qos, retain):
    if not _valid_topic(topic):
        raise ValueError("invalid MQTT topic")
    if not isinstance(payload, bytes):
        raise ValueError("payload must be bytes")
    if type(qos) is not int or qos not in (0, 1, 2):
        raise ValueError("invalid QoS")
    if type(retain) is not bool:
        raise ValueError("retain must be a boolean")


class JsonlRecorder:
    """Record callback observations into a new JSONL file (never overwrite)."""

    def __init__(self, path, clock=time.monotonic, now=None):
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._start = clock()
        if not math.isfinite(self._start):
            raise ValueError("invalid monotonic clock")
        self._last_elapsed = 0.0
        self._lock = threading.Lock()
        self._file = open(path, "x", encoding="utf-8", newline="\n")

    def record(self, topic, payload, qos, retain):
        _check_message(topic, payload, qos, retain)
        with self._lock:
            elapsed = self._clock() - self._start
            if not math.isfinite(elapsed) or elapsed < self._last_elapsed:
                raise ValueError("monotonic clock moved backwards")
            instant = self._now()
            if not isinstance(instant, datetime) or instant.utcoffset() is None:
                raise ValueError("a timezone-aware reception time is required")
            row = {
                "received_at": instant.astimezone(timezone.utc).isoformat(),
                "elapsed": elapsed,
                "topic": topic,
                "payload_b64": base64.b64encode(payload).decode("ascii"),
                "qos": qos,
                "retain": retain,
            }
            self._file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            self._file.flush()
            self._last_elapsed = elapsed

    def close(self):
        with self._lock:
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON constant")


def read_log(path):
    """Read and validate the entire log before returning messages with byte payloads."""
    result = []
    previous = 0.0
    with open(path, encoding="utf-8") as file:
        for index, line in enumerate(file, start=1):
            try:
                row = json.loads(line, object_pairs_hook=_unique_pairs,
                                 parse_constant=_invalid_constant)
                if not isinstance(row, dict):
                    raise ValueError("record must be an object")
                raw = row["payload_b64"]
                if not isinstance(raw, str):
                    raise ValueError("invalid payload encoding")
                payload = base64.b64decode(raw.encode("ascii"), validate=True)
                _check_message(row["topic"], payload, row["qos"], row["retain"])
                elapsed = row["elapsed"]
                if (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
                        or not math.isfinite(elapsed) or elapsed < previous):
                    raise ValueError("invalid or out-of-order elapsed time")
                instant = datetime.fromisoformat(row["received_at"])
                if instant.utcoffset() != timedelta(0):
                    raise ValueError("reception time must be UTC")
                result.append({"received_at": row["received_at"], "elapsed": elapsed,
                               "topic": row["topic"], "payload": payload,
                               "qos": row["qos"], "retain": row["retain"]})
                previous = elapsed
            except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error) as exc:
                raise ValueError("invalid log record on line {}".format(index)) from exc
    return result


def replay(path, publish=None, allow_publish=False, sleep=time.sleep,
           clock=time.monotonic):
    """Plan replay by default; explicit publishing is forced under replay/."""
    if type(allow_publish) is not bool:
        raise ValueError("allow_publish must be a boolean")
    records = read_log(path)
    plan = []
    previous = records[0]["elapsed"] if records else 0.0
    for row in records:
        replay_topic = "replay/" + row["topic"]
        if not _valid_topic(replay_topic):
            raise ValueError("prefixed replay topic is invalid or too long")
        plan.append({"topic": replay_topic, "payload": row["payload"],
                     "qos": row["qos"], "retain": row["retain"],
                     "delay": row["elapsed"] - previous})
        previous = row["elapsed"]
    if not allow_publish:
        return plan
    if not callable(publish) or not callable(sleep) or not callable(clock):
        raise ValueError("explicit replay requires publish, sleep and clock callables")
    start = clock()
    planned_offset = 0.0
    for item in plan:
        planned_offset += item["delay"]
        delay = max(0.0, start + planned_offset - clock())
        if delay:
            sleep(delay)
        result = publish(item["topic"], item["payload"], item["qos"], item["retain"])
        if result is not None:
            code = getattr(result, "rc", None)
            if isinstance(code, bool) or not isinstance(code, int) or code != 0:
                raise RuntimeError("replay publish failed or returned an unknown result")
    return plan
