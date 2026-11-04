import importlib
import importlib.util
import gc
import json
import threading
import weakref
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

    def claimed(self, client):
        publisher = self.module.OrderPublisher(client, enabled=True)
        owner = object()
        publisher.claim(owner)
        return publisher, owner

    def test_disabled_by_default_and_cannot_publish(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client)
        with self.assertRaises(self.module.OrderPublishingDisabled):
            publisher.publish("sim-robot1", order())
        self.assertEqual(client.calls, [])

    def test_enabled_publishes_exact_json_with_qos1_and_no_retain(self):
        client = FakeClient()
        publisher, owner = self.claimed(client)
        info = publisher.publish("sim-robot1", order(), owner=owner)
        self.assertIs(info, client.info)
        self.assertEqual(len(client.calls), 1)
        topic, payload, qos, retain = client.calls[0]
        self.assertEqual((topic, qos, retain), ("agv/sim-robot1/order", 1, False))
        self.assertEqual(json.loads(payload), order())

    def test_disabled_or_disconnected_never_calls_publish(self):
        client = FakeClient(connected=False)
        publisher, owner = self.claimed(client)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order(), owner=owner)
        self.assertEqual(client.calls, [])

    def test_invalid_robot_id_never_published(self):
        for value in ("", " ", "robot1", "sim-", "sim-r/1", "sim-+", "sim-r#", "sim-r\x00", "sim-\ud800", "sim-" + "a" * 129):
            with self.subTest(value=value):
                client = FakeClient()
                publisher, owner = self.claimed(client)
                with self.assertRaises(ValueError):
                    publisher.publish(value, order(), owner=owner)
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
                publisher, owner = self.claimed(client)
                with self.assertRaises(ValueError):
                    publisher.publish("sim-robot1", value, owner=owner)
                self.assertEqual(client.calls, [])

    def test_rejected_enqueue_is_reported_and_is_not_retried(self):
        client = FakeClient(rc=4)
        publisher, owner = self.claimed(client)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order(), owner=owner)
        self.assertEqual(len(client.calls), 1)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order(), owner=owner)
        self.assertEqual(len(client.calls), 1)

    def test_one_instance_cannot_send_twice_even_after_success(self):
        client = FakeClient()
        publisher, owner = self.claimed(client)
        publisher.publish("sim-robot1", order(), owner=owner)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order(), owner=owner)
        self.assertEqual(len(client.calls), 1)

    def test_client_exception_is_reported_without_retry(self):
        client = FakeClient()
        def fail(*args, **kwargs):
            raise OSError("disconnect")
        client.publish = fail
        publisher, owner = self.claimed(client)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order(), owner=owner)

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
        publisher, owner = self.claimed(client)
        client._reconnect_on_failure = True
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order(), owner=owner)
        self.assertEqual(client.calls, [])

    def test_validate_has_no_send_or_one_shot_side_effect(self):
        client = FakeClient()
        publisher, owner = self.claimed(client)
        publisher.validate("sim-robot1", order(), owner=owner)
        publisher.validate("sim-robot1", order(), owner=owner)
        self.assertEqual(client.calls, [])
        publisher.publish("sim-robot1", order(), owner=owner)
        self.assertEqual(len(client.calls), 1)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.validate("sim-robot1", order(), owner=owner)

    def test_validate_checks_disabled_invalid_and_disconnected_without_sending(self):
        client = FakeClient()
        with self.assertRaises(self.module.OrderPublishingDisabled):
            self.module.OrderPublisher(client).validate("sim-robot1", order())
        publisher, owner = self.claimed(client)
        with self.assertRaises(ValueError):
            publisher.validate("robot1", order(), owner=owner)
        invalid = order(); invalid["released_until"] = "missing"
        with self.assertRaises(ValueError):
            publisher.validate("sim-robot1", invalid, owner=owner)
        client.connected = False
        with self.assertRaises(self.module.OrderPublishError):
            publisher.validate("sim-robot1", order(), owner=owner)
        self.assertEqual(client.calls, [])

    def test_validate_checks_reconnect_flag_and_connection_errors(self):
        client = FakeClient()
        publisher, owner = self.claimed(client)
        client._reconnect_on_failure = True
        with self.assertRaises(self.module.OrderPublishError):
            publisher.validate("sim-robot1", order(), owner=owner)
        client._reconnect_on_failure = False
        def fail():
            raise OSError("not connected")
        client.is_connected = fail
        with self.assertRaises(self.module.OrderPublishError):
            publisher.validate("sim-robot1", order(), owner=owner)
        self.assertEqual(client.calls, [])

    def test_validate_order_is_pure_schema_check_without_mqtt_client(self):
        self.module.validate_order("sim-robot1", order())
        with self.assertRaises(ValueError):
            self.module.validate_order("robot1", order())
        invalid = order(); invalid["released_until"] = "missing"
        with self.assertRaises(ValueError):
            self.module.validate_order("sim-robot1", invalid)

    def test_claim_blocks_other_owner_and_direct_unclaimed_publish(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client, enabled=True)
        owner = object()
        publisher.claim(owner)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.claim(owner)
        publisher.validate("sim-robot1", order(), owner=owner)
        for other in (None, object()):
            with self.subTest(other=other):
                with self.assertRaises(self.module.OrderPublishError):
                    publisher.validate("sim-robot1", order(), owner=other)
                with self.assertRaises(self.module.OrderPublishError):
                    publisher.publish("sim-robot1", order(), owner=other)
        self.assertEqual(client.calls, [])
        publisher.publish("sim-robot1", order(), owner=owner)
        self.assertEqual(len(client.calls), 1)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.release_claim(owner)

    def test_unclaimed_publisher_cannot_validate_or_publish(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client, enabled=True)
        for owner in (None, object()):
            with self.subTest(owner=owner):
                with self.assertRaises(self.module.OrderPublishError):
                    publisher.validate("sim-robot1", order(), owner=owner)
                with self.assertRaises(self.module.OrderPublishError):
                    publisher.publish("sim-robot1", order(), owner=owner)
        self.assertEqual(client.calls, [])

    def test_claim_and_release_require_same_owner_identity(self):
        class EqualObjects:
            def __eq__(self, other):
                return True
        client = FakeClient()
        publisher = self.module.OrderPublisher(client, enabled=True)
        owner, same_value = EqualObjects(), EqualObjects()
        publisher.claim(owner)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.claim(same_value)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.release_claim(same_value)
        publisher.release_claim(owner)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order())
        new_owner = object()
        publisher.claim(new_owner)
        publisher.publish("sim-robot1", order(), owner=new_owner)
        self.assertEqual(len(client.calls), 1)

    def test_claim_cannot_be_released_after_an_uncertain_attempt(self):
        client = FakeClient(rc=4)
        publisher = self.module.OrderPublisher(client, enabled=True)
        owner = object()
        publisher.claim(owner)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order(), owner=owner)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.release_claim(owner)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.claim(object())
        self.assertEqual(len(client.calls), 1)

    def test_two_simultaneous_coordinators_cannot_claim_same_publisher(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client, enabled=True)
        barrier = threading.Barrier(2)
        successes, failures = [], []
        def try_claim(owner):
            barrier.wait()
            try:
                publisher.claim(owner)
                successes.append(owner)
            except self.module.OrderPublishError:
                failures.append(owner)
        threads = [threading.Thread(target=try_claim, args=(object(),)) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual((len(successes), len(failures)), (1, 1))
        with self.assertRaises(self.module.OrderPublishError):
            publisher.publish("sim-robot1", order(), owner=failures[0])
        publisher.publish("sim-robot1", order(), owner=successes[0])
        self.assertEqual(len(client.calls), 1)

    def test_disabled_or_invalid_owner_cannot_claim(self):
        publisher = self.module.OrderPublisher(FakeClient())
        with self.assertRaises(self.module.OrderPublishingDisabled):
            publisher.claim(object())
        publisher = self.module.OrderPublisher(FakeClient(), enabled=True)
        with self.assertRaises(ValueError):
            publisher.claim(None)
        with self.assertRaises(self.module.OrderPublishError):
            publisher.release_claim(object())

    def test_same_client_cannot_be_wrapped_again_after_first_attempt(self):
        client = FakeClient()
        publisher, owner = self.claimed(client)
        publisher.publish("sim-robot1", order(), owner=owner)
        with self.assertRaises(ValueError):
            self.module.OrderPublisher(client, enabled=True)
        self.assertEqual(len(client.calls), 1)

    def test_same_client_cannot_be_wrapped_after_publisher_is_collected(self):
        client = FakeClient()
        publisher = self.module.OrderPublisher(client, enabled=True)
        del publisher
        gc.collect()
        with self.assertRaises(ValueError):
            self.module.OrderPublisher(client, enabled=True)
        self.assertEqual(client.calls, [])

    def test_two_simultaneous_wrappers_of_same_client_have_only_one_winner(self):
        client = FakeClient()
        gate = threading.Barrier(2)
        successes, failures = [], []
        def wrap():
            gate.wait()
            try:
                successes.append(self.module.OrderPublisher(client, enabled=True))
            except ValueError:
                failures.append(True)
        threads = [threading.Thread(target=wrap) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual((len(successes), len(failures)), (1, 1))
        owner = object()
        successes[0].claim(owner)
        successes[0].publish("sim-robot1", order(), owner=owner)
        self.assertEqual(len(client.calls), 1)

    def test_client_tracking_uses_identity_not_equality_and_does_not_keep_client_alive(self):
        class EqualClient(FakeClient):
            __hash__ = None
            def __eq__(self, other):
                return True
        first, second = EqualClient(), EqualClient()
        one = self.module.OrderPublisher(first, enabled=True)
        two = self.module.OrderPublisher(second, enabled=True)
        ref = weakref.ref(first)
        first_id = id(first)
        del first, one
        gc.collect()
        self.assertIsNone(ref())
        self.assertNotIn(first_id, self.module._registered_clients)
        owner = object()
        two.claim(owner)
        two.publish("sim-robot1", order(), owner=owner)

    def test_disabled_wrapper_does_not_claim_client(self):
        client = FakeClient()
        self.module.OrderPublisher(client)
        publisher, owner = self.claimed(client)
        publisher.publish("sim-robot1", order(), owner=owner)
        self.assertEqual(len(client.calls), 1)

    def test_untrackable_client_cannot_be_enabled(self):
        class UntrackableClient:
            __slots__ = ()
            _reconnect_on_failure = False
        with self.assertRaises(ValueError):
            self.module.OrderPublisher(UntrackableClient(), enabled=True)


if __name__ == "__main__":
    unittest.main()
