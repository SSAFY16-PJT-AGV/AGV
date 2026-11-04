import asyncio
import json
import unittest

from server.main import app


async def request(method, path):
    messages = []
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [], "client": ("test", 1234), "server": ("test", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    first = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return first["status"], body, dict(first.get("headers", []))


class FakeService:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error

    def snapshot(self):
        if self.error is not None:
            raise self.error
        return self.data


class RobotsApiTest(unittest.TestCase):
    def setUp(self):
        self.old_state = app.state._state.copy()

    def tearDown(self):
        app.state._state.clear()
        app.state._state.update(self.old_state)

    def call(self, method="GET"):
        return asyncio.run(request(method, "/robots"))

    def test_returns_observation_unchanged_not_a_movement_permission(self):
        data = {"connected": True, "ready": True, "robots": {
            "robot1": {"state": {"last_node": "N1"}, "state_received_live": False,
                       "connection_report": "online", "connection_received_live": False}}}
        app.state.mqtt_service = FakeService(data)
        status, body, headers = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), data)
        self.assertEqual(headers.get(b"cache-control"), b"no-store")

    def test_disconnected_snapshot_preserves_historical_observations(self):
        data = {"connected": False, "ready": False, "robots": {
            "robot1": {"state": {"last_node": "N1"}, "state_received_live": False,
                       "connection_report": "online", "connection_received_live": False}}}
        app.state.mqtt_service = FakeService(data)
        status, body, _ = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), data)

    def test_missing_service_returns_503_without_internal_details(self):
        app.state._state.pop("mqtt_service", None)
        status, body, headers = self.call()
        self.assertEqual(status, 503)
        self.assertNotIn(b"Traceback", body)
        self.assertEqual(headers.get(b"cache-control"), b"no-store")

    def test_snapshot_failure_is_503_without_exception_details(self):
        app.state.mqtt_service = FakeService(error=RuntimeError("secret broker password"))
        status, body, _ = self.call()
        self.assertEqual(status, 503)
        self.assertNotIn(b"secret", body)

    def test_malformed_snapshot_is_not_reported_as_ok(self):
        for value in (None, {"connected": True, "ready": True},
                      {"connected": False, "ready": True, "robots": {}},
                      {"connected": True, "ready": True, "robots": {"x": float("nan")}}):
            with self.subTest(value=value):
                app.state.mqtt_service = FakeService(value)
                status, _, _ = self.call()
                self.assertEqual(status, 503)

    def test_endpoint_does_not_accept_commands(self):
        status, _, _ = self.call("POST")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
