import importlib
import importlib.util
import json
import unittest
from types import SimpleNamespace


def order():
    return {
        "order_id": "T-0012", "seq": 3, "map_id": "layout_A@v4",
        "nodes": ["N3", "N4", "N7", "N9"], "released_until": "N7",
        "action_at_end": {"type": "unload", "shelf": "S2"},
    }


class FakeClient:
    def __init__(self, connected=True, rc=0):
        self.connected = connected
        self._reconnect_on_failure = False
        self.rc = rc
        self.calls = []
        self.info = SimpleNamespace(rc=rc, mid=10)

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos, retain):
        self.calls.append((topic, payload, qos, retain))
        return self.info


class OrderPublisherTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("server.order_publisher"),
                             "server.order_publisher must exist")
        self.module = importlib.import_module("server.order_publisher")

    def test_disabled_by_default_and_cannot_publish(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client)
        with self.assertRaises(self.module.OrderPublishingDisabled):
            publisher.publish("sim-robot1", order())
        self.assertEqual(client.calls, [])

    def test_enabled_publishes_exact_json_with_qos1_and_no_retain(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client, enabled=True)
        info = publisher.publish("sim-robot1", order())
        self.assertIs(info, client.info)
        self.assertEqual(len(client.calls), 1)
        topic, payload, qos, retain = client.calls[0]
        self.assertEqual((topic, qos, retain), ("agv/sim-robot1/order", 1, False))
        self.assertEqual(json.loads(payload), order())

    def test_disabled_or_disconnected_never_calls_publish(self):
        client = FakeClient(connected=False)
        publisher = self.module.OrderPublisher(client, enabled=True)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order())
        self.assertEqual(client.calls, [])

    def test_invalid_robot_id_never_published(self):
        for value in ("", " ", "robot1", "sim-", "sim-r/1", "sim-+", "sim-r#", "sim-r\x00", "sim-\ud800", "sim-" + "a" * 129):
            with self.subTest(value=value):
                client = FakeClient()
                with self.assertRaises(ValueError):
                    self.module.OrderPublisher(client, enabled=True).publish(value, order())
                self.assertEqual(client.calls, [])

    def test_invalid_orders_never_published(self):
        variants = []
        bad = order(); del bad["seq"]; variants.append(bad)
        bad = order(); bad["surprise"] = True; variants.append(bad)
        bad = order(); bad["seq"] = True; variants.append(bad)
        bad = order(); bad["seq"] = -1; variants.append(bad)
        bad = order(); bad["nodes"] = []; variants.append(bad)
        bad = order(); bad["nodes"] = ["N1", ""]; variants.append(bad)
        bad = order(); bad["released_until"] = "N0"; variants.append(bad)
        bad = order(); bad["action_at_end"] = {}; variants.append(bad)
        bad = order(); bad["action_at_end"] = {"type": "unload", "value": float("nan")}; variants.append(bad)
        bad = order(); bad["action_at_end"] = {"type": "\ud800"}; variants.append(bad)
        bad = order(); bad["action_at_end"] = {"type": "unload", 123: "x"}; variants.append(bad)
        bad = order(); bad["action_at_end"] = {"type": "unload", "amount": float("inf")}; variants.append(bad)
        for value in variants:
            with self.subTest(value=value):
                client = FakeClient()
                with self.assertRaises(ValueError):
                    self.module.OrderPublisher(client, enabled=True).publish("sim-robot1", value)
                self.assertEqual(client.calls, [])

    def test_rejected_enqueue_is_reported_and_is_not_retried(self):
        client = FakeClient(rc=4)
        publisher = self.module.OrderPublisher(client, enabled=True)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order())
        self.assertEqual(len(client.calls), 1)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order())
        self.assertEqual(len(client.calls), 1)

    def test_one_instance_cannot_send_twice_even_after_success(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client, enabled=True)
        publisher.publish("sim-robot1", order())
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order())
        self.assertEqual(len(client.calls), 1)

    def test_client_exception_is_reported_without_retry(self):
        client = FakeClient()
        def fail(*args, **kwargs):
            raise OSError("disconnect")
        client.publish = fail
        with self.assertRaises(self.module.OrderPublishError):
            self.module.OrderPublisher(client, enabled=True).publish("sim-robot1", order())

    def test_enabling_requires_an_actual_boolean(self):
        for value in (None, 1, "true"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.module.OrderPublisher(FakeClient(), enabled=value)

    def test_enabled_publisher_refuses_automatic_reconnect(self):
        client = FakeClient()
        client._reconnect_on_failure = True
        with self.assertRaises(ValueError):
            self.module.OrderPublisher(client, enabled=True)
        self.assertEqual(client.calls, [])

    def test_reconnect_setting_changed_after_construction_prevents_publish(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client, enabled=True)
        client._reconnect_on_failure = True
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order())
        self.assertEqual(client.calls, [])

    def test_validate_has_no_send_or_one_shot_side_effect(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client, enabled=True)
        publisher.validate("sim-robot1", order())
        publisher.validate("sim-robot1", order())
        self.assertEqual(client.calls, [])
        publisher.publish("sim-robot1", order())
        self.assertEqual(len(client.calls), 1)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.validate("sim-robot1", order())

    def test_validate_checks_disabled_invalid_and_disconnected_without_sending(self):
        client = FakeClient()
        with self.assertRaises(self.module.OrderPublishingDisabled):
            self.module.OrderPublisher(client).validate("sim-robot1", order())
        publisher = self.module.OrderPublisher(client, enabled=True)
        with self.assertRaises(ValueError):
            publisher.validate("robot1", order())
        invalid = order(); invalid["released_until"] = "missing"
        with self.assertRaises(ValueError):
            publisher.validate("sim-robot1", invalid)
        client.connected = False
        with self.assertRaises(self.module.OrderPublishError):
            publisher.validate("sim-robot1", order())
        self.assertEqual(client.calls, [])

    def test_validate_checks_reconnect_flag_and_connection_errors(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client, enabled=True)
        client._reconnect_on_failure = True
        with self.assertRaises(self.module.OrderPublishError):
            publisher.validate("sim-robot1", order())
        client._reconnect_on_failure = False
        def fail():
            raise OSError("not connected")
        client.is_connected = fail
        with self.assertRaises(self.module.OrderPublishError):
            publisher.validate("sim-robot1", order())
        self.assertEqual(client.calls, [])

    def test_validate_order_is_pure_schema_check_without_mqtt_client(self):
        self.module.validate_order("sim-robot1", order())
        with self.assertRaises(ValueError):
            self.module.validate_order("robot1", order())
        invalid = order(); invalid["released_until"] = "missing"
        with self.assertRaises(ValueError):
            self.module.validate_order("sim-robot1", invalid)


if __name__ == "__main__":
    unittest.main()
