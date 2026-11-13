"""Shared MQTT client authentication and verified TLS configuration."""
import os
import ssl


class SafeHandshakeSocket(ssl.SSLSocket):
    """Close failed Paho initial handshakes, which otherwise lose the socket.

    Paho's synchronous connection path discards the socket on any handshake
    exception; even SSLWantRead/Write therefore cannot be retried in that path.
    """

    def do_handshake(self, *args, **kwargs):
        try:
            return super().do_handshake(*args, **kwargs)
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise


def create_tls_context(ca_certs):
    context = ssl.create_default_context(cafile=ca_certs)
    context.sslsocket_class = SafeHandshakeSocket
    return context


def read_mqtt_security():
    return (os.getenv('MQTT_USERNAME'), os.getenv('MQTT_PASSWORD'),
            os.getenv('MQTT_TLS_CA'))


def configure_mqtt_client(client, username, password, tls_ca):
    if username is not None and not username.strip():
        raise RuntimeError('MQTT_USERNAME must not be empty when set')
    if tls_ca is not None and not tls_ca.strip():
        raise RuntimeError('MQTT_TLS_CA must not be empty when set')
    if password is not None and not username:
        raise RuntimeError('MQTT_USERNAME is required when MQTT_PASSWORD is set')
    if (username or password is not None) and not tls_ca:
        raise RuntimeError('MQTT_TLS_CA is required for authenticated MQTT')
    if tls_ca:
        try:
            client.tls_set_context(create_tls_context(tls_ca))
        except Exception:
            raise RuntimeError('MQTT TLS configuration failed') from None
    if username:
        try:
            client.username_pw_set(username, password)
        except Exception:
            raise RuntimeError('MQTT authentication configuration failed') from None
