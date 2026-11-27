"""Explicit, loopback-only automatic reroute for one virtual workflow.

No stage is retried after an uncertain write or publish.  The caller owns the
session and must stop it after the simulation. This never operates real robots.
"""
from contextlib import ExitStack, closing
import json
import sqlite3
import threading
from functools import wraps

from server.segments import plan_segment
from server.sim_reroute_activation import stage_activation
from server.sim_reroute_activate_wire import publish_activation, SimRerouteActivationGuard
from server.sim_reroute_activate_receipt import ActivationReceiptInbox, ActivationReceiptReceiver
from server.sim_reroute_execute_wire import prepare_execution, publish_execution, SimRerouteExecutionGuard
from server.sim_reroute_finish import finish_reroute
from server.sim_reroute_receipt import SimRerouteReceiptInbox, SimRerouteReceiptReceiver
from server.sim_reroute_wire import SimRerouteProposalGuard, publish_proposal
from sim.reroute_acceptance import SimRerouteAcceptance
from sim.reroute_activation import SimRerouteActivation
from sim.reroute_activation_mqtt import SimRerouteActivationMqttReceiver
from sim.reroute_execution import SimRerouteExecution
from sim.reroute_execute_mqtt import SimRerouteExecutionMqttReceiver
from sim.reroute_mqtt import SimRerouteMqttReceiver


def _serialized(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self._lifecycle_lock:
            return method(self, *args, **kwargs)
    return invoke


class SimRerouteAutoSession:
    """Own actual MQTT handshakes for one live robot and its current task."""
    def __init__(self, runtime, robot, session, *, key, simulation_enabled=False):
        if simulation_enabled is not True or not runtime._reroute_transitions or not isinstance(session, str) or not session:
            raise ValueError('explicit isolated reroute session required')
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError('isolated virtual robot session key required')
        self.runtime, self.robot, self.session = runtime, robot, session
        self._lifecycle_lock = threading.RLock()
        self._cleanup = ExitStack()
        self._cleanup_failure = None
        self._started, self._phase, self._task = False, 'idle', None
        self._evidence, self._event_id, self._payload = None, None, None
        self.receipts = SimRerouteReceiptInbox(runtime, {robot.robot_id: key})
        self.observer = SimRerouteReceiptReceiver(self.receipts)
        self.acceptance = SimRerouteAcceptance(robot.executor, robot_session=session)
        self.proposal = SimRerouteMqttReceiver(robot, SimRerouteProposalGuard(runtime, robot_id=robot.robot_id),
                                              self.acceptance, receipt_key=key)
        self.activation_receipts = ActivationReceiptInbox(runtime, self.receipts, self.proposal)
        self.activation_observer = ActivationReceiptReceiver(self.activation_receipts)
        self.activation = SimRerouteActivation(self.acceptance)
        self.activation_guard = SimRerouteActivationGuard(runtime, receipt_inbox=self.receipts,
                                                           robot_receiver=self.proposal)
        self.activation_receiver = SimRerouteActivationMqttReceiver(self.proposal, self.activation_guard,
                                                                     self.activation, receipt_key=key)
        self.execution = SimRerouteExecution(self.activation)
        self.guard = SimRerouteExecutionGuard(runtime, receipt_inbox=self.receipts, robot_receiver=self.proposal,
                                               activation_inbox=self.activation_receipts,
                                               activation_receiver=self.activation_receiver)
        self.execution_receiver = SimRerouteExecutionMqttReceiver(self.guard, self.execution, receipt_key=key)

    @_serialized
    def start(self):
        if self._started:
            raise RuntimeError('reroute session is already started')
        self._started = True
        try:
            self.receipts.initialize()
            self.activation_receipts.initialize()
            for endpoint in (self.observer, self.proposal, self.activation_observer,
                             self.activation_receiver, self.execution_receiver):
                endpoint.start()
                self._cleanup.callback(endpoint.stop)
            return self
        except BaseException as original:
            try:
                self._cleanup.close()
            except BaseException as cleanup_error:
                self._cleanup_failure = cleanup_error
                original.add_note('reroute session cleanup also failed: ' + repr(cleanup_error))
            self._phase = 'unknown'
            raise

    def stop(self):
        if self._lifecycle_lock._is_owned():
            raise RuntimeError('cannot stop a reroute session within its active operation')
        with self._lifecycle_lock:
            self._phase = 'stopped'
            if self._cleanup_failure is not None:
                raise RuntimeError('reroute session cleanup previously failed') from self._cleanup_failure
            try:
                self._cleanup.close()
            except BaseException as exc:
                self._cleanup_failure = exc
                raise RuntimeError('reroute session cleanup failed') from exc

    @_serialized
    def begin(self, task):
        """Reserve one safe candidate once; a missing candidate causes no write."""
        if self._phase == 'finished' and task != self._task:
            with closing(sqlite3.connect(self.runtime.path)) as db:
                previous = db.execute('SELECT status FROM tasks WHERE task_id=?', (self._task,)).fetchone()
            if previous != ('DONE',):
                raise RuntimeError('previous simulated reroute has not been handed off')
            self._phase, self._task = 'idle', None
            self._evidence, self._event_id, self._payload = None, None, None
        if not self._started or self._phase != 'idle':
            raise RuntimeError('reroute session is not idle')
        if self.runtime._tasks.get(task) != (self.robot.robot_id, self.session):
            raise ValueError('task is not bound to this simulated robot session')
        candidate = self.runtime.reroute_candidate(task)
        if candidate.get('status') != 'candidate':
            return False
        nodes = candidate.get('nodes')
        if not isinstance(nodes, list) or len(nodes) < 2 or plan_segment(self.runtime.progress._map, nodes).released_until != nodes[-1]:
            return False  # Multi-segment generations are deliberately unsupported.
        self._task, self._phase = task, 'preparing'
        try:
            result = self.runtime.prepare_reroute(task)
            if result.get('state') != 'prepared' or result.get('nodes') != nodes:
                raise RuntimeError('reroute reservation outcome is unknown')
            self._event_id = 'reroute-' + result['transition_id']
            self._phase = 'proposal_attempted'
            publish_proposal(self.runtime, task)
            return True
        except Exception:
            self._phase = 'unknown'
            raise

    @_serialized
    def step(self, dt):
        """Progress one bounded step; return synthetic final evidence if done."""
        if self._phase in ('idle', 'unknown', 'stopped'):
            raise RuntimeError('no active automatic reroute')
        task = self._task
        try:
            if self._phase == 'proposal_attempted':
                if self.receipts.get(task) is None:
                    return None
                stage_activation(self.runtime, task, self.receipts, self.proposal)
                self._phase = 'activation_attempted'
                publish_activation(self.runtime, task, self.receipts, self.proposal)
                return None
            if self._phase == 'activation_attempted':
                if self.activation_receipts.get(task) is None:
                    return None
                prepare_execution(self.runtime, task, self.guard, self.execution_receiver)
                with closing(sqlite3.connect(self.runtime.path)) as db:
                    row = db.execute('SELECT payload FROM sim_reroute_execution_attempts WHERE task_id=?', (task,)).fetchone()
                if not row or not isinstance(row[0], bytes):
                    raise RuntimeError('execution payload is unavailable')
                self._payload = row[0]
                self._phase = 'execution_attempted'
                publish_execution(self.runtime, task, self.guard, self.execution_receiver)
                return None
            if self._phase == 'execution_attempted':
                state = self.robot.executor.snapshot()
                data = json.loads(self._payload)['data']
                if state.get('reroute_transition_id') is None:
                    return None
                if state['reroute_transition_id'] != data['transition_id']:
                    raise RuntimeError('unexpected simulated generation')
                state = self.robot.tick(dt)
                if state.get('status') == 'moving':
                    return None
                if state.get('status') != 'stopped' or state.get('current_node') != data['nodes'][-1]:
                    raise RuntimeError('simulated reroute stop differs')
                receipt, order = state.get('receipt') or {}, state.get('order') or {}
                if (receipt.get('state') != 'completed' or receipt.get('seq') != data['parent_seq'] + 1
                        or order.get('order_id') != task or order.get('nodes') != data['nodes']):
                    raise RuntimeError('simulated generation receipt differs')
                # The guard verifies the exact live route, session, both holds,
                # and stopped robot before deriving the new clearance event.
                with self.guard.guard(self._payload):
                    held = {item for item, owner in self.runtime.store.list_occupancy().items()
                            if owner == self.robot.robot_id}
                    keep = {('node', data['nodes'][-1])}
                    if not keep <= held:
                        raise RuntimeError('goal reservation is unavailable')
                    event = {'type': 'rear_clear', 'event_id': self._event_id, 'robot_session': self.session,
                             'order_id': task, 'seq': receipt['seq'], 'map_id': data['map_id'],
                             'stopped_at': data['nodes'][-1], 'cleared_resources':
                             [list(item) for item in sorted(held - keep)]}
                    self._evidence = {'task_id': task, 'session': self.session, 'robot_id': self.robot.robot_id,
                        'seq': receipt['seq'], 'stopped_at': data['nodes'][-1], 'event_id': self._event_id,
                        'map_id': data['map_id'], 'digest': receipt['digest']}
                self._phase = 'event_attempted'
                self.robot.publish_event(event)
                return None
            if self._phase == 'event_attempted':
                item = self.runtime.inbox.get(self.robot.robot_id, self._event_id)
                if item is None:
                    return None
                if item.get('state') != 'pending':
                    raise RuntimeError('new generation event outcome is not pending')
                if (self.robot.robot_id, self._event_id) not in self.runtime.receiver._occupied_events:
                    return None
                result = finish_reroute(self.runtime, task, self._event_id, self.guard)
                if result.get('state') != 'reached_goal_held':
                    raise RuntimeError('simulated completion outcome is unknown')
                self._phase = 'finished'
                return dict(self._evidence)
            if self._phase == 'finished':
                return dict(self._evidence)
            raise RuntimeError('unexpected automatic reroute state')
        except Exception:
            self._phase = 'unknown'
            raise
