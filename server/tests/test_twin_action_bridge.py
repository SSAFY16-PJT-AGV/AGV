import asyncio
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from server.twin_websocket import router


class Service:
    def twin_observation(self, robot):
        return {'revision': 0}


class Actions:
    def __init__(self):
        self.claims, self.received, self.detached = [], [], []
        self.sent = False

    def attach(self, client, trusted):
        self.claims.append(trusted)
        return trusted

    def poll(self, client, now):
        if self.sent:
            return None
        self.sent = True
        return {'type': 'robot.action', 'data': {'action_id': 'a1'}}

    def receive(self, client, payload):
        self.received.append(payload)

    def detach(self, client):
        self.detached.append(client)


class TwinActionBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def _test_slow_action_does_not_block_http(self, method):
        entered, release = threading.Event(), threading.Event()
        timer = threading.Timer(2.0, release.set)
        timer.start()

        class SlowActions(Actions):
            def poll(self, client, now):
                if method == 'poll':
                    entered.set()
                    release.wait(3)
                    return None
                return super().poll(client, now)

            def receive(self, client, payload):
                if method == 'receive':
                    entered.set()
                    release.wait(3)
                return super().receive(client, payload)

        token = 'A' * 32
        app = FastAPI()
        app.include_router(router)
        app.state.mqtt_service = Service()
        app.state.demo_actions = SlowActions()

        @app.get('/probe')
        async def probe():
            return {'ready': True}

        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        ws_scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws',
                    'path': '/ws/digital-twin', 'raw_path': b'/ws/digital-twin',
                    'query_string': b'', 'root_path': '',
                    'headers': [(b'origin', b'http://127.0.0.1'),
                                (b'x-agv-twin-token', token.encode())],
                    'client': ('127.0.0.1', 1111), 'server': ('127.0.0.1', 8000), 'subprotocols': []}
        http_scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                      'scheme': 'http', 'method': 'GET', 'path': '/probe', 'raw_path': b'/probe',
                      'query_string': b'', 'root_path': '', 'headers': [],
                      'client': ('127.0.0.1', 1112), 'server': ('127.0.0.1', 8000)}
        await incoming.put({'type': 'websocket.connect'})
        with patch.dict(os.environ, {'AGV_TWIN_WEBSOCKET': '1', 'AGV_TWIN_ROBOT_ID': 'sim-r1',
                                     'AGV_TWIN_UNREAL_TOKEN': token}, clear=False):
            os.environ.pop('AGV_TWIN_ROBOT_MAP', None)
            task = asyncio.create_task(app(ws_scope, incoming.get, outgoing.put))
            try:
                self.assertEqual((await asyncio.wait_for(outgoing.get(), 1))['type'], 'websocket.accept')
                if method == 'receive':
                    self.assertEqual(json.loads((await asyncio.wait_for(outgoing.get(), 1))['text'])['type'], 'robot.action')
                    await incoming.put({'type': 'websocket.receive', 'text': json.dumps({'type': 'robot.action.result'})})
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                # On the old implementation the ASGI loop itself is blocked;
                # the timer releases the action before this coroutine resumes.
                self.assertFalse(release.is_set(), 'action blocked the ASGI event loop')
                http_messages = []
                async def http_receive():
                    return {'type': 'http.request', 'body': b'', 'more_body': False}
                async def http_send(message):
                    http_messages.append(message)
                await asyncio.wait_for(app(http_scope, http_receive, http_send), .5)
                self.assertEqual(http_messages[0]['status'], 200)
                self.assertFalse(release.is_set(), 'HTTP waited for the slow action')
            finally:
                release.set()
                await incoming.put({'type': 'websocket.disconnect', 'code': 1000})
                await asyncio.wait_for(task, 4)
                timer.cancel()

    async def test_slow_action_poll_does_not_block_other_http(self):
        await self._test_slow_action_does_not_block_http('poll')

    async def test_slow_action_receive_does_not_block_other_http(self):
        await self._test_slow_action_does_not_block_http('receive')

    async def _test_cancel_waits_for_action_and_detaches(self, blocked):
        entered, release = threading.Event(), threading.Event()
        invalidated = threading.Event()

        class BlockingActions(Actions):
            def invalidate_owner(self, client):
                invalidated.set()

            def attach(self, client, trusted):
                if blocked == 'attach':
                    entered.set()
                    release.wait(3)
                return super().attach(client, trusted)

            def poll(self, client, now):
                if blocked == 'poll':
                    entered.set()
                    release.wait(3)
                return super().poll(client, now)

            def receive(self, client, payload):
                if blocked == 'receive':
                    entered.set()
                    release.wait(3)
                return super().receive(client, payload)

        token = 'A' * 32
        app = FastAPI()
        app.include_router(router)
        app.state.mqtt_service = Service()
        actions = app.state.demo_actions = BlockingActions()
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws',
                 'path': '/ws/digital-twin', 'raw_path': b'/ws/digital-twin',
                 'query_string': b'', 'root_path': '',
                 'headers': [(b'origin', b'http://127.0.0.1'),
                             (b'x-agv-twin-token', token.encode())],
                 'client': ('127.0.0.1', 1111), 'server': ('127.0.0.1', 8000), 'subprotocols': []}
        await incoming.put({'type': 'websocket.connect'})
        with patch.dict(os.environ, {'AGV_TWIN_WEBSOCKET': '1', 'AGV_TWIN_ROBOT_ID': 'sim-r1',
                                     'AGV_TWIN_UNREAL_TOKEN': token}, clear=False):
            os.environ.pop('AGV_TWIN_ROBOT_MAP', None)
            task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
            try:
                self.assertEqual((await asyncio.wait_for(outgoing.get(), 1))['type'], 'websocket.accept')
                if blocked == 'receive':
                    await asyncio.wait_for(outgoing.get(), 1)
                    await incoming.put({'type': 'websocket.receive', 'text': json.dumps({'type': 'robot.action.result'})})
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), 'in-flight action must not be abandoned')
                if blocked != 'attach':
                    self.assertTrue(invalidated.is_set(), 'cancelled owner must become unavailable immediately')
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertEqual(actions.claims, [True])
                self.assertEqual(len(actions.detached), 1)
                self.assertTrue(invalidated.is_set())
                self.assertTrue(outgoing.empty(), 'cancelled action must not be sent')
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_during_attach_does_not_leak_owner(self):
        await self._test_cancel_waits_for_action_and_detaches('attach')

    async def test_cancel_during_poll_detaches_before_return(self):
        await self._test_cancel_waits_for_action_and_detaches('poll')

    async def test_cancel_during_receive_invalidates_before_waiting(self):
        await self._test_cancel_waits_for_action_and_detaches('receive')

    async def test_authenticated_unreal_receives_actions_and_returns_results(self):
        token = 'A' * 32
        app = FastAPI()
        app.include_router(router)
        app.state.mqtt_service = Service()
        app.state.demo_actions = Actions()
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws',
                 'path': '/ws/digital-twin', 'raw_path': b'/ws/digital-twin',
                 'query_string': b'', 'root_path': '',
                 'headers': [(b'origin', b'http://127.0.0.1'), (b'x-agv-twin-token', token.encode())],
                 'client': ('127.0.0.1', 1111), 'server': ('127.0.0.1', 8000), 'subprotocols': []}
        await incoming.put({'type': 'websocket.connect'})
        with patch.dict(os.environ, {'AGV_TWIN_WEBSOCKET': '1', 'AGV_TWIN_ROBOT_ID': 'sim-r1',
                                     'AGV_TWIN_UNREAL_TOKEN': token}, clear=False):
            os.environ.pop('AGV_TWIN_ROBOT_MAP', None)
            task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
            try:
                self.assertEqual((await asyncio.wait_for(outgoing.get(), 2))['type'], 'websocket.accept')
                sent = await asyncio.wait_for(outgoing.get(), 2)
                self.assertEqual(json.loads(sent['text'])['type'], 'robot.action')
                ack = {'type': 'robot.action.result', 'data': {'action_id': 'a1', 'applied': True}}
                await incoming.put({'type': 'websocket.receive', 'text': json.dumps(ack)})
                await incoming.put({'type': 'websocket.disconnect', 'code': 1000})
                await asyncio.wait_for(task, 2)
                self.assertEqual(app.state.demo_actions.claims, [True])
                self.assertEqual(app.state.demo_actions.received, [ack])
                self.assertEqual(len(app.state.demo_actions.detached), 1)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
