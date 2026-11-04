import asyncio
import json
import unittest

from fastapi import FastAPI


class RecordingRuntime:
    def __init__(self):
        self.calls = []
        self.failure = None

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            if self.failure is not None:
                raise self.failure
            return {"method": name}
        return call


async def request(app, method, path, *, body=None, token="secret-token", host="127.0.0.1"):
    messages = []
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    headers = [(b"content-type", b"application/json")]
    if token is not None:
        headers.append((b"authorization", token.encode("utf-8")))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": headers, "client": (host, 1234) if host is not None else None,
        "server": ("localhost", 8000),
    }
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], json.loads(raw), dict(start["headers"])


class SimulationApiTests(unittest.TestCase):
    def setUp(self):
        from server.sim_api import router
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.state.sim_api_token = "secret-token"
        self.runtime = RecordingRuntime()
        self.app.state.sim_runtime = self.runtime

    def call(self, method, path, **kwargs):
        kwargs.setdefault("token", "Bearer secret-token")
        return asyncio.run(request(self.app, method, "/simulation" + path, **kwargs))

    def test_authentication_and_socket_are_required(self):
        for token in (None, "secret-token", "Bearer incorrect", "Bearer ", "Basic secret-token"):
            with self.subTest(token=token):
                code, _, _ = self.call("GET", "/status", token=token)
                self.assertEqual(code, 401)
        for host in ("192.0.2.10", "localhost", None):
            with self.subTest(host=host):
                code, _, _ = self.call("GET", "/status", host=host)
                self.assertEqual(code, 403)
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.call("GET", "/status", host="::1")[0], 200)

    def test_swagger_advertises_bearer_authentication(self):
        schema = self.app.openapi()
        self.assertEqual(schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"], "bearer")
        self.assertEqual(schema["paths"]["/simulation/status"]["get"]["security"], [{"HTTPBearer": []}])

    def test_unavailable_runtime_or_missing_token_is_closed(self):
        self.app.state.sim_runtime = None
        self.assertEqual(self.call("GET", "/status")[0], 503)
        self.app.state.sim_runtime = self.runtime
        self.app.state.sim_api_token = None
        self.assertEqual(self.call("GET", "/status")[0], 503)
        self.assertEqual(self.runtime.calls, [])

    def test_create_prepare_publish_are_dispatched(self):
        cases = [
            ("POST", "/tasks/T1", None, "create_task", ("T1",)),
            ("GET", "/tasks/T1", None, "task_status", ("T1",)),
            ("POST", "/tasks/T1/prepare",
             {"pickup_node": "N5", "candidates": [{"robot_id": "sim-r1", "node": "N1", "available": True}], "action": {"type": "load"}},
             "prepare", ("T1", "N5", [{"robot_id": "sim-r1", "node": "N1", "available": True}], {"type": "load"})),
            ("POST", "/tasks/T1/publish", None, "publish", ("T1",)),
        ]
        for method, path, body, target, args in cases:
            with self.subTest(target=target):
                code, result, headers = self.call(method, path, body=body)
                self.assertEqual((code, result), (200, {"method": target}))
                self.assertEqual(self.runtime.calls[-1], (target, args))
                self.assertEqual(headers.get(b"cache-control"), b"no-store")

    def test_recovery_retry_and_submit_are_dispatched(self):
        complete = {"session": "boot-1", "challenge": "abc", "robot_id": "sim-r1",
                    "order_id": "T1", "seq": 1, "map_id": "M@v1", "stopped_at": "N3",
                    "order_digest": "digest", "stopped": True}
        cases = [
            ("POST", "/recovery/T1/begin", {"session": "boot-1"}, "begin", ("T1", "boot-1")),
            ("POST", "/recovery/T1/complete", complete, "complete",
             ("T1", "boot-1", "abc", "sim-r1", "T1", 1, "M@v1", "N3", "digest", True)),
            ("POST", "/recovery/T1/reset-unallocated", {"session": "boot-1"},
             "reset_unallocated", ("T1", "boot-1")),
            ("GET", "/events/pending", None, "pending_events", ()),
            ("POST", "/events/sim-r1/E1/retry", None, "retry", ("sim-r1", "E1")),
            ("POST", "/orders/T1/2/submit", {"session": "boot-1"}, "submit", ("T1", 2, "boot-1")),
        ]
        for method, path, body, target, args in cases:
            with self.subTest(target=target):
                code, result, _ = self.call(method, path, body=body)
                self.assertEqual((code, result), (200, {"method": target}))
                self.assertEqual(self.runtime.calls[-1], (target, args))

    def test_strict_validation_rejects_coercion_and_unknown_fields(self):
        cases = [
            ("/tasks/T1/prepare", {"pickup_node": "N1", "candidates": [{"robot_id": "sim-r1", "node": "N1", "available": "true"}], "action": {"type": "load"}}),
            ("/tasks/T1/prepare", {"pickup_node": "N1", "candidates": [], "action": {}, "unexpected": 3}),
            ("/recovery/T1/begin", {"session": 1}),
            ("/orders/T1/2/submit", {"session": "boot-1", "unexpected": 1}),
            ("/recovery/T1/complete", {"session": "s", "challenge": "c", "robot_id": "sim-r1", "order_id": "T1", "seq": True, "map_id": "M@v1", "stopped_at": "N1", "order_digest": "d", "stopped": True}),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=body):
                self.assertEqual(self.call("POST", path, body=body)[0], 422)
        self.assertEqual(self.runtime.calls, [])

    def test_errors_do_not_leak_details(self):
        for error, expected in ((ValueError("password"), 409),
                                (RuntimeError("password"), 503)):
            self.runtime.failure = error
            code, result, headers = self.call("POST", "/tasks/T1")
            self.assertEqual(code, expected)
            self.assertNotIn("password", str(result))
            self.assertEqual(headers.get(b"cache-control"), b"no-store")


if __name__ == "__main__":
    unittest.main()
