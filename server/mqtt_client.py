"""Receive fleet observations. Observations alone never grant drive permission.

The retained flag and received-live flags describe MQTT delivery, not physical
location, freshness or proof that an AGV is safe to move. All such decisions
require the agreed fleet protocol and independent safety interlocks.
"""
import copy
import json
import math
import os
import threading
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

# Temporary defensive limits; agree on operational limits with the team.
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_JSON_DEPTH = 32
MAX_ROBOT_ID_BYTES = 128
MAX_ROBOTS = 128


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('invalid JSON constant')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('invalid JSON number')
    return number


def _within_json_depth(value):
    pending = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            return False
        children = current.values() if isinstance(current, dict) else current
        for child in children:
            if isinstance(child, (dict, list)):
                pending.append((child, depth + 1))
    return True


def _strings_are_utf8(value):
    """Reject unpaired Unicode surrogates in JSON keys and string values."""
    pending = [value]
    try:
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                pending.extend(current.keys())
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)
            elif isinstance(current, str):
                current.encode('utf-8', errors='strict')
    except UnicodeEncodeError:
        return False
    return True


class MqttService:
    """Background MQTT receiver; snapshots are copies protected by a lock."""

    def __init__(self, host, port, client=None):
        self.host = host
        self.port = port
        self.client = client if client is not None else mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self._lock = threading.RLock()
        self._connected = False
        self._ready = False
        self._stopped = False
        self._pending_mid = None
        self._robots = {}

    def start(self):
        with self._lock:
            self._stopped = False
        result = self.client.connect(self.host, self.port, keepalive=60)
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError('MQTT connection could not be initiated')
        try:
            result = self.client.loop_start()
            if result != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('MQTT network loop could not be started')
        except Exception:
            self.client.disconnect()
            raise
        return self

    def stop(self):
        with self._lock:
            self._stopped = True
        self._mark_disconnected()
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def snapshot(self):
        with self._lock:
            return copy.deepcopy({'connected': self._connected, 'ready': self._ready,
                                  'robots': self._robots})

    def _mark_disconnected(self):
        with self._lock:
            self._connected = False
            self._ready = False
            self._pending_mid = None
            for observation in self._robots.values():
                observation['state_received_live'] = False
                observation['connection_received_live'] = False

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self._mark_disconnected()
        if reason_code.is_failure:
            print('[MQTT] broker connection rejected', flush=True)
            return
        with self._lock:
            if self._stopped:
                return
            self._connected = True
            try:
                result, mid = client.subscribe([('agv/+/state', 0), ('agv/+/conn', 1)])
            except OSError:
                self._mark_disconnected()
                print('[MQTT] subscription request failed', flush=True)
                return
            if result == mqtt.MQTT_ERR_SUCCESS:
                self._pending_mid = mid
            else:
                print('[MQTT] subscription request failed', flush=True)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        with self._lock:
            if not self._connected or mid != self._pending_mid:
                return
            self._pending_mid = None
            self._ready = len(reason_codes) == 2 and all(not code.is_failure for code in reason_codes)
            if not self._ready:
                print('[MQTT] subscription rejected', flush=True)

    def _on_message(self, client, userdata, message):
        parts = message.topic.split('/')
        if len(parts) != 3 or parts[0] != 'agv' or not parts[1] or parts[2] not in ('state', 'conn'):
            return
        try:
            if len(parts[1].encode('utf-8')) > MAX_ROBOT_ID_BYTES or len(message.payload) > MAX_PAYLOAD_BYTES:
                return
        except UnicodeEncodeError:
            return
        try:
            text = message.payload.decode('utf-8')
            if parts[2] == 'state':
                value = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant,
                                   parse_float=_finite_float)
                if (not isinstance(value, dict) or not _within_json_depth(value)
                        or not _strings_are_utf8(value)):
                    return
            else:
                value = text
                if value not in ('online', 'offline'):
                    return
        except (UnicodeDecodeError, ValueError, RecursionError):
            return
        with self._lock:
            if not self._connected:
                return
            if parts[1] not in self._robots and len(self._robots) >= MAX_ROBOTS:
                return
            observation = self._robots.setdefault(parts[1], {
                'state': None, 'state_received_live': False,
                'connection_report': None, 'connection_received_live': False,
            })
            if parts[2] == 'state':
                observation['state'] = value
                observation['state_received_live'] = not message.retain
            else:
                observation['connection_report'] = value
                observation['connection_received_live'] = not message.retain

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._mark_disconnected()


def start_mqtt():
    env_path = Path(__file__).resolve().parent.parent / '.env'
    load_dotenv(env_path)
    host = os.getenv('MQTT_HOST')
    port = os.getenv('MQTT_PORT')
    if not host or not port:
        raise RuntimeError('MQTT_HOST and MQTT_PORT must be set')
    try:
        port = int(port)
    except ValueError as error:
        raise RuntimeError('MQTT_PORT must be a valid port number') from error
    if not 1 <= port <= 65535:
        raise RuntimeError('MQTT_PORT must be a valid port number')
    return MqttService(host, port).start()


def stop_mqtt(service):
    service.stop()
