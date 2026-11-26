# -*- coding: utf-8 -*-
"""Actual controller and MQTT callbacks stay in hold during reconciliation."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agv.link.mqtt import MqttLink
from agv.tests.test_link_disconnect_fence import _controller, _payload
from agv.tests.test_reconcile_robot import KEY, sign


class ReconcileLinkTests(unittest.TestCase):
    def setUp(self):
        self.link = MqttLink('physical-1', reconcile_isolated=True, reconcile_key=KEY)
        self.client = MagicMock()
        self.client.subscribe.return_value = (0, 17)
        self.client.publish.return_value.rc = 0
        self.link.c = self.client
        self.responses = []
        self.link.publish_reconcile_once = lambda response, _g, _s: self.responses.append(response) or True
        self.link._on_connect(self.client, None, None, 0)
        self.link._on_subscribe(self.client, None, 17, [1, 1])
        self.event_sends = []
        self.link.publish_event_once = lambda event: self.event_sends.append(event) or True
        self.ctl, self.runner = _controller(self.link)
        self.ctl._handle(_payload())
        self.runner.state, self.runner.hold_reason = 'hold', 'nav_hold'
        self.ctl.nav.at_node = 'B'
        self.ctl._watch_stop()
        self.assertIsNotNone(self.ctl._reconcile_event)
        self.link._on_disconnect(self.client, None, 1)
        self.link._on_connect(self.client, None, None, 0)
        self.link._on_subscribe(self.client, None, 17, [1, 1])

    def msg(self, kind, payload, retain=False, qos=1):
        self.link._on_message(self.client, None, SimpleNamespace(
            topic='agv/physical-1/reconcile/' + kind, retain=retain, qos=qos,
            payload=json.dumps(payload).encode('utf8')))

    def test_signed_probe_and_commit_never_clear_order_or_motion_latch(self):
        b, c = self.ctl.book, self.ctl
        probe = sign({'v': 1, 'kind': 'probe', 'nonce': 'a' * 64, 'server_epoch': 'b' * 64,
                      'robot_id': 'physical-1', 'robot_session': c.session, 'order_id': 'T1',
                      'seq': 1, 'order_digest': b.current_digest, 'map_id': 'm@v1',
                      'release_index': b.release_i, 'stopped_at': 'B', 'event_id': None,
                      'event_digest': None})
        self.msg('request', probe)
        c.step(now=5)
        self.assertEqual(len(self.responses), 1)
        snapshot = self.responses[0]
        commit = {k: v for k, v in snapshot.items() if k not in ('mac', 'event')}
        commit.update(kind='commit', decision='hold_confirmed')
        self.msg('commit', sign(commit))
        c.step(now=5)
        self.assertTrue(self.link.disconnect_pending())
        self.assertEqual(self.runner.state, 'hold')
        self.assertEqual(self.runner.resume_count, 1)
        self.assertEqual(len(self.responses), 2)

    def test_retained_and_wrong_qos_do_not_enter_control_queue(self):
        self.msg('request', {}, retain=True)
        self.msg('request', {}, qos=0)
        self.assertEqual(self.link.poll_reconcile(), [])

    def test_failed_stop_event_is_retained_once_without_automatic_republish(self):
        self.assertEqual(len(self.event_sends), 1)
        c = self.ctl
        previous = dict(c._reconcile_event)
        c._stopped.clear()
        c.error = None
        c._reconcile_event = None
        self.link._disconnect_observed.clear()
        self.link.publish_event_once = lambda event: self.event_sends.append(event) and False
        c._watch_stop()
        event = dict(c._reconcile_event)
        self.assertNotEqual(event['event_id'], previous['event_id'])
        self.assertEqual(c.error, 'event_unconfirmed')
        c._watch_stop()
        self.assertEqual(c._reconcile_event, event)
        self.assertEqual(len(self.event_sends), 2)
        probe = sign({'v': 1, 'kind': 'probe', 'nonce': 'a' * 64, 'server_epoch': 'b' * 64,
                      'robot_id': 'physical-1', 'robot_session': c.session, 'order_id': 'T1',
                      'seq': 1, 'order_digest': c.book.current_digest, 'map_id': 'm@v1',
                      'release_index': c.book.release_i, 'stopped_at': 'B', 'event_id': None,
                      'event_digest': None})
        self.msg('request', probe)
        c.step(now=5)
        self.assertEqual(self.responses[-1]['event'], event)
        self.assertEqual(len(self.event_sends), 2)

    def test_disconnect_at_stop_keeps_one_local_only_event(self):
        c = self.ctl
        c._reconcile_event = None
        c._stopped.clear()
        c.error = 'link_lost'
        self.link.connected = False
        before = len(self.event_sends)
        c._watch_stop()
        event = dict(c._reconcile_event)
        self.assertEqual(event['stopped_at'], 'B')
        self.assertEqual(c.error, 'event_unconfirmed')
        c._watch_stop()
        self.assertEqual(c._reconcile_event, event)
        self.assertEqual(len(self.event_sends), before)

    def test_repeated_route_requires_exact_observed_visit_index(self):
        c = self.ctl
        nav = c.nav
        nav._strict_repeat, nav.route = True, list(c.book.current['nodes'])
        nav._target_i = c.book.release_i
        nav._route_pos = c.book.release_i
        nav.target = c.book.current['released_until']
        self.assertIsNotNone(c._reconcile_snapshot())
        nav._route_pos -= 1
        self.assertIsNone(c._reconcile_snapshot())
        nav._route_pos = c.book.release_i
        nav._target_i += 1
        self.assertIsNone(c._reconcile_snapshot())
        nav._target_i = c.book.release_i
        nav.route = list(reversed(nav.route))
        self.assertIsNone(c._reconcile_snapshot())

    def test_one_shot_response_is_not_replayed_after_loss(self):
        once = MagicMock()
        once.connect.return_value = 0
        once.publish.return_value = SimpleNamespace(rc=0, mid=4)
        def poll(*_args, **_kwargs):
            once.on_connect(once, None, None, 0)
            return 0
        once.loop.side_effect = poll
        def publish(*_args, **_kwargs):
            self.link._on_disconnect(self.client, None, 1)
            return SimpleNamespace(rc=0, mid=4)
        once.publish.side_effect = publish
        generation, signal = self.link._connection_generation, self.link._disconnect_signal
        with patch('agv.link.mqtt.mqtt.Client', return_value=once) as factory:
            ok = self.link._publish_once('reconcile/response', {'signed': 'test'},
                    lambda: self.link._reconcile_current(generation, signal),
                    'agv-reconcile-', 1)
        self.assertFalse(ok)
        self.assertEqual(once.connect.call_count, 1)
        self.assertEqual(once.publish.call_count, 1)
        self.assertEqual(once.disconnect.call_count, 1)
        self.assertFalse(factory.call_args.kwargs['reconnect_on_failure'])


if __name__ == '__main__':
    unittest.main()
