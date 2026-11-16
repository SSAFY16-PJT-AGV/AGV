import asyncio
import json
import os
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
