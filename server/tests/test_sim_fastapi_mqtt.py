"""FastAPI lifecycle and HTTP integration against a private MQTT broker.

The test starts its own broker and fake robot; it never opens robot hardware.
"""
import asyncio
import json
import os
import queue
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server.main import app
from server.allocation_store import AllocationStore
from server.segments import plan_segment
from server.tests import test_fleet_mqtt_integration as fixture
from server.tests.test_segment_recovery_mqtt import two_segment_map
from sim.order_inbox import SimOrderInbox


TEST_TOKEN = 'integration-token-1234567890-abcdef'


async def request(method, path, payload=None, token=TEST_TOKEN):
    body = b'' if payload is None else json.dumps(payload).encode('utf-8')
    headers = [(b'authorization', ('Bearer ' + token).encode())] if token else []
    if payload is not None:
        headers.append((b'content-type', b'application/json'))
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'root_path': '', 'headers': headers,
             'client': ('127.0.0.1', 1234), 'server': ('test', 80)}
    output = []
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {'type': 'http.disconnect'}
        sent = True
        return {'type': 'http.request', 'body': body, 'more_body': False}
    async def send(message):
        output.append(message)
    await app(scope, receive, send)
    status = next(item['status'] for item in output if item['type'] == 'http.response.start')
    data = b''.join(item.get('body', b'') for item in output if item['type'] == 'http.response.body')
    return status, json.loads(data) if data else None


class FakeObservations:
    def snapshot(self):
        return {'connected': False, 'ready': False, 'robots': {}}


class DisabledSimulationApiTest(unittest.TestCase):
    def test_simulation_disabled_without_configuration(self):
        async def scenario():
            with patch.dict(os.environ, {}, clear=True), patch('server.main.start_mqtt', return_value=FakeObservations()), \
                    patch('server.main.stop_mqtt'):
                async with app.router.lifespan_context(app):
                    status, _ = await request('GET', '/simulation/status')
                    self.assertIn(status, (403, 404, 503))
        asyncio.run(scenario())


@unittest.skipUnless(os.environ.get('MOSQUITTO_EXE'), 'set MOSQUITTO_EXE for isolated MQTT tests')
class SimulationHttpMqttTest(unittest.TestCase):
    setUp = fixture.FleetMqttIntegrationTest.setUp
    new_client = fixture.FleetMqttIntegrationTest.new_client
    connect = fixture.FleetMqttIntegrationTest.connect

    def config_and_robot(self):
        data = two_segment_map()
        folder = Path(self.tmp.name)
        map_path = folder / 'map.json'
        map_path.write_text(json.dumps(data), encoding='utf-8')
        config_path = folder / 'sim.json'
        config_path.write_text(json.dumps({'database': str(self.path), 'map_file': str(map_path),
            'robots': {'sim-r1': {'T1': 'boot-1'}}, 'mqtt_port': self.port,
            'mqtt_client_id': 'http-integration-observer'}), encoding='utf-8')
        inbox = SimOrderInbox(folder / 'robot.sqlite3', 'sim-r1', 'integration@v1')
        inbox.initialize()
        messages = queue.Queue()
        subscribed = threading.Event()
        robot = self.new_client()
        def receive(client, userdata, message):
            try:
                messages.put(inbox.accept(message.payload))
            except Exception as error:
                messages.put(error)
        robot.on_message = receive
        robot.on_subscribe = lambda client, userdata, mid, codes, properties: subscribed.set()
        self.connect(robot, lambda client: client.subscribe('agv/sim-r1/order', qos=1))
        self.assertTrue(subscribed.wait(5))
        return data, config_path, inbox, messages, robot

    async def ok(self, method, path, data=None):
        status, result = await request(method, '/simulation' + path, data)
        self.assertIn(status, (200, 201, 202), (method, path, status, result))
        return result

    async def setup_order(self, messages):
        await self.ok('POST', '/tasks/T1')
        await self.ok('POST', '/tasks/T1/prepare', {'pickup_node': 'N5',
            'candidates': [{'robot_id': 'sim-r1', 'node': 'N1', 'available': True}],
            'action': {'type': 'load'}})
        await self.ok('POST', '/tasks/T1/publish')
        receipt = messages.get(timeout=5)
        self.assertFalse(isinstance(receipt, Exception), repr(receipt))
        self.assertTrue(receipt.new)
        self.assertEqual(receipt.seq, 1)
        return receipt

    async def prove(self, seq, digest, stopped_at):
        started = await self.ok('POST', '/recovery/T1/begin', {'session': 'boot-1'})
        await self.ok('POST', '/recovery/T1/complete', {'session': 'boot-1',
            'challenge': started['challenge'], 'robot_id': 'sim-r1', 'order_id': 'T1',
            'seq': seq, 'map_id': 'integration@v1', 'stopped_at': stopped_at,
            'order_digest': digest, 'stopped': True})

    def send_event(self, robot, event):
        info = robot.publish('agv/sim-r1/event', json.dumps(event).encode(), qos=1, retain=False)
        info.wait_for_publish(timeout=5)
        self.assertTrue(info.is_published())

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail('timed out waiting for integration result')

    def test_http_lifecycle_events_progress_and_duplicate_is_not_republished(self):
        data, config, inbox, messages, robot = self.config_and_robot()
        old = set(plan_segment(data, ['N1', 'N2', 'N3', 'N4', 'N5']).resources)
        following = set(plan_segment(data, ['N3', 'N4', 'N5']).resources)
        event = {'type': 'rear_clear', 'event_id': 'event-1', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'integration@v1', 'stopped_at': 'N3',
                 'cleared_resources': [list(item) for item in sorted(old - following)]}
        async def scenario():
            with patch.dict(os.environ, {'AGV_SIM_CONFIG': str(config), 'AGV_SIM_API_TOKEN': TEST_TOKEN}), \
                    patch('server.main.start_mqtt', return_value=FakeObservations()), patch('server.main.stop_mqtt'):
                async with app.router.lifespan_context(app):
                    status, _ = await request('POST', '/simulation/tasks/T1', token='wrong')
                    self.assertIn(status, (401, 403))
                    first = await self.setup_order(messages)
                    inbox.mark_completed('T1', 1, first.digest, 'N3', rear_clear_confirmed=True)
                    await self.prove(1, first.digest, 'N3')
                    self.send_event(robot, event)
                    second = messages.get(timeout=5)
                    self.assertFalse(isinstance(second, Exception), repr(second))
                    self.assertTrue(second.new)
                    self.assertEqual(second.seq, 2)
                    self.wait_until(lambda: set(AllocationStore(self.path).list_occupancy()) == following)
                    self.send_event(robot, event)
                    with self.assertRaises(queue.Empty):
                        messages.get(timeout=0.4)
        asyncio.run(scenario())

    def test_restart_requires_recovery_before_retry_and_explicit_submission(self):
        data, config, inbox, messages, robot = self.config_and_robot()
        old = set(plan_segment(data, ['N1', 'N2', 'N3', 'N4', 'N5']).resources)
        following = set(plan_segment(data, ['N3', 'N4', 'N5']).resources)
        event = {'type': 'rear_clear', 'event_id': 'restart-event', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'integration@v1', 'stopped_at': 'N3',
                 'cleared_resources': [list(item) for item in sorted(old - following)]}
        async def scenario():
            with patch.dict(os.environ, {'AGV_SIM_CONFIG': str(config), 'AGV_SIM_API_TOKEN': TEST_TOKEN}), \
                    patch('server.main.start_mqtt', return_value=FakeObservations()), patch('server.main.stop_mqtt'):
                async with app.router.lifespan_context(app):
                    first = await self.setup_order(messages)
                    inbox.mark_completed('T1', 1, first.digest, 'N3', rear_clear_confirmed=True)
                    # No recovery proof: receiving cannot release the old segment.
                    self.send_event(robot, event)
                    for _ in range(100):
                        pending = await self.ok('GET', '/events/pending')
                        if pending['events']:
                            break
                        await asyncio.sleep(0.05)
                    self.assertEqual(pending['events'], [
                        {'robot_id': 'sim-r1', 'event_id': 'restart-event', 'task_id': 'T1', 'seq': 1}])
                    self.assertEqual(set(AllocationStore(self.path).list_occupancy()), old)
                # New lifespan has a new epoch. Stored event and old reservations persist.
                async with app.router.lifespan_context(app):
                    pending = await self.ok('GET', '/events/pending')
                    self.assertEqual(pending['events'][0]['event_id'], 'restart-event')
                    status, _ = await request('POST', '/simulation/events/sim-r1/restart-event/retry')
                    self.assertGreaterEqual(status, 400)
                    self.assertEqual(set(AllocationStore(self.path).list_occupancy()), old)
                    await self.prove(1, first.digest, 'N3')
                    await self.ok('POST', '/events/sim-r1/restart-event/retry')
                    self.assertEqual(set(AllocationStore(self.path).list_occupancy()), following)
                    task = await self.ok('GET', '/tasks/T1')
                    self.assertEqual(task['orders'], [{'seq': 1, 'state': 'acked'},
                                                       {'seq': 2, 'state': 'pending'}])
                    self.assertEqual({tuple(resource) for resource in task['occupancy']}, following)
                    with self.assertRaises(queue.Empty):
                        messages.get(timeout=0.3)
                    await self.ok('POST', '/orders/T1/2/submit', {'session': 'boot-1'})
                    second = messages.get(timeout=5)
                    self.assertFalse(isinstance(second, Exception), repr(second))
                    self.assertTrue(second.new)
                    self.assertEqual(second.seq, 2)
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
