import json
import unittest
from types import SimpleNamespace

from demo import DemoRobot, DemoServer


class FakeClient:
    def __init__(self):
        self.publications = []

    def publish(self, topic, payload, qos, retain):
        self.publications.append((topic, json.loads(payload), qos, retain))


class DemoTest(unittest.TestCase):
    def test_state_from_fake_robot_triggers_one_documented_order(self):
        server = DemoServer('demo-robot1')
        client = FakeClient()
        message = SimpleNamespace(payload=json.dumps({
            'robot_id': 'demo-robot1', 'status': 'idle', 'simulated': True
        }).encode(), retain=False)
        server.on_message(client, None, message)
        server.on_message(client, None, message)
        self.assertEqual(len(client.publications), 1)
        topic, order, qos, retained = client.publications[0]
        self.assertEqual((topic, qos, retained), ('agv/demo-robot1/order', 1, False))
        self.assertEqual(order['released_until'], 'N4')
        self.assertEqual(order['nodes'], ['N3', 'N4'])
        self.assertEqual(set(order), {'order_id', 'seq', 'map_id', 'nodes',
                                      'released_until', 'action_at_end'})

    def test_fake_robot_reports_order_without_executing_motion(self):
        robot = DemoRobot('demo-robot1')
        client = FakeClient()
        message = SimpleNamespace(payload=json.dumps({'order_id': 'demo-001'}).encode())
        robot.on_message(client, None, message)
        self.assertEqual(client.publications, [(
            'agv/demo-robot1/state',
            {'robot_id': 'demo-robot1', 'status': 'order_received',
             'order_id': 'demo-001', 'simulated': True}, 0, False)])

    def test_invalid_or_retained_states_do_not_trigger_order(self):
        server = DemoServer('demo-robot1')
        client = FakeClient()
        for payload, retained in [(b'{', False), (b'{}', False),
                                  (b'{"robot_id":"demo-robot1","status":"idle","simulated":true}', True)]:
            server.on_message(client, None, SimpleNamespace(payload=payload, retain=retained))
        self.assertEqual(client.publications, [])


if __name__ == '__main__':
    unittest.main()
