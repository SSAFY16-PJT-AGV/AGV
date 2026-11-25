"""MQTT subscription/connection evidence must fail closed without real robots."""
import json
import threading
import unittest
from types import SimpleNamespace

from server.physical_state_proof import PhysicalStateWatcher


class FakeClient:
    def __init__(self):
        self.subscriptions = []
        self.rc = 0
        self.disconnected = False

    def connect(self, *_args, **_kwargs):
        return 0

    def loop_start(self):
        self.on_connect(self, None, None, SimpleNamespace(is_failure=False), None)
        return 0

    def subscribe(self, topics, *args, **kwargs):
        self.subscriptions.append((topics, args, kwargs))
        return self.rc, 19

    def disconnect(self):
        self.disconnected = True

    def loop_stop(self):
        pass


def message(robot, kind, payload, *, retain=False, qos=1):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode('utf-8')
    return SimpleNamespace(topic='agv/%s/%s' % (robot, kind), payload=payload,
                           retain=retain, qos=qos)


class SubscriptionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.watcher = PhysicalStateWatcher(
            ['physical-1', 'physical-2'], client=self.client, host='127.0.0.1', port=1,
            connect_timeout=0.01, log=lambda _message: None)
        self.watcher._mqtt_started = True
        self.watcher._on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.state = {'robot_session': 's1', 'status': 'WAITING', 'order_id': 'T1', 'seq': 1}

    def suback(self, *, mid=19, codes=(0, 1, 0, 1)):
        self.watcher._on_subscribe(self.client, None, mid,
                                   [SimpleNamespace(is_failure=code >= 128, value=code) for code in codes], None)

    def send(self, robot, kind, payload, **flags):
        self.watcher._on_message(self.client, None, message(robot, kind, payload, **flags))

    def ready(self):
        self.suback()
        for robot in ('physical-1', 'physical-2'):
            self.send(robot, 'conn', b'online', retain=True)

    def test_subscribes_to_each_state_and_connection_before_marking_ready(self):
        self.assertEqual(self.client.subscriptions, [([
            ('agv/physical-1/state', 0), ('agv/physical-1/conn', 1),
            ('agv/physical-2/state', 0), ('agv/physical-2/conn', 1)], (), {})])
        self.assertFalse(self.watcher._connected.is_set())
        self.send('physical-1', 'state', self.state)
        self.assertIsNone(self.watcher.latest('physical-1'))
        self.ready()
        self.send('physical-1', 'state', self.state)
        self.assertEqual(self.watcher.latest('physical-1'), self.state)

    def test_bad_subacks_and_wrong_mid_never_allow_state(self):
        for mid, codes in ((20, (0, 1, 0, 1)), (19, (0, 128, 0, 1)),
                           (19, (0, 0, 0, 1)), (19, (0, 1))):
            with self.subTest(mid=mid, codes=codes):
                self.setUp()
                self.suback(mid=mid, codes=codes)
                self.assertFalse(self.watcher._connected.is_set())
                self.send('physical-1', 'conn', b'online')
                self.send('physical-1', 'state', self.state)
                self.assertIsNone(self.watcher.latest('physical-1'))

    def test_missing_suback_times_out_before_any_evidence(self):
        w = PhysicalStateWatcher(['physical-1'], client=FakeClient(), host='127.0.0.1',
                                 port=1, connect_timeout=0.01, log=lambda _message: None)
        with self.assertRaisesRegex(RuntimeError, 'subscription|timed out'):
            w.start()
        self.assertFalse(w._connected.is_set())
        self.assertTrue(w._client.disconnected)

    def test_offline_invalidates_only_that_robot_and_online_does_not_revive_it(self):
        self.ready()
        blocked = []
        self.watcher._runtime = SimpleNamespace(
            _tasks={'T1': ('physical-1', 's1'), 'T2': ('physical-2', 's2')},
            gate=SimpleNamespace(invalidate_many=lambda tasks: blocked.append(tuple(tasks))))
        self.watcher._sessions = {'physical-1': 's1', 'physical-2': 's2'}
        self.send('physical-1', 'state', self.state)
        other = dict(self.state, robot_session='s2', order_id='T2')
        self.send('physical-2', 'state', other)
        old_token = self.watcher._token('physical-1', self.state)
        self.assertIsNotNone(old_token)
        self.send('physical-1', 'conn', b'offline', retain=False)
        self.assertEqual(blocked, [('T1',)])
        self.assertIsNone(self.watcher.latest('physical-1'))
        self.assertEqual(self.watcher.latest('physical-2'), other)
        self.assertIsNone(self.watcher._token('physical-1', self.state))
        self.send('physical-1', 'conn', b'online', retain=False)
        self.send('physical-1', 'state', self.state)
        self.assertIsNone(self.watcher._token('physical-1', self.state))
        self.assertFalse(self.watcher._check_session('physical-1', self.state))

    def test_live_state_after_offline_before_runtime_needs_new_online(self):
        self.ready()
        self.send('physical-1', 'state', self.state)
        self.send('physical-1', 'conn', b'offline')
        self.send('physical-1', 'state', self.state)
        self.assertIsNone(self.watcher.latest('physical-1'))
        self.send('physical-1', 'conn', b'online')
        self.assertIsNone(self.watcher.latest('physical-1'))
        self.send('physical-1', 'state', self.state)
        self.assertEqual(self.watcher.latest('physical-1'), self.state)

    def test_invalid_connection_payload_revokes_existing_robot_state(self):
        self.ready()
        self.send('physical-1', 'state', self.state)
        self.send('physical-1', 'conn', b'garbage')
        self.assertIsNone(self.watcher.latest('physical-1'))

    def test_old_generation_suback_cannot_accept_reused_message_id(self):
        old_suback = self.client.on_subscribe
        self.watcher._on_disconnect(self.client, None, None, None, None)
        self.watcher._on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        granted = [SimpleNamespace(is_failure=False, value=code) for code in (0, 1, 0, 1)]
        old_suback(self.client, None, 19, granted, None)
        self.assertFalse(self.watcher._connected.is_set())
        self.client.on_subscribe(self.client, None, 19, granted, None)
        self.assertTrue(self.watcher._connected.is_set())

    def test_old_generation_disconnect_and_message_do_not_affect_reconnected_watcher(self):
        self.ready()
        old_disconnect, old_message = self.client.on_disconnect, self.client.on_message
        self.watcher._on_disconnect(self.client, None, None, None, None)
        self.watcher._on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.ready()
        self.send('physical-1', 'state', self.state)
        old_message(self.client, None, message('physical-1', 'conn', b'offline'))
        old_disconnect(self.client, None, None, None, None)
        self.assertTrue(self.watcher._connected.is_set())
        self.assertEqual(self.watcher.latest('physical-1'), self.state)

    def test_failed_subscribe_request_never_marks_ready(self):
        self.client.rc = 1
        self.watcher._on_connect(self.client, None, None, SimpleNamespace(is_failure=False), None)
        self.assertFalse(self.watcher._connected.is_set())
        self.assertTrue(self.watcher._subscription_done.is_set())

    def test_disconnect_serializes_after_suback_and_leaves_connection_closed(self):
        entered, release = threading.Event(), threading.Event()
        values = [SimpleNamespace(is_failure=False, value=code) for code in (0, 1, 0, 1)]

        class BlockingCodes:
            def __iter__(self):
                entered.set()
                if not release.wait(2):
                    raise AssertionError('test failed to release suback')
                return iter(values)

        suback = threading.Thread(target=self.client.on_subscribe,
                                  args=(self.client, None, 19, BlockingCodes(), None))
        suback.start()
        self.assertTrue(entered.wait(2))
        disconnect = threading.Thread(target=self.client.on_disconnect,
                                      args=(self.client, None, None, None, None))
        disconnect.start()
        release.set()
        suback.join(2)
        disconnect.join(2)
        self.assertFalse(suback.is_alive())
        self.assertFalse(disconnect.is_alive())
        self.assertFalse(self.watcher._connected.is_set())


if __name__ == '__main__':
    unittest.main()
