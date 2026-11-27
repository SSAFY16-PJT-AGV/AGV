"""Record virtual robot activation receipts; neither move nor release holds."""
from contextlib import closing
import hashlib
import sqlite3

from server.sim_mqtt_endpoint import IsolatedMqttEndpoint
from server.sim_reroute_activate_wire import _activation, _check_binding
from server.sim_reroute_endpoint_lifecycle import stop_forbidden
from server.sim_reroute_receipt import SimRerouteReceiptInbox
from sim.reroute_activation_mqtt import verify_activation_receipt
from sim.reroute_mqtt import SimRerouteMqttReceiver


class ActivationReceiptInbox:
    def __init__(self, runtime, receipt_inbox, robot_receiver):
        if (type(receipt_inbox) is not SimRerouteReceiptInbox or receipt_inbox.runtime is not runtime
                or type(robot_receiver) is not SimRerouteMqttReceiver or robot_receiver.guard.runtime is not runtime):
            raise ValueError('bound virtual proposal receipt session required')
        self.runtime, self.proposals, self.robot_receiver = runtime, receipt_inbox, robot_receiver

    def initialize(self):
        runtime = self.runtime
        with runtime._runtime_read_operation(), runtime._lock:
            runtime._ready()
            with runtime.receiver._dispatch_lock, runtime.progress._transaction() as db:
                db.execute('CREATE TABLE IF NOT EXISTS sim_reroute_activation_receipts ('
                           'transition_id TEXT PRIMARY KEY NOT NULL, task_id TEXT UNIQUE NOT NULL, '
                           'robot_id TEXT NOT NULL, payload BLOB NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL)')

    def record(self, payload):
        robot_id = self.robot_receiver.robot.robot_id
        key = self.proposals._keys.get(robot_id)
        receipt = verify_activation_receipt(payload, key)
        if receipt['robot_id'] != robot_id:
            raise ValueError('activation receipt belongs to another robot')
        runtime, task_id = self.runtime, receipt['task_id']
        with runtime._runtime_read_operation():
            robot = _check_binding(runtime, task_id, self.proposals, self.robot_receiver)
            with self.robot_receiver._lifecycle_lock, self.robot_receiver._connection_fence, robot._lock, runtime._lock:
                runtime._ready()
                receiver = runtime.receiver
                with receiver._dispatch_lock, runtime.inbox._lock, robot._motion_fence:
                    with runtime.progress._transaction() as db:
                        message = _activation(db, runtime, task_id, self.proposals, self.robot_receiver,
                                              robot, state='uncertain')
                        import json
                        expected = json.loads(message)['data']
                        if (receipt['transition_id'] != expected['transition_id']
                                or receipt['robot_session'] != expected['robot_session']
                                or receipt['generation'] != expected['generation']
                                or receipt['parent_seq'] != expected['parent_seq']
                                or receipt['proposal_digest'] != expected['proposal_digest']
                                or receipt['digest'] != hashlib.sha256(message).hexdigest()
                                or receiver is not runtime.receiver or not receiver.ready):
                            raise ValueError('activation receipt differs from the sent generation')
                        digest = hashlib.sha256(payload).hexdigest()
                        current = db.execute('SELECT transition_id,robot_id,payload,digest,state '
                            'FROM sim_reroute_activation_receipts WHERE task_id=? OR transition_id=?',
                            (task_id, expected['transition_id'])).fetchall()
                        saved = (expected['transition_id'], robot_id, payload, digest, 'recorded_not_executed')
                        if current:
                            if current != [saved]:
                                raise ValueError('conflicting activation receipt')
                            new = False
                        else:
                            db.execute('INSERT INTO sim_reroute_activation_receipts VALUES (?,?,?,?,?,?)',
                                       (expected['transition_id'], task_id, robot_id, payload, digest,
                                        'recorded_not_executed'))
                            new = True
                        return {'new': new, 'state': 'recorded_not_executed', 'task_id': task_id}

    def get(self, task_id):
        with self.runtime._runtime_read_operation(), closing(sqlite3.connect(self.runtime.path)) as db:
            row = db.execute('SELECT transition_id,robot_id,payload,digest,state FROM sim_reroute_activation_receipts '
                             'WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            return None
        transition, robot, payload, digest, state = row
        key = self.proposals._keys.get(robot)
        if (key is None or not isinstance(payload, bytes) or hashlib.sha256(payload).hexdigest() != digest
                or state != 'recorded_not_executed'):
            raise ValueError('stored activation receipt is inconsistent')
        data = verify_activation_receipt(payload, key)
        if data['transition_id'] != transition or data['robot_id'] != robot or data['task_id'] != task_id:
            raise ValueError('stored activation receipt identity changed')
        return {'transition_id': transition, 'robot_id': robot, 'state': state, 'payload': payload}


class ActivationReceiptReceiver:
    def __init__(self, inbox):
        if type(inbox) is not ActivationReceiptInbox:
            raise ValueError('isolated simulated activation inbox required')
        self.inbox = inbox
        robot = inbox.robot_receiver.robot.robot_id
        self._topic = 'agv/{}/reroute/activation-receipt'.format(robot)
        self.endpoint = IsolatedMqttEndpoint(inbox.runtime.port, {self._topic}, set(), self._process,
                                             expected_security=inbox.runtime._mqtt_security,
                                             stop_forbidden=lambda: stop_forbidden(inbox.robot_receiver))

    def _process(self, topic, payload):
        if topic != self._topic:
            raise ValueError('activation receipt topic changed')
        self.inbox.record(payload)
        return None

    def start(self):
        self.endpoint.start()
        return self

    def stop(self):
        self.endpoint.stop()
