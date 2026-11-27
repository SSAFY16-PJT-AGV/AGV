"""A stopped Paho simulation client should close its wakeup sockets."""
import unittest
from unittest.mock import Mock

import paho.mqtt.client as mqtt

from server.sim_runtime import SimRuntime


class SimRuntimeSocketCleanupTest(unittest.TestCase):
    def test_loop_stop_closes_owned_paho_socketpair(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        self.assertEqual(client.loop_start(), mqtt.MQTT_ERR_SUCCESS)
        sockets = (client._sockpairR, client._sockpairW)
        self.assertTrue(all(socket is not None and socket.fileno() >= 0 for socket in sockets))
        SimRuntime._close_client(client)
        self.assertTrue(all(socket.fileno() == -1 for socket in sockets))

    def test_live_network_thread_is_not_reset(self):
        client = Mock(spec=mqtt.Client)
        client._thread = Mock()
        client._thread.is_alive.return_value = True
        client._reset_sockets = Mock()
        with self.assertRaises(RuntimeError):
            SimRuntime._close_client(client)
        client._reset_sockets.assert_not_called()

    def test_failed_loop_stop_does_not_reset_a_possibly_live_thread(self):
        client = Mock(spec=mqtt.Client)
        client.loop_stop.side_effect = RuntimeError('network loop blocked')
        client._reset_sockets = Mock()
        with self.assertRaises(RuntimeError):
            SimRuntime._close_client(client)
        client._reset_sockets.assert_not_called()

    def test_disconnect_failure_is_preserved_when_reset_also_fails(self):
        client = Mock(spec=mqtt.Client)
        client._thread = None
        first = RuntimeError('disconnect failed')
        client.disconnect.side_effect = first
        client._reset_sockets = Mock(side_effect=ValueError('reset failed'))
        with self.assertRaises(RuntimeError) as raised:
            SimRuntime._close_client(client)
        self.assertIs(raised.exception, first)
        client.loop_stop.assert_called_once()
        self.assertTrue(any('ValueError' in note for note in raised.exception.__notes__))


if __name__ == '__main__':
    unittest.main()
