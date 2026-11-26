"""Physical hold observers must never redirect their loopback connection."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server.physical_reconcile_mqtt import PhysicalReconcileMqtt
from server.tests import test_physical_observation_exchange as fixtures


class FakeClient:
    _reconnect_on_failure = False

    def __init__(self, proxy=None):
        self._proxy = {} if proxy is None else proxy
        self.connect_calls = 0

    def connect(self, *_args, **_kwargs):
        self.connect_calls += 1
        raise AssertionError('a proxy must be rejected before connect')

    def disconnect(self):
        pass

    def loop_stop(self):
        pass


class PhysicalProxyGuardTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PhysicalReconcileExchangeTests(
            'test_ordinary_gate_and_inbox_still_reject_physical_execution')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def attempt(self, kind, client):
        if kind == 'watcher':
            target = self.fixture.watcher
            target._client = client
        else:
            target = PhysicalReconcileMqtt(self.fixture.service, port=1,
                                           client=client, isolated=True)
        with self.assertRaisesRegex(RuntimeError, 'proxy'):
            target.start()
        self.assertEqual(client.connect_calls, 0)
        self.assertIn('T1', self.fixture.gate._blocked)

    def test_process_wide_socks_proxy_is_rejected_before_connect(self):
        fake = SimpleNamespace(get_default_proxy=lambda: (2, '192.0.2.1', 1080))
        with patch('sim.mqtt_order_robot.mqtt.socks', fake):
            for kind in ('watcher', 'bridge'):
                with self.subTest(kind=kind):
                    self.attempt(kind, FakeClient())

    def test_explicit_client_proxy_is_rejected_even_without_global_proxy(self):
        with patch('sim.mqtt_order_robot._verify_no_proxy'):
            for kind in ('watcher', 'bridge'):
                with self.subTest(kind=kind):
                    self.attempt(kind, FakeClient({'proxy_type': 2, 'proxy_addr': '192.0.2.1'}))

    def test_malformed_client_proxy_is_also_rejected(self):
        # A malformed or unknown client proxy representation must not be
        # treated as a verified direct connection.
        for proxy in ('unexpected-proxy', ('socks', '192.0.2.1'), False):
            with self.subTest(proxy=proxy), self.assertRaisesRegex(RuntimeError, 'proxy'):
                from server.physical_evidence_observer import require_direct_mqtt_client
                require_direct_mqtt_client(FakeClient(proxy))

    def test_environment_mqtt_proxy_is_rejected_before_connect(self):
        fake = SimpleNamespace(get_default_proxy=lambda: None)
        with patch('sim.mqtt_order_robot.mqtt.socks', fake), \
             patch('sim.mqtt_order_robot.urllib.request.getproxies', return_value={'mqtt': 'socks://192.0.2.1:1080'}), \
             patch('sim.mqtt_order_robot.urllib.request.proxy_bypass', return_value=False):
            for kind in ('watcher', 'bridge'):
                with self.subTest(kind=kind):
                    self.attempt(kind, FakeClient())


if __name__ == '__main__':
    unittest.main()
