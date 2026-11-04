"""Explicit, simulation-only MQTT order publisher; no network setup.

QoS 1 queue acceptance and broker PUBACK are not evidence of robot receipt or
execution. Paho may retain a failed QoS 1 publish for later redelivery; use a
dedicated isolated client with reconnect disabled and discard it on uncertainty.
The caller must durably record each attempt and retain reservations before use.
"""
import json
import math
import threading

import paho.mqtt.client as mqtt


_ORDER_FIELDS = frozenset(("order_id", "seq", "map_id", "nodes", "released_until", "action_at_end"))
_MAX_PAYLOAD_BYTES = 256 * 1024
_MAX_JSON_DEPTH = 32
_MAX_ROBOT_ID_BYTES = 128


class OrderPublishingDisabled(RuntimeError):
    """Publishing must be explicitly enabled by the simulation caller."""


class OrderPublishError(RuntimeError):
    """Publish did not report queue acceptance, or may have an unknown outcome."""


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _validate_json(value, depth=0):
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("order JSON is nested too deeply")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid Unicode in order") from exc
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("order numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("order object keys must be strings")
            _validate_json(key, depth + 1)
            _validate_json(item, depth + 1)
        return
    raise ValueError("order must contain only JSON values")


def _topic(robot_id):
    if not _nonempty_string(robot_id) or not robot_id.startswith("sim-") or not robot_id[4:].strip():
        raise ValueError("only non-empty simulation robot IDs starting with sim- are allowed")
    if any(ch in robot_id for ch in ("/", "+", "#")) or any(ord(ch) < 32 or ord(ch) == 127 for ch in robot_id):
        raise ValueError("invalid robot ID for MQTT topic")
    try:
        encoded = robot_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid robot ID for MQTT topic") from exc
    if len(encoded) > _MAX_ROBOT_ID_BYTES:
        raise ValueError("robot ID exceeds temporary size limit")
    return "agv/{}/order".format(robot_id)


def _payload(order):
    if not isinstance(order, dict) or set(order) != _ORDER_FIELDS:
        raise ValueError("order must contain exactly the six documented fields")
    _validate_json(order)
    if not _nonempty_string(order["order_id"]) or not _nonempty_string(order["map_id"]):
        raise ValueError("order_id and map_id must be non-empty strings")
    seq = order["seq"]
    if type(seq) is not int or seq < 0:
        raise ValueError("seq must be a non-negative integer")
    nodes = order["nodes"]
    if not isinstance(nodes, list) or not nodes or any(not _nonempty_string(node) for node in nodes):
        raise ValueError("nodes must be a non-empty list of non-empty node IDs")
    if not _nonempty_string(order["released_until"]) or order["released_until"] not in nodes:
        raise ValueError("released_until must be a node in the route")
    action = order["action_at_end"]
    if not isinstance(action, dict) or not _nonempty_string(action.get("type")):
        raise ValueError("action_at_end must be an object with a non-empty type")
    try:
        payload = json.dumps(order, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError, TypeError) as exc:
        raise ValueError("invalid JSON order") from exc
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise ValueError("order exceeds temporary payload size limit")
    return payload


def validate_order(robot_id, order):
    """Check simulation target and JSON structure only; does not access MQTT."""
    _topic(robot_id)
    _payload(order)


class OrderPublisher:
    """One-shot publisher for an explicitly isolated simulation client only."""

    def __init__(self, client, *, enabled=False):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        # Paho 2.1 has no public getter for this construction option. Requiring
        # it to be explicitly False avoids automatically replaying QoS 1 queue
        # entries after disconnect; the caller must still never reconnect/reuse.
        if enabled and getattr(client, "_reconnect_on_failure", None) is not False:
            raise ValueError("simulation client must disable automatic reconnect")
        self._client = client
        self._enabled = enabled
        self._attempted = False
        self._lock = threading.Lock()
        self._claim_owner = None

    def claim(self, owner):
        """Reserve this one-shot publisher for one in-process operation."""
        if not self._enabled:
            raise OrderPublishingDisabled("simulation order publishing is disabled")
        if owner is None:
            raise ValueError("claim owner must be a non-None identity")
        with self._lock:
            if self._attempted or self._claim_owner is not None:
                raise OrderPublishError("publisher is already claimed or used")
            self._claim_owner = owner

    def release_claim(self, owner):
        """Release only before any attempt and only for the identical owner.

        The caller must independently verify that no durable reservation or
        uncertain order requires the claim before releasing it.
        """
        with self._lock:
            if self._attempted or self._claim_owner is None or owner is not self._claim_owner:
                raise OrderPublishError("publisher claim cannot be released")
            self._claim_owner = None

    def _check_owner(self, owner):
        if self._claim_owner is not owner:
            raise OrderPublishError("publisher claim belongs to another operation")

    def _check_transport(self):
        try:
            if getattr(self._client, "_reconnect_on_failure", None) is not False:
                raise OrderPublishError("automatic reconnect must remain disabled")
            if self._client.is_connected() is not True:
                raise OrderPublishError("MQTT client is not connected")
        except OrderPublishError:
            raise
        except Exception as exc:
            raise OrderPublishError("could not check MQTT connection") from exc

    def validate(self, robot_id, order, *, owner=None):
        """Check current readiness without sending or consuming the one attempt.

        The connection may change immediately after this check; the caller
        must treat failure after persisting an attempt as an uncertain outcome.
        """
        if not self._enabled:
            raise OrderPublishingDisabled("simulation order publishing is disabled")
        validate_order(robot_id, order)
        with self._lock:
            self._check_owner(owner)
            if self._attempted:
                raise OrderPublishError("publisher is one-shot; create a new isolated client")
            self._check_transport()

    def publish(self, robot_id, order, *, owner=None):
        if not self._enabled:
            raise OrderPublishingDisabled("simulation order publishing is disabled")
        topic = _topic(robot_id)
        payload = _payload(order)
        with self._lock:
            self._check_owner(owner)
            if self._attempted:
                raise OrderPublishError("publisher is one-shot; create a new isolated client")
            self._attempted = True
            self._check_transport()
            try:
                info = self._client.publish(topic, payload, qos=1, retain=False)
            except Exception as exc:
                raise OrderPublishError("MQTT publish outcome is uncertain") from exc
            rc = getattr(info, "rc", None)
            if isinstance(rc, bool) or not isinstance(rc, int) or rc != mqtt.MQTT_ERR_SUCCESS:
                raise OrderPublishError("MQTT enqueue failed or outcome is uncertain")
            return info
