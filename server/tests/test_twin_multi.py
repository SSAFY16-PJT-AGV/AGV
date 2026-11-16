"""Multi-robot WebSocket observation remains explicit and simulation-only."""
import asyncio
import json
import os
import time
import unittest
from unittest.mock import patch

from server.main import app
from server.mqtt_client import MqttService
from server.tests.test_mqtt_client import FakeClient, message, reason


MAP = '{"sim-r1":"AGV_01","sim-r2":"AGV_02"}'


class TwinMultiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_state = app.state._state.copy()
        self.client = FakeClient()
        self.service = MqttService('localhost', 1883, client=self.client)
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_subscribe(self.client, None, 17, [reason(), reason()], None)
        app.state.mqtt_service = self.service
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.tasks = []
        self.env = patch.dict(os.environ, {
            'AGV_TWIN_WEBSOCKET': '1', 'AGV_TWIN_ROBOT_ID': 'sim-r1',
            'AGV_TWIN_UNREAL_TOKEN': '', 'AGV_TWIN_POSE_MAP_ID': '',
        })
        self.env.start()
        os.environ.pop('AGV_TWIN_ROBOT_MAP', None)

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.env.stop()
        app.state._state.clear()
        app.state._state.update(self.old_state)

    async def connect(self, client=('127.0.0.1', 1234), headers=None, on_accept=None):
        scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws',
                 'path': '/ws/digital-twin', 'raw_path': b'/ws/digital-twin',
                 'query_string': b'', 'root_path': '', 'headers': headers or [],
                 'client': client, 'server': ('127.0.0.1', 8000), 'subprotocols': []}
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        self.incoming, self.outgoing = incoming, outgoing
        await incoming.put({'type': 'websocket.connect'})
        async def send(event):
            if event['type'] == 'websocket.accept' and on_accept is not None:
                on_accept()
            await outgoing.put(event)

        task = asyncio.create_task(app(scope, incoming.get, send))
        self.tasks.append(task)
        return await asyncio.wait_for(outgoing.get(), 1)

    def publish(self, robot, state, retained=False):
        self.client.on_message(self.client, None,
            message('agv/{}/state'.format(robot), state, retained))

    async def receive(self):
        result = await asyncio.wait_for(self.outgoing.get(), 1)
        self.assertEqual(result['type'], 'websocket.send')
        return json.loads(result['text'])['data']

    async def no_message(self):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.outgoing.get(), 0.3)

    async def test_two_robots_are_mapped_and_forwarded_independently(self):
        os.environ['AGV_TWIN_ROBOT_MAP'] = MAP
        state = {'last_node': 'J01', 'status': 'MOVING', 'battery': 0.8}
        self.publish('sim-r1', state)
        self.publish('sim-r2', state)
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        await self.no_message()  # Older samples are not replayed at connection.
        self.publish('sim-r3', state)  # Outside the explicit map.
        self.publish('sim-r1', state, retained=True)
        await self.no_message()

        self.publish('sim-r1', dict(state, last_node='N01'))
        first = await self.receive()
        self.assertEqual((first['agv_id'], first['current_node']), ('AGV_01', 'N01'))
        self.publish('sim-r2', dict(state, last_node='J02'))
        second = await self.receive()
        self.assertEqual((second['agv_id'], second['current_node']), ('AGV_02', 'J02'))
        await self.no_message()  # r1's previous revision must not be sent again.

        self.publish('sim-r1', dict(state, last_node='J03'))
        self.assertEqual((await self.receive())['agv_id'], 'AGV_01')
        await self.no_message()  # r2's previous revision is independent too.

    async def test_pose_mode_maps_both_robots_and_drops_invalid_and_stale(self):
        os.environ['AGV_TWIN_ROBOT_MAP'] = MAP
        os.environ['AGV_TWIN_POSE_MAP_ID'] = 'twin-validation-v1'
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        base = {'last_node': 'J01', 'status': 'MOVING', 'battery': 0.8,
                'map_id': 'twin-validation-v1'}
        self.publish('sim-r1', dict(base, pose={'x': 1500, 'y': 2000, 'th': 0}))
        first = await self.receive()
        self.assertEqual((first['agv_id'], first['x'], first['y'], first['yaw']),
                         ('AGV_01', 1.5, 2.0, 0))
        self.publish('sim-r2', dict(base, pose={'x': 3500, 'y': 4000, 'th': 90}))
        second = await self.receive()
        self.assertEqual((second['agv_id'], second['x'], second['y'], second['yaw']),
                         ('AGV_02', 3.5, 4.0, 90))
        self.publish('sim-r1', dict(base, pose={'x': 1600, 'y': 2000}))
        await self.no_message()  # No node-only fallback for incomplete pose.
        self.publish('sim-r2', dict(base, map_id='other', pose={'x': 1, 'y': 2, 'th': 0}))
        await self.no_message()
        self.publish('sim-r1', dict(base, pose={'x': 1700, 'y': 2000, 'th': 0}))
        with self.service._lock:
            self.service._twin_received_at['sim-r1'] = time.monotonic() - 10
        await self.no_message()

    async def test_simultaneous_new_states_both_arrive_without_duplicates(self):
        os.environ['AGV_TWIN_ROBOT_MAP'] = MAP
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        for node1, node2 in (('J01', 'J02'), ('N01', 'N02')):
            self.publish('sim-r1', {'last_node': node1, 'status': 'MOVING', 'battery': 0.8})
            self.publish('sim-r2', {'last_node': node2, 'status': 'IDLE', 'battery': 0.7})
            received = {item['agv_id']: item for item in (await self.receive(), await self.receive())}
            self.assertEqual(set(received), {'AGV_01', 'AGV_02'})
            self.assertEqual(received['AGV_01']['current_node'], node1)
            self.assertEqual(received['AGV_02']['current_node'], node2)
            await self.no_message()

    async def test_observations_during_accept_are_not_replayed(self):
        os.environ['AGV_TWIN_ROBOT_MAP'] = MAP
        state = {'last_node': 'J01', 'status': 'MOVING', 'battery': 0.8}

        def during_accept():
            self.publish('sim-r1', state)
            self.publish('sim-r2', state)

        self.assertEqual((await self.connect(on_accept=during_accept))['type'],
                         'websocket.accept')
        await self.no_message()
        self.publish('sim-r1', dict(state, last_node='N01'))
        self.publish('sim-r2', dict(state, last_node='N02'))
        received = {item['agv_id']: item for item in (await self.receive(), await self.receive())}
        self.assertEqual(received['AGV_01']['current_node'], 'N01')
        self.assertEqual(received['AGV_02']['current_node'], 'N02')

    async def test_invalid_maps_are_rejected_even_with_valid_single_id(self):
        invalid = ('', '{}', 'not-json', '[]', '{"sim-r1":"AGV_01"}',
                   '{"sim-r1":"AGV_01","sim-r1":"AGV_02"}',
                   '{"sim-r1":"AGV_01","sim-r2":"AGV_01"}',
                   '{"real-r1":"AGV_01","sim-r2":"AGV_02"}',
                   '{"sim-+":"AGV_01","sim-r2":"AGV_02"}',
                   '{"sim-r1":"AGV_01","sim-r2":"AGV/#"}',
                   '{"sim-r1":"AGV_01","sim-r2":"AGV_02","sim-r3":"AGV_03"}')
        for mapping in invalid:
            with self.subTest(mapping=mapping):
                os.environ['AGV_TWIN_ROBOT_MAP'] = mapping
                self.assertEqual((await self.connect())['type'], 'websocket.close')
                await self.tasks[-1]

    async def test_map_does_not_bypass_disabled_loopback_or_origin_guard(self):
        os.environ['AGV_TWIN_ROBOT_MAP'] = MAP
        os.environ['AGV_TWIN_WEBSOCKET'] = '0'
        self.assertEqual((await self.connect())['type'], 'websocket.close')
        await self.tasks[-1]
        os.environ['AGV_TWIN_WEBSOCKET'] = '1'
        self.assertEqual((await self.connect(('192.0.2.1', 1234)))['type'], 'websocket.close')
        await self.tasks[-1]
        self.assertEqual((await self.connect(headers=[(b'origin', b'https://example.com')]))['type'],
                         'websocket.close')
        await self.tasks[-1]
        os.environ['AGV_TWIN_UNREAL_TOKEN'] = 'a' * 32
        headers = [(b'origin', b'http://127.0.0.1'), (b'x-agv-twin-token', b'a' * 32)]
        self.assertEqual((await self.connect(headers=headers))['type'], 'websocket.accept')

    async def test_no_map_keeps_single_robot_behavior(self):
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        state = {'last_node': 'J01', 'status': 'IDLE', 'battery': 0.8}
        self.publish('sim-r2', state)
        await self.no_message()
        self.publish('sim-r1', state)
        self.assertEqual((await self.receive())['agv_id'], 'sim-r1')


if __name__ == '__main__':
    unittest.main()
