"""TLS handshake ownership checks for the MQTT client's SSL context."""

import ssl
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.mqtt_security import SafeHandshakeSocket, create_tls_context


class MqttTlsSocketCleanupTest(unittest.TestCase):
    def _socket(self):
        left, right = socket.socketpair()
        self.addCleanup(right.close)
        self.addCleanup(left.close)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.sslsocket_class = SafeHandshakeSocket
        wrapped = context.wrap_socket(left, server_hostname='example.invalid',
                                      do_handshake_on_connect=False)
        self.addCleanup(wrapped.close)
        return wrapped

    def test_tls_context_uses_cleanup_socket_and_verifies_peer_and_hostname(self):
        system = ssl.create_default_context()
        authorities = system.get_ca_certs(binary_form=True)
        if not authorities:
            self.skipTest('no local trusted CA available for test fixture')
        with tempfile.TemporaryDirectory() as folder:
            ca_path = Path(folder) / 'ca.pem'
            ca_path.write_text(ssl.DER_cert_to_PEM_cert(authorities[0]), encoding='ascii')
            context = create_tls_context(str(ca_path))
        self.assertIs(context.sslsocket_class, SafeHandshakeSocket)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_failed_handshake_closes_socket_and_preserves_original_error(self):
        wrapped = self._socket()
        error = ssl.SSLCertVerificationError('invalid test certificate')
        with patch.object(ssl.SSLSocket, 'do_handshake', side_effect=error):
            with self.assertRaises(ssl.SSLCertVerificationError) as caught:
                wrapped.do_handshake()
        self.assertIs(caught.exception, error)
        self.assertEqual(wrapped.fileno(), -1)

    def test_want_read_or_write_also_closes_unowned_paho_handshake_socket(self):
        for exception_class in (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            with self.subTest(error=exception_class.__name__):
                wrapped = self._socket()
                error = exception_class('handshake needs more I/O')
                with patch.object(ssl.SSLSocket, 'do_handshake', side_effect=error):
                    with self.assertRaises(exception_class) as caught:
                        wrapped.do_handshake()
                self.assertIs(caught.exception, error)
                self.assertEqual(wrapped.fileno(), -1)

    def test_successful_handshake_does_not_close_socket(self):
        wrapped = self._socket()
        with patch.object(ssl.SSLSocket, 'do_handshake', return_value=None) as handshake:
            self.assertIsNone(wrapped.do_handshake())
        handshake.assert_called_once()
        self.assertGreaterEqual(wrapped.fileno(), 0)

    def test_close_error_does_not_mask_original_handshake_error(self):
        wrapped = self._socket()
        error = ssl.SSLError('original handshake error')
        with patch.object(ssl.SSLSocket, 'do_handshake', side_effect=error), \
             patch.object(SafeHandshakeSocket, 'close', side_effect=OSError('close failed')):
            with self.assertRaises(ssl.SSLError) as caught:
                wrapped.do_handshake()
        self.assertIs(caught.exception, error)
        ssl.SSLSocket.close(wrapped)


if __name__ == '__main__':
    unittest.main()
