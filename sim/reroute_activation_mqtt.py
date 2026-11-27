"""Record guarded virtual reroute activation over isolated MQTT, without moving."""
import hashlib
import hmac
import json
import re
from contextlib import contextmanager

from server.sim_mqtt_endpoint import IsolatedMqttEndpoint
from server.sim_reroute_endpoint_lifecycle import stop_forbidden
from server.sim_reroute_activate_wire import SimRerouteActivationGuard
from sim.order_inbox import _object_pairs, _constant
from sim.reroute_activation import SimRerouteActivation
from sim.reroute_mqtt import SimRerouteMqttReceiver


_FIELDS = frozenset(('robot_id', 'robot_session', 'task_id', 'transition_id', 'generation',
                     'parent_seq', 'digest', 'proposal_digest', 'state'))


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('ascii')


def verify_activation_receipt(payload, key):
    if not isinstance(payload, bytes) or len(payload) > 65536 or not isinstance(key, bytes) or len(key) < 32:
        raise ValueError('bounded receipt and a simulation key are required')
    try:
        message = json.loads(payload.decode('utf-8'), object_pairs_hook=_object_pairs, parse_constant=_constant)
        if (not isinstance(message, dict) or set(message) != {'type', 'data', 'mac'}
                or message['type'] != 'sim.reroute.activation-receipt.v1'
                or not isinstance(message['data'], dict) or set(message['data']) != _FIELDS
                or not isinstance(message['mac'], str) or re.fullmatch('[0-9a-f]{64}', message['mac']) is None):
            raise ValueError('invalid activation receipt')
        signed = {'type': message['type'], 'data': message['data']}
        if not hmac.compare_digest(hmac.new(key, _canonical(signed), hashlib.sha256).hexdigest(), message['mac']):
            raise ValueError('activation signature differs')
        data = message['data']
        if (data['robot_id'] not in ('sim-r1', 'sim-r2') or type(data['generation']) is not int
                or data['generation'] != 1 or type(data['parent_seq']) is not int or data['parent_seq'] < 1
                or any(not isinstance(data[k], str) or not data[k] or len(data[k].encode('utf-8')) > 256
                       for k in ('robot_session', 'task_id'))
                or not isinstance(data['transition_id'], str)
                or re.fullmatch('[0-9a-f]{32}', data['transition_id']) is None
                or any(not isinstance(data[k], str) or re.fullmatch('[0-9a-f]{64}', data[k]) is None
                       for k in ('digest', 'proposal_digest')) or data['state'] != 'recorded_not_executed'):
            raise ValueError('invalid activation receipt fields')
        return dict(data)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid simulated activation receipt') from exc


class SimRerouteActivationMqttReceiver:
    def __init__(self, proposal_receiver, guard, activation, *, receipt_key):
        if (type(proposal_receiver) is not SimRerouteMqttReceiver
                or type(guard) is not SimRerouteActivationGuard
                or guard.robot_receiver is not proposal_receiver
                or type(activation) is not SimRerouteActivation
                or activation.acceptance is not proposal_receiver.acceptance
                or not isinstance(receipt_key, bytes) or len(receipt_key) < 32
                or not hmac.compare_digest(receipt_key, proposal_receiver._key)):
            raise ValueError('bound simulated proposal, activation and session key required')
        self.proposal_receiver, self.guard, self.activation, self._key = proposal_receiver, guard, activation, receipt_key
        robot = proposal_receiver.robot
        self._topic = 'agv/{}/reroute/activate'.format(robot.robot_id)
        self._response = 'agv/{}/reroute/activation-receipt'.format(robot.robot_id)
        self.endpoint = IsolatedMqttEndpoint(robot.port, {self._topic}, {self._response}, self._process,
                                             expected_security=guard.runtime._mqtt_security,
                                             effect_guard=self._proposal_fence,
                                             stop_forbidden=lambda: stop_forbidden(proposal_receiver))

    @contextmanager
    def _proposal_fence(self):
        with self.proposal_receiver._lifecycle_lock, self.proposal_receiver._connection_fence:
            yield

    def _process(self, topic, payload):
        if topic != self._topic:
            raise ValueError('simulated activation topic differs')
        with self.guard.guard(payload):
            result = self.activation.record(payload)
        robot = self.proposal_receiver.robot
        if robot._client is not self.proposal_receiver._robot_client or not robot.ready:
            raise ValueError('simulated robot connection changed after activation record')
        data = {key: result[key] for key in ('task_id', 'transition_id', 'generation', 'parent_seq',
                                             'digest', 'proposal_digest', 'state')}
        data.update(robot_id=robot.robot_id, robot_session=self.activation.robot_session)
        signed = {'type': 'sim.reroute.activation-receipt.v1', 'data': data}
        return self._response, _canonical(dict(signed, mac=hmac.new(self._key, _canonical(signed), hashlib.sha256).hexdigest()))

    def start(self):
        self.activation.initialize()
        self.endpoint.start()
        return self

    def stop(self):
        self.endpoint.stop()
