import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from agv.link.controller import LinkController
from agv.link.mqtt import FakeLink, MqttLink


def _payload():
    return json.dumps({'order_id': 'T1', 'seq': 1, 'map_id': 'm@v1',
                       'nodes': ['A', 'B'], 'released_until': 'B',
                       'action_at_end': {'type': 'none'}}).encode('utf8')


class Runner:
    def __init__(self):
        self.finished = False
        self.finish_reason = None
        self.state = 'hold'
        self.hold_reason = 'wait_order'
        self.odo = 0
        self.resume_count = 0

    def hold(self, reason):
        self.state, self.hold_reason = 'hold', reason
        return True

    def resume(self):
        self.state = 'follow'
        self.resume_count += 1
        return True

    def policy_changed(self):
        pass


def _controller(link, on_route=None):
    nav = SimpleNamespace(m=SimpleNamespace(map_id='m', version=1), at_node='A',
                          hold_at_end=False, set_route=on_route or (lambda *a: None))
    raw = {'nodes': [{'id': 'A', 'pos': [0, 0]}, {'id': 'B', 'pos': [1, 0]}],
           'edges': [], 'zones': []}
    ctl = LinkController(link, nav, raw, start_node='A', clock=lambda: 5)
    runner = Runner()
    ctl.attach(runner)
    ctl.state = lambda now=None: {'status': 'IDLE'}
    return ctl, runner


class DisconnectFenceTests(unittest.TestCase):
    def test_disconnected_queued_order_does_not_resume(self):
        link = FakeLink('physical-1')
        ctl, runner = _controller(link)
        link.send(_payload())
        link.connected, link.disconnected_at = False, 0
        ctl.step(now=5)
        self.assertEqual(runner.resume_count, 0)
        self.assertIsNone(ctl.book.current)

    def test_mqtt_discards_queued_order_on_disconnect_reconnect(self):
        link = MqttLink('physical-1')
        client = MagicMock()
        link._on_connect(client, None, None, 0)
        link._on_message(client, None, SimpleNamespace(topic='agv/physical-1/order',
                                                       retain=False, payload=_payload()))
        link._on_disconnect(client, None, 0)
        link._on_connect(client, None, None, 0)
        self.assertEqual(link.poll(), [])

    def test_disconnection_during_route_setup_does_not_resume(self):
        link = MqttLink('physical-1')
        client = MagicMock()
        link._on_connect(client, None, None, 0)
        def race(*args):
            link._on_disconnect(client, None, 0)
            link._on_connect(client, None, None, 0)
        ctl, runner = _controller(link, race)
        link._on_message(client, None, SimpleNamespace(topic='agv/physical-1/order',
                                                       retain=False, payload=_payload()))
        ctl.step(now=5)
        self.assertEqual(runner.resume_count, 0)
        self.assertIsNone(ctl.book.current)

    def test_dequeued_order_rejected_after_generation_changes(self):
        link = MqttLink('physical-1')
        client = MagicMock()
        link._on_connect(client, None, None, 0)
        link._on_message(client, None, SimpleNamespace(topic='agv/physical-1/order',
                                                       retain=False, payload=_payload()))
        generation, payload = link.poll_with_generation()[0]
        link._on_disconnect(client, None, 0)
        link._on_connect(client, None, None, 0)
        handled = []
        self.assertFalse(link.process_if_current(generation, payload, handled.append))
        self.assertEqual(handled, [])
        link._on_message(client, None, SimpleNamespace(topic='agv/physical-1/order',
                                                       retain=False, payload=_payload()))
        self.assertEqual(link.poll(), [])  # no implicit recovery after reconnect

    def test_callback_can_signal_loss_while_control_holds_connection_lock(self):
        link = MqttLink('physical-1')
        client = MagicMock()
        link._on_connect(client, None, None, 0)
        callbacks = []
        def route(*args):
            t = threading.Thread(target=link._on_disconnect, args=(client, None, 0))
            callbacks.append(t)
            t.start()
            until = time.monotonic() + 2
            while not link.disconnect_pending() and time.monotonic() < until:
                time.sleep(.001)
            self.assertTrue(link.disconnect_pending())
        ctl, runner = _controller(link, route)
        link._on_message(client, None, SimpleNamespace(topic='agv/physical-1/order',
                                                       retain=False, payload=_payload()))
        ctl.step(now=5)
        callbacks[0].join(2)
        self.assertFalse(callbacks[0].is_alive())
        self.assertEqual(runner.resume_count, 0)
        self.assertIsNone(ctl.book.current)

    def test_unstarted_order_has_no_first_motor_output_after_disconnect(self):
        from agv.runner import ExploreRunner
        runner = ExploreRunner.__new__(ExploreRunner)
        runner.finished, runner.state, runner.mode = False, 'follow', 'passive'
        runner.stall = SimpleNamespace(stalled=False)
        runner._stall_logged, runner._sent = False, (0.0, 0.0)
        runner.search_side, runner.lost_s = None, 0
        runner._step = lambda frame, dt: (0.5, 0.5)
        runner._write_trace = lambda: None
        cancelled = []
        runner.cancel_unstarted_motion = cancelled.append
        getattr(runner, 'arm_unstarted_disconnect_guard', lambda guard: None)(lambda: True)
        self.assertEqual(runner.step(None, 0.1), (0.0, 0.0))
        self.assertEqual(cancelled, ['link_lost'])


if __name__ == '__main__':
    unittest.main()
