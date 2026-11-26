"""Protocol checks for the isolated Unreal guard probe."""
import json
import unittest

from server.tools.v3_unreal_guard_probe import _action, _snapshot


class FakeSocket:
    def __init__(self):
        self.sent = []

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout):
        last = self.sent[-1]
        if last['type'] == 'robot.action':
            data = last['data']
            return json.dumps({'type': 'robot.action.result', 'data': {
                'order_id': data['order_id'], 'action_id': data['action_id'],
                'agv_id': data['agv_id'], 'action': data['action'], 'applied': False}})
        return json.dumps({'type': 'shelf.snapshot', 'data': {
            'request_id': last['data']['request_id'], 'shelves': [], 'robots': [], 'pickups': []}})


class UnrealGuardProbeTest(unittest.TestCase):
    def test_wrong_position_place_is_correlated_and_receives_false(self):
        ws = FakeSocket()
        reply = _action(ws, 'place', 'D04', {'D04': (1.0, .5)},
                        shelf='SHELF_RED', wrong_position=True)
        self.assertFalse(reply['applied'])
        message = ws.sent[0]
        self.assertEqual(message['data']['expected_x'], 6.0)
        self.assertEqual(message['data']['shelf_id'], 'SHELF_RED')
        self.assertEqual(message['data']['slot_index'], 0)

    def test_snapshot_requests_both_shelves_with_correlated_response(self):
        ws = FakeSocket()
        reply = _snapshot(ws)
        self.assertEqual(reply['request_id'], ws.sent[0]['data']['request_id'])
        self.assertEqual(ws.sent[0]['data']['shelf_ids'], ['SHELF_RED', 'SHELF_BLUE'])


if __name__ == '__main__':
    unittest.main()
