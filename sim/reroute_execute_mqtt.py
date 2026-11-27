"""Run a reserved virtual reroute only after the actual server guard accepts it."""
from contextlib import contextmanager
import hashlib
import hmac
import json

from server.sim_mqtt_endpoint import IsolatedMqttEndpoint
from server.sim_reroute_endpoint_lifecycle import stop_forbidden
from server.sim_reroute_execute_wire import SimRerouteExecutionGuard
from sim.reroute_execution import SimRerouteExecution


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('ascii')


class SimRerouteExecutionMqttReceiver:
    def __init__(self, guard, execution, *, receipt_key):
        if (type(guard) is not SimRerouteExecutionGuard or type(execution) is not SimRerouteExecution
                or execution.executor is not guard.robot_receiver.robot.executor
                or not isinstance(receipt_key, bytes) or len(receipt_key) < 32
                or not hmac.compare_digest(receipt_key, guard.robot_receiver._key)):
            raise ValueError('bound virtual execution and live receipt key required')
        self.guard, self.execution, self._key = guard, execution, receipt_key
        robot = guard.robot_receiver.robot
        self._topic = 'agv/{}/reroute/execute'.format(robot.robot_id)
        self._response = 'agv/{}/reroute/execution-receipt'.format(robot.robot_id)
        self.endpoint = IsolatedMqttEndpoint(robot.port, {self._topic}, {self._response}, self._process,
            expected_security=guard.runtime._mqtt_security, effect_guard=self._peer_fences,
            stop_forbidden=lambda: stop_forbidden(guard.robot_receiver)
                or guard.activation_receiver.endpoint._connection_fence._is_owned())
        guard.bind_receiver(self)

    @contextmanager
    def _peer_fences(self):
        with self.guard.robot_receiver._lifecycle_lock, self.guard.robot_receiver._connection_fence, \
                self.guard.activation_receiver.endpoint._connection_fence:
            yield

    def _process(self, topic, payload):
        if topic != self._topic:
            raise ValueError('virtual execution topic differs')
        with self.guard.guard(payload):
            robot = self.guard.robot_receiver.robot
            driver = robot._reroute_driver
            if driver is None:
                result = self.execution.record(payload)
            else:
                # QoS1 may redeliver a request after motion began.  Do not
                # re-run the parent-stop check or install a second driver.
                # Compare the exact in-process driver and durable lineage
                # under the same live generation and motion fences.
                if driver[0] is not self.execution or driver[1] != payload or driver[2] is not self.guard:
                    raise ValueError('different virtual execution driver is already installed')
                data = self.execution._matching_record(payload)
                result = {k: data[k] for k in ('task_id', 'transition_id', 'generation', 'parent_seq')}
                result.update(digest=hashlib.sha256(payload).hexdigest(), state='recorded_not_executed', new=False)
        # start_reroute reacquires the guard itself; do not nest the
        # non-reentrant robot motion fence held by guard.guard().
        state = self.guard.robot_receiver.robot.start_reroute(self.execution, payload, guard=self.guard)
        data = {k: result[k] for k in ('task_id', 'transition_id', 'generation', 'parent_seq', 'digest', 'state')}
        data.update(robot_id=self.guard.robot_receiver.robot.robot_id,
                    robot_session=self.execution.robot_session, motion_state=state['state'])
        signed = {'type': 'sim.reroute.execution-receipt.v1', 'data': data}
        return self._response, _canonical(dict(signed, mac=hmac.new(self._key, _canonical(signed), hashlib.sha256).hexdigest()))

    def start(self):
        self.execution.initialize()
        self.endpoint.start()
        return self

    def stop(self):
        self.endpoint.stop()
