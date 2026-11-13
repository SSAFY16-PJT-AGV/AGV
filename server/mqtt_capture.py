"""Explicit MQTT-to-JSONL collector. It never publishes messages."""
import argparse
import os
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from server.mqtt_log import JsonlRecorder
from server.mqtt_security import configure_mqtt_client


class CaptureError(RuntimeError):
    """Capture ended with a possible gap or incomplete record."""


class CaptureSession:
    """One MQTT connection and one log; never silently reconnect after a gap."""

    def __init__(self, client, recorder, clock=time.monotonic):
        self.client = client
        self.recorder = recorder
        self._clock = clock
        self.ready = False
        self.error = None
        self._pending_mid = None
        self._stopping = False
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

    def _fail(self, message):
        if self.error is not None:
            return
        self.error = message
        self.ready = False
        self._pending_mid = None
        try:
            self.client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if self.error is not None or self._stopping:
            return
        if reason_code.is_failure:
            self._fail('MQTT broker rejected the connection')
            return
        try:
            result, mid = client.subscribe('#', qos=1)
        except Exception:
            self._fail('MQTT subscription request failed')
            return
        if result != mqtt.MQTT_ERR_SUCCESS:
            self._fail('MQTT subscription request failed')
            return
        self._pending_mid = mid

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if self.error is not None or self._stopping or mid != self._pending_mid:
            return
        self._pending_mid = None
        if (len(reason_codes) != 1 or reason_codes[0].is_failure
                or reason_codes[0].value != 1):
            self._fail('MQTT subscription was rejected or downgraded')
            return
        self.ready = True
        print('[capture] collecting MQTT user topics; Ctrl+C to stop', flush=True)

    def _on_message(self, client, userdata, message):
        if self.error is not None or self._stopping:
            return
        try:
            self.recorder.record(message.topic, message.payload, message.qos, message.retain)
        except KeyboardInterrupt:
            self._fail('MQTT log write interrupted; capture may be incomplete')
        except Exception:
            self._fail('MQTT log write failed; capture stopped')

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.ready = False
        self._pending_mid = None
        if not self._stopping:
            self._fail('MQTT connection lost; log may have a gap')

    def run(self, host, port, username=None, password=None, tls_ca=None):
        try:
            configure_mqtt_client(self.client, username, password, tls_ca)
        except RuntimeError as exc:
            raise CaptureError(str(exc)) from None
        try:
            try:
                result = self.client.connect(host, port, keepalive=60)
            except (OSError, ValueError) as exc:
                raise CaptureError('MQTT connection failed') from exc
            if result != mqtt.MQTT_ERR_SUCCESS:
                raise CaptureError('MQTT connection failed')
            deadline = self._clock() + 10.0
            try:
                # A single loop() does not reconnect automatically. A capture
                # gap therefore cannot be silently bridged by a new session.
                while True:
                    result = self.client.loop(timeout=1.0)
                    if self.error is not None:
                        raise CaptureError(self.error)
                    if result != mqtt.MQTT_ERR_SUCCESS:
                        raise CaptureError('MQTT network loop failed; log may have a gap')
                    if not self.ready and self._clock() >= deadline:
                        raise CaptureError('MQTT subscription acknowledgement timed out')
            except KeyboardInterrupt:
                self._stopping = True
                if self.error is not None:
                    raise CaptureError(self.error)
                if not self.ready:
                    raise CaptureError('Capture stopped before subscription acknowledgement')
                return
        finally:
            self._stopping = True
            self.ready = False
            self.client.disconnect()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Record MQTT user topics into a new JSONL file')
    parser.add_argument('--output', required=True, help='path to a new JSONL file')
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    host = os.getenv('MQTT_HOST')
    port_text = os.getenv('MQTT_PORT')
    if not host or not port_text:
        print('MQTT_HOST and MQTT_PORT must be set', file=sys.stderr)
        return 1
    try:
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError('invalid port')
    except ValueError:
        print('MQTT_PORT must be a valid port number', file=sys.stderr)
        return 1
    try:
        with JsonlRecorder(args.output) as recorder:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
            CaptureSession(client, recorder).run(host, port, os.getenv('MQTT_USERNAME'),
                                                os.getenv('MQTT_PASSWORD'), os.getenv('MQTT_TLS_CA'))
    except FileExistsError:
        print('Capture file already exists; choose a new path', file=sys.stderr)
        return 1
    except (OSError, ValueError, CaptureError) as exc:
        if isinstance(exc, CaptureError):
            print(str(exc), file=sys.stderr)
        else:
            print('Could not create or close the capture file', file=sys.stderr)
        return 1
    print('[capture] stopped', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
