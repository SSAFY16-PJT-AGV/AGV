"""Record signed virtual-robot reroute receipts without activating a route.

Keys are in-process isolation aids, not physical device identity. A receipt
never transfers occupancy or proves a physical stop or movement.
"""
import hashlib
import queue
import secrets
import threading

import paho.mqtt.client as mqtt

from server.allocation_store import _verify_field_global
from server.mqtt_security import configure_mqtt_client, read_mqtt_security
from server.sim_mqtt_cleanup import reset_stopped_client
from server.sim_reroute_wire import _message, _robot
from sim.mqtt_order_robot import _verify_no_proxy
from sim.reroute_mqtt import verify_receipt


class SimRerouteReceiptInbox:
    def __init__(self, runtime, robot_keys):
        if (not runtime._reroute_transitions or not isinstance(robot_keys, dict) or not robot_keys
                or any(robot not in ('sim-r1', 'sim-r2') or not isinstance(key, bytes) or len(key) < 32
                       for robot, key in robot_keys.items())
                or len(set(robot_keys.values())) != len(robot_keys)):
            raise ValueError('explicit isolated simulation and per-robot receipt keys required')
        self.runtime, self._keys = runtime, dict(robot_keys)

    def initialize(self):
        runtime = self.runtime
        with runtime._runtime_read_operation(), runtime._lock:
            runtime._ready()
            with runtime.receiver._dispatch_lock, runtime.progress._transaction() as db:
                _verify_field_global(db, field_required=True)
                db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_server_receipts ('
                           'transition_id TEXT PRIMARY KEY NOT NULL, task_id TEXT UNIQUE NOT NULL, '
                           'robot_id TEXT NOT NULL, payload BLOB NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL)')

    def record(self, payload):
        if not isinstance(payload, bytes) or len(payload) > 65536:
            raise ValueError('bounded signed simulated receipt required')
        verified = None
        for robot, key in self._keys.items():
            try:
                candidate = verify_receipt(payload, key)
            except ValueError:
                continue
            if candidate['robot_id'] == robot:
                if verified is not None:
                    raise ValueError('ambiguous receipt identity')
                verified = candidate
        if verified is None:
            raise ValueError('simulated receipt signature or identity differs')
        runtime, task = self.runtime, verified['task_id']
        with runtime._runtime_read_operation():
            robot = _robot(runtime, task)
            if robot.robot_id != verified['robot_id']:
                raise ValueError('receipt belongs to another simulated robot')
            with robot._lock, runtime._lock:
                runtime._ready()
                receiver = runtime.receiver
                with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                    if not robot.ready or robot._event_uncertain or not robot._queue.empty():
                        raise ValueError('simulated robot is unavailable')
                    with runtime.progress._transaction() as db:
                        _verify_field_global(db, field_required=True)
                        proposal = _message(db, runtime, task, required_state=('uncertain', 'accepted'))
                        import json
                        data = json.loads(proposal)['data']
                        if (verified['transition_id'] != data['transition_id']
                                or verified['robot_session'] != data['robot_session']
                                or verified['generation'] != data['generation']
                                or verified['parent_seq'] != data['parent_seq']
                                or verified['digest'] != hashlib.sha256(proposal).hexdigest()
                                or receiver is not runtime.receiver or not receiver.ready):
                            raise ValueError('simulated receipt differs from reserved proposal')
                        digest = hashlib.sha256(payload).hexdigest()
                        existing = db.execute('SELECT transition_id,robot_id,payload,digest,state '
                            'FROM sim_reroute_server_receipts WHERE task_id=? OR transition_id=?',
                            (task, data['transition_id'])).fetchall()
                        expected = (data['transition_id'], robot.robot_id, payload, digest, 'recorded_not_activated')
                        if existing:
                            if existing != [expected]:
                                raise ValueError('conflicting simulated receipt')
                            new = False
                        else:
                            db.execute('INSERT INTO sim_reroute_server_receipts VALUES (?,?,?,?,?,?)',
                                (data['transition_id'], task, robot.robot_id, payload, digest, 'recorded_not_activated'))
                            if db.execute("UPDATE sim_reroute_generations SET state='accepted' "
                                          "WHERE task_id=? AND state='uncertain'", (task,)).rowcount != 1:
                                raise ValueError('simulated reroute state changed before receipt')
                            _verify_field_global(db, field_required=True)
                            new = True
                        return {'new': new, 'state': 'recorded_not_activated', 'task_id': task}

    def get(self, task_id):
        from contextlib import closing
        import sqlite3
        with self.runtime._runtime_read_operation(), closing(sqlite3.connect(self.runtime.path)) as db:
            row = db.execute('SELECT transition_id,robot_id,payload,digest,state FROM sim_reroute_server_receipts '
                             'WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            return None
        transition, robot, payload, digest, state = row
        key = self._keys.get(robot)
        if (key is None or not isinstance(payload, bytes) or hashlib.sha256(payload).hexdigest() != digest
                or state != 'recorded_not_activated'):
            raise ValueError('stored simulated receipt is inconsistent')
        data = verify_receipt(payload, key)
        if data['transition_id'] != transition or data['robot_id'] != robot or data['task_id'] != task_id:
            raise ValueError('stored simulated receipt identity changed')
        return {'transition_id': transition, 'robot_id': robot, 'state': state, 'payload': payload}


class SimRerouteReceiptReceiver:
    """Consume receipt messages on a dedicated loopback-only MQTT connection."""
    def __init__(self, inbox):
        if type(inbox) is not SimRerouteReceiptInbox:
            raise ValueError('isolated simulated receipt inbox required')
        self.inbox = inbox
        self._queue = queue.Queue(maxsize=32)
        self._ready, self._failed, self._stopping = threading.Event(), threading.Event(), threading.Event()
        self._started, self._worker, self._mid = False, None, None
        self._lifecycle_lock = threading.RLock()
        self._network_cleanup_failed = False
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
            client_id='sim-reroute-receipt-' + secrets.token_hex(8), protocol=mqtt.MQTTv311,
            reconnect_on_failure=False)
        configure_mqtt_client(self._client, *read_mqtt_security())
        self._client.manual_ack_set(True)
        self._client.on_connect, self._client.on_subscribe = self._connect, self._subscribe
        self._client.on_message, self._client.on_disconnect = self._message, self._disconnect

    def _connect(self, client, _userdata, _flags, reason, _properties):
        if reason.is_failure or self._stopping.is_set():
            self._failed.set()
            return
        topics = [('agv/{}/reroute/receipt'.format(robot), 1) for robot in sorted(self.inbox._keys)]
        result, self._mid = client.subscribe(topics)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self._failed.set()

    def _subscribe(self, _client, _userdata, mid, reasons, _properties):
        if (self._stopping.is_set() or mid != self._mid or len(reasons) != len(self.inbox._keys)
                or any(reason.is_failure or reason.value != 1 for reason in reasons)):
            self._failed.set()
        else:
            self._ready.set()

    def _message(self, _client, _userdata, message):
        allowed = {'agv/{}/reroute/receipt'.format(robot) for robot in self.inbox._keys}
        if (self._stopping.is_set() or self._failed.is_set() or message.topic not in allowed
                or message.qos != 1 or message.retain or not isinstance(message.payload, bytes)
                or len(message.payload) > 65536):
            self._failed.set()
            return
        try:
            self._queue.put_nowait((message.topic, message.payload, message.mid, message.qos))
        except queue.Full:
            self._failed.set()

    def _disconnect(self, _client, _userdata, _flags, _reason, _properties):
        self._ready.clear()
        if not self._stopping.is_set():
            self._failed.set()

    def _run(self):
        while not self._stopping.is_set() and not self._failed.is_set():
            try:
                topic, payload, mid, qos = self._queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                robot = topic.split('/')[1]
                if verify_receipt(payload, self.inbox._keys[robot])['robot_id'] != robot:
                    raise ValueError('receipt topic and identity differ')
                self.inbox.record(payload)
                if (self._stopping.is_set() or not self._client.is_connected() or self._failed.is_set()
                        or self._client.ack(mid, qos) != mqtt.MQTT_ERR_SUCCESS):
                    raise RuntimeError('simulated receipt acknowledgement failed')
            except Exception:
                self._failed.set()
                self._ready.clear()
                return

    def start(self):
        with self._lifecycle_lock:
            return self._start_locked()

    def _start_locked(self):
        if self._started or self._stopping.is_set():
            raise RuntimeError('simulated receipt receiver cannot be restarted')
        self._started = True
        _verify_no_proxy()
        if getattr(self._client, '_proxy', None):
            raise RuntimeError('a client-specific MQTT proxy is not allowed')
        if read_mqtt_security() != self.inbox.runtime._mqtt_security:
            raise RuntimeError('simulated MQTT security configuration changed')
        try:
            if self._client.connect('127.0.0.1', self.inbox.runtime.port, keepalive=15) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('isolated simulated broker connect failed')
            if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('simulated MQTT loop failed')
            if not self._ready.wait(5) or self._failed.is_set():
                raise RuntimeError('simulated receipt receiver not ready')
            self._worker = threading.Thread(target=self._run, name='sim-reroute-receipt', daemon=True)
            self._worker.start()
            return self
        except BaseException:
            self.stop()
            raise

    def stop(self):
        with self._lifecycle_lock:
            return self._stop_locked()

    def _stop_locked(self):
        if self._stopping.is_set():
            if self._worker is not None:
                self._worker.join(timeout=10)
                if self._worker.is_alive():
                    raise RuntimeError('simulated receipt worker did not stop')
            if self._network_cleanup_failed:
                raise RuntimeError('simulated receipt network cleanup failed')
            reset_stopped_client(self._client)
            return
        self._stopping.set()
        self._ready.clear()
        errors = []
        loop_stopped = False
        for index, action in enumerate((self._client.disconnect, self._client.loop_stop)):
            try:
                action()
                if index == 1:
                    loop_stopped = True
            except BaseException as exc:
                errors.append(exc)
        if self._worker is not None:
            self._worker.join(timeout=10)
            if self._worker.is_alive():
                raise RuntimeError('simulated receipt worker did not stop')
        if loop_stopped:
            try:
                reset_stopped_client(self._client)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            self._network_cleanup_failed = True
            raise RuntimeError('simulated receipt network cleanup failed') from errors[0]
