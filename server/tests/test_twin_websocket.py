import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.main import app
from server.mqtt_client import MqttService
from server.tests.test_mqtt_client import FakeClient, message, reason


class TwinWebSocketTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_state = app.state._state.copy()
        self.client = FakeClient()
        self.service = MqttService('localhost', 1883, client=self.client)
        self.client.on_connect(self.client, None, None, reason(), None)
        self.client.on_subscribe(self.client, None, 17, [reason(), reason()], None)
        app.state.mqtt_service = self.service
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.task = None
        self.env = patch.dict(os.environ, {'AGV_TWIN_WEBSOCKET': '1', 'AGV_TWIN_ROBOT_ID': 'sim-r1',
                                           'AGV_TWIN_UNREAL_TOKEN': '', 'AGV_TWIN_POSE_MAP_ID': ''})
        self.env.start()

    async def asyncTearDown(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.env.stop()
        app.state._state.clear()
        app.state._state.update(self.old_state)

    async def connect(self, client=('127.0.0.1', 1234), headers=None):
        scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws',
                 'path': '/ws/digital-twin', 'raw_path': b'/ws/digital-twin',
                 'query_string': b'', 'root_path': '', 'headers': headers or [],
                 'client': client, 'server': ('127.0.0.1', 8000), 'subprotocols': []}
        await self.incoming.put({'type': 'websocket.connect'})
        self.task = asyncio.create_task(app(scope, self.incoming.get, self.outgoing.put))
        return await asyncio.wait_for(self.outgoing.get(), 1)

    def publish(self, robot, state, retained=False):
        self.client.on_message(self.client, None, message('agv/{}/state'.format(robot), state, retained))

    async def test_disabled_or_non_loopback_connection_is_rejected(self):
        os.environ['AGV_TWIN_WEBSOCKET'] = '0'
        self.assertEqual((await self.connect())['type'], 'websocket.close')
        await self.task
        os.environ['AGV_TWIN_WEBSOCKET'] = '1'
        self.assertEqual((await self.connect(('192.0.2.10', 1234)))['type'], 'websocket.close')

    async def test_browser_origin_is_rejected(self):
        self.assertEqual((await self.connect(headers=[(b'origin', b'https://example.com')]))['type'],
                         'websocket.close')

    async def test_unreal_origin_with_matching_token_is_accepted(self):
        token = 'a' * 32
        os.environ['AGV_TWIN_UNREAL_TOKEN'] = token
        response = await self.connect(headers=[(b'origin', b'http://127.0.0.1'),
                                               (b'x-agv-twin-token', token.encode('ascii'))])
        self.assertEqual(response['type'], 'websocket.accept')
        self.publish('sim-r1', {'last_node': 'N1', 'status': 'IDLE', 'battery': 0.8})
        message = json.loads((await asyncio.wait_for(self.outgoing.get(), 1))['text'])
        self.assertEqual(message['data']['current_node'], 'N1')

    async def test_unreal_origin_without_configured_token_is_rejected(self):
        response = await self.connect(headers=[(b'origin', b'http://127.0.0.1'),
                                               (b'x-agv-twin-token', b'a' * 32)])
        self.assertEqual(response['type'], 'websocket.close')

    async def test_short_configured_token_is_rejected(self):
        os.environ['AGV_TWIN_UNREAL_TOKEN'] = 'short'
        response = await self.connect(headers=[(b'origin', b'http://127.0.0.1'),
                                               (b'x-agv-twin-token', b'short')])
        self.assertEqual(response['type'], 'websocket.close')

    async def test_unreal_origin_without_supplied_token_is_rejected(self):
        os.environ['AGV_TWIN_UNREAL_TOKEN'] = 'a' * 32
        response = await self.connect(headers=[(b'origin', b'http://127.0.0.1')])
        self.assertEqual(response['type'], 'websocket.close')

    async def test_unreal_origin_with_incorrect_token_is_rejected(self):
        os.environ['AGV_TWIN_UNREAL_TOKEN'] = 'a' * 32
        response = await self.connect(headers=[(b'origin', b'http://127.0.0.1'),
                                               (b'x-agv-twin-token', b'b' * 32)])
        self.assertEqual(response['type'], 'websocket.close')

    async def test_authenticated_wrong_origin_is_rejected(self):
        os.environ['AGV_TWIN_UNREAL_TOKEN'] = 'a' * 32
        response = await self.connect(headers=[(b'origin', b'https://example.com'),
                                               (b'x-agv-twin-token', b'a' * 32)])
        self.assertEqual(response['type'], 'websocket.close')

    async def test_duplicate_origins_are_rejected_even_with_token(self):
        os.environ['AGV_TWIN_UNREAL_TOKEN'] = 'a' * 32
        response = await self.connect(headers=[(b'origin', b'http://127.0.0.1'),
                                               (b'origin', b'http://127.0.0.1'),
                                               (b'x-agv-twin-token', b'a' * 32)])
        self.assertEqual(response['type'], 'websocket.close')

    async def test_duplicate_tokens_are_rejected_even_when_identical(self):
        os.environ['AGV_TWIN_UNREAL_TOKEN'] = 'a' * 32
        response = await self.connect(headers=[(b'origin', b'http://127.0.0.1'),
                                               (b'x-agv-twin-token', b'a' * 32),
                                               (b'x-agv-twin-token', b'a' * 32)])
        self.assertEqual(response['type'], 'websocket.close')

    async def test_authenticated_unreal_request_cannot_bypass_remote_or_disabled_guard(self):
        os.environ['AGV_TWIN_UNREAL_TOKEN'] = 'a' * 32
        headers = [(b'origin', b'http://127.0.0.1'), (b'x-agv-twin-token', b'a' * 32)]
        self.assertEqual((await self.connect(client=('192.0.2.10', 1234), headers=headers))['type'],
                         'websocket.close')
        await self.task
        os.environ['AGV_TWIN_WEBSOCKET'] = '0'
        self.assertEqual((await self.connect(headers=headers))['type'], 'websocket.close')

    async def test_only_new_live_states_for_selected_robot_are_forwarded(self):
        state = {'last_node': 'N1', 'status': 'IDLE', 'battery': 0.8,
                 'pose': {'x': 100, 'y': 200, 'th': 90}}
        self.publish('sim-r1', state)
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        self.publish('sim-r2', state)
        self.publish('sim-r1', state, retained=True)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.outgoing.get(), 0.3)
        self.publish('sim-r1', state)
        sent = await asyncio.wait_for(self.outgoing.get(), 1)
        self.assertEqual(json.loads(sent['text']), {'type': 'robot.state', 'data': {
            'agv_id': 'sim-r1', 'current_node': 'N1', 'status': 'IDLE', 'battery': 0.8}})
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.outgoing.get(), 0.3)
        self.publish('sim-r1', state)
        self.assertEqual(json.loads((await asyncio.wait_for(self.outgoing.get(), 1))['text'])['type'], 'robot.state')

    async def test_pose_mode_forwards_valid_same_map_pose_in_meters(self):
        os.environ['AGV_TWIN_POSE_MAP_ID'] = 'twin-validation-v1'
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        self.publish('sim-r1', {'last_node': 'J01', 'status': 'MOVING', 'battery': 0.8,
                                'map_id': 'twin-validation-v1',
                                'pose': {'x': 1500, 'y': 2000, 'th': 90, 'src': 'interp'}})
        sent = json.loads((await asyncio.wait_for(self.outgoing.get(), 1))['text'])
        self.assertEqual(sent, {'type': 'robot.state', 'data': {
            'agv_id': 'sim-r1', 'current_node': 'J01', 'status': 'MOVING',
            'battery': 0.8, 'x': 1.5, 'y': 2.0, 'yaw': 90}})

    async def test_pose_mode_rejects_wrong_or_missing_map_and_invalid_pose(self):
        os.environ['AGV_TWIN_POSE_MAP_ID'] = 'twin-validation-v1'
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        valid_state = {'last_node': 'J01', 'status': 'MOVING', 'battery': 0.8,
                       'map_id': 'twin-validation-v1',
                       'pose': {'x': 1500, 'y': 2000, 'th': 90}}
        cases = [
            dict(valid_state, map_id='different-map'),
            {key: value for key, value in valid_state.items() if key != 'map_id'},
            {key: value for key, value in valid_state.items() if key != 'pose'},
            dict(valid_state, pose=None),
            dict(valid_state, pose={'x': 1500, 'y': 2000}),
            dict(valid_state, pose={'x': True, 'y': 2000, 'th': 90}),
            dict(valid_state, pose={'x': '1500', 'y': 2000, 'th': 90}),
            dict(valid_state, pose={'x': float('nan'), 'y': 2000, 'th': 90}),
            dict(valid_state, pose={'x': 1500, 'y': float('inf'), 'th': 90}),
            dict(valid_state, pose={'x': 10 ** 100, 'y': 2000, 'th': 90}),
        ]
        for state in cases:
            with self.subTest(state=state):
                self.publish('sim-r1', state)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(self.outgoing.get(), 0.25)

    async def test_pose_mode_off_keeps_node_only_message(self):
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        self.publish('sim-r1', {'last_node': 'J01', 'status': 'MOVING', 'battery': 0.8,
                                'map_id': 'twin-validation-v1',
                                'pose': {'x': 1500, 'y': 2000, 'th': 90}})
        sent = json.loads((await asyncio.wait_for(self.outgoing.get(), 1))['text'])
        self.assertEqual(sent, {'type': 'robot.state', 'data': {
            'agv_id': 'sim-r1', 'current_node': 'J01', 'status': 'MOVING', 'battery': 0.8}})

    async def test_unknown_pose_map_configuration_does_not_fall_back_to_nodes(self):
        os.environ['AGV_TWIN_POSE_MAP_ID'] = 'unapproved-map'
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        self.publish('sim-r1', {'last_node': 'J01', 'status': 'MOVING', 'battery': 0.8,
                                'map_id': 'unapproved-map',
                                'pose': {'x': 1500, 'y': 2000, 'th': 0}})
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.outgoing.get(), 0.3)

    async def test_disconnect_stops_forwarding_until_a_new_live_message(self):
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        self.client.on_disconnect(self.client, None, None, reason(), None)
        self.publish('sim-r1', {'last_node': 'N2', 'status': 'IDLE', 'battery': 1})
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.outgoing.get(), 0.3)

    async def test_very_large_battery_does_not_break_stream(self):
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        self.publish('sim-r1', {'last_node': 'N2', 'status': 'IDLE', 'battery': 10 ** 400})
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.outgoing.get(), 0.3)
        self.publish('sim-r1', {'last_node': 'N3', 'status': 'IDLE', 'battery': 0.8})
        sent = json.loads((await asyncio.wait_for(self.outgoing.get(), 1))['text'])
        self.assertEqual(sent['data']['current_node'], 'N3')

    async def test_incomplete_state_does_not_fabricate_twin_defaults(self):
        self.assertEqual((await self.connect())['type'], 'websocket.accept')
        self.publish('sim-r1', {'last_node': 'N2'})
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.outgoing.get(), 0.3)


if __name__ == '__main__':
    unittest.main()
