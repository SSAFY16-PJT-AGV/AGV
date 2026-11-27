"""Connect one web pickup to the existing isolated MQTT reservation flow.

Only a dedicated simulation runtime and synthetic robot are allowed here.
This controller proves neither real position nor physical clearance. It does
not perform pickup, delivery, inventory updates, or physical robot control.
"""
import copy
import math
import threading
import time

from server.allocation_store import _id
from server.order_publisher import validate_order, _topic
from server.sim_handoff_store import SimResourceBusy, SimHandoffCoordinatorStore
from server.segments import plan_segment
from server.route_occurrence import plan_occurrence_segment


class PickupWorkflow:
    """Single explicitly mapped pickup task; uncertain operations never retry."""

    TASK = 'web-pickup'
    SESSION = 'demo-session'
    ROBOT = 'sim-r1'

    def __init__(self, runtime, robot, map_data, product_lookup, *, simulation_enabled=False,
                 clock=time.monotonic, timeout_seconds=30, task_id=TASK, session=SESSION,
                 action_at_end=None, accept_preceding=None, prepare_options=None,
                 robot_id=ROBOT, wait_for_reservation=False, reroute_session=None):
        if simulation_enabled is not True:
            raise ValueError('isolated simulation must be explicitly enabled')
        if not isinstance(product_lookup, dict) or not product_lookup or not callable(clock):
            raise ValueError('simulated product mapping and clock are required')
        if (prepare_options is not None and
                (not isinstance(prepare_options, dict)
                 or any(not isinstance(key, str) for key in prepare_options))):
            raise ValueError('simulation prepare options must be a dictionary')
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 300):
            raise ValueError('invalid simulated workflow timeout')
        _id(task_id, 'task_id')
        _id(session, 'robot_session')
        _topic(robot_id)
        if type(wait_for_reservation) is not bool:
            raise ValueError('reservation waiting must be explicitly enabled')
        if reroute_session is not None:
            from server.sim_reroute_automatic import SimRerouteAutoSession
            if (type(reroute_session) is not SimRerouteAutoSession or reroute_session.runtime is not runtime
                    or reroute_session.robot is not robot or reroute_session.session != session
                    or not wait_for_reservation):
                raise ValueError('matching explicit virtual reroute session is required')
        self.reroute_session = reroute_session
        self.ROBOT = robot_id
        self.wait_for_reservation = wait_for_reservation
        action = copy.deepcopy({'type': 'load'} if action_at_end is None else action_at_end)
        validate_order(self.ROBOT, {'order_id': task_id, 'seq': 1, 'map_id': 'demo@v1',
                                    'nodes': ['a', 'b'], 'released_until': 'b',
                                    'action_at_end': action})
        if accept_preceding is not None:
            if not isinstance(accept_preceding, tuple) or len(accept_preceding) != 2:
                raise ValueError('preceding task and stop must be explicit')
            _id(accept_preceding[0], 'preceding task')
            _id(accept_preceding[1], 'preceding stop')
            if accept_preceding[0] == task_id:
                raise ValueError('preceding task must differ')
        self.runtime, self.robot = runtime, robot
        self.task_id, self.session = task_id, session
        self.action_at_end, self.accept_preceding = action, accept_preceding
        # Handoff adapters own locks and are intentionally passed by identity.
        self.prepare_options = dict(prepare_options or {})
        self.map = copy.deepcopy(map_data)
        self.products = copy.deepcopy(product_lookup)
        self.clock, self.timeout_seconds = clock, float(timeout_seconds)
        self.lock = threading.RLock()
        self._status, self._error = 'idle', None
        self._product_id = self._pickup = self._allocation = None
        self._robot_state = None
        self._expected_seq = 1
        self._segment_start = None
        self._segment_start_index = 0
        self._waiting = None
        self._next_retry = None
        self._finished_evidence = None
        self._deadline = None
        self._last_position = None
        self._paused_at = None

    def pause_for_reconnect(self):
        with self.lock:
            if self._paused_at is not None:
                return True
            # Pending event delivery and unallocated handoffs need explicit
            # reconciliation; they cannot be safely guessed from transport state.
            if self._status not in ('moving', 'awaiting_order', 'pickup_reached') or self._waiting:
                return False
            now = self.clock()
            if self._deadline is not None and now >= self._deadline:
                return False
            self._paused_at = now
            return True

    def resume_after_reconnect(self):
        with self.lock:
            if self._paused_at is None:
                return False
            elapsed = max(0.0, self.clock() - self._paused_at)
            if self._deadline is not None:
                self._deadline += elapsed
            if self._next_retry is not None:
                self._next_retry += elapsed
            self._paused_at = None
            return True

    def _reset_deadline(self):
        self._deadline = self.clock() + self.timeout_seconds

    @property
    def reroute_handoff_guard(self):
        session = self.reroute_session
        if (session is None or session._phase != 'finished' or session._task != self.task_id
                or self._finished_evidence is None):
            return None
        return session.guard

    def _unknown(self):
        self._status = 'unknown'
        self._error = 'simulation outcome unavailable'

    def snapshot(self):
        with self.lock:
            result = {'status': self._status, 'task_id': self.task_id if self._product_id else None,
                      'product_id': self._product_id, 'pickup_node': self._pickup,
                      'route': list(self._allocation['nodes']) if self._allocation else [],
                      'error': self._error,
                      'finished_evidence': copy.deepcopy(self._finished_evidence)}
            if self._robot_state is not None:
                result.update(current_node=self._robot_state.get('current_node'),
                              pose=copy.deepcopy(self._robot_state.get('pose')))
            if self._status == 'rerouting':
                actual = self._robot_state.get('status') if isinstance(self._robot_state, dict) else None
                result['reroute_motion'] = {'moving': 'moving', 'stopped': 'waiting'}.get(actual, 'unknown')
            return result

    def _is_preceding_stop(self, state):
        if self.accept_preceding is None or not isinstance(state, dict):
            return False
        order, receipt = state.get('order'), state.get('receipt')
        if not isinstance(order, dict) or not isinstance(receipt, dict):
            return False
        nodes = order.get('nodes')
        task, stop = self.accept_preceding
        executor = getattr(self.robot, 'executor', None)
        recovered = (state.get('recovered') is True and callable(getattr(executor, 'matches_recovered_stop', None))
                     and executor.matches_recovered_stop(task, stop, receipt))
        return (state.get('status') == 'stopped' and state.get('current_node') == stop
                and receipt.get('state') == 'completed' and (receipt.get('new') is True or recovered)
                and receipt.get('order_id') == task and order.get('order_id') == task
                and receipt.get('seq') == order.get('seq') and isinstance(nodes, list)
                and bool(nodes) and nodes[-1] == stop and order.get('released_until') == stop)

    def submit(self, product_id):
        with self.lock:
            if self._status != 'idle':
                raise RuntimeError('simulated pickup already attempted')
            product = self.products.get(product_id)
            if not isinstance(product, dict) or not isinstance(product.get('pickup_node'), str):
                raise ValueError('unknown simulated product')
            if self.robot.ready is not True or self.runtime.status().get('ready') is not True:
                raise RuntimeError('isolated simulation is not ready')
            initial = self.robot.tick(0)
            idle = (isinstance(initial, dict) and initial.get('status') == 'idle'
                    and isinstance(initial.get('current_node'), str)
                    and initial.get('order') is None and initial.get('receipt') is None)
            if not idle and not self._is_preceding_stop(initial):
                raise RuntimeError('simulated robot is not independently idle')
            self._robot_state = copy.deepcopy(initial)
            self._segment_start = initial['current_node']
            self._product_id, self._pickup = product_id, product['pickup_node']
            # From the first mutating call onward, failures are uncertain;
            # only a classified, rolled-back reservation conflict is retried.
            try:
                self.runtime.create_task(self.task_id, reset_unallocated=True)
                self._reset_deadline()
                self._prepare_once()
            except Exception:
                self._unknown()
            return self.snapshot()

    def _prepare_once(self):
        try:
            if getattr(self.runtime, '_field_simulation', False) is True:
                node, incoming = self.robot.executor.field_stop_direction()
                if node != self._segment_start:
                    raise ValueError('field stop direction does not match position')
                allocation = self.runtime.prepare_field(
                    self.task_id, self._pickup,
                    [{'robot_id': self.ROBOT, 'node': node, 'incoming_edge': incoming, 'available': True}],
                    copy.deepcopy(self.action_at_end), **self.prepare_options)
            else:
                allocation = self.runtime.prepare(
                    self.task_id, self._pickup,
                    [{'robot_id': self.ROBOT, 'node': self._segment_start, 'available': True}],
                    copy.deepcopy(self.action_at_end), **self.prepare_options)
        except SimResourceBusy:
            adapter = self.prepare_options.get('handoff_store')
            valid_handoff = (set(self.prepare_options) == {'handoff_store'}
                             and type(adapter) is SimHandoffCoordinatorStore
                             and adapter._target == self.task_id and adapter._robot == self.ROBOT)
            if not self.wait_for_reservation or (self.prepare_options and not valid_handoff):
                raise
            self._status = 'waiting_reservation'
            self._next_retry = self.clock() + .25
            return
        if (not isinstance(allocation, dict) or allocation.get('task_id') != self.task_id
                or allocation.get('robot_id') != self.ROBOT
                or not isinstance(allocation.get('nodes'), list)
                or allocation['nodes'][0] != self._segment_start
                or allocation['nodes'][-1] != self._pickup):
            raise ValueError('invalid isolated allocation')
        self._allocation = copy.deepcopy(allocation)
        self.runtime.publish(self.task_id)
        self._status = 'awaiting_order'
        self._reset_deadline()

    def _order_and_receipt(self, state):
        order, receipt = state.get('order'), state.get('receipt')
        if not isinstance(order, dict) or not isinstance(receipt, dict):
            raise ValueError('missing simulated order receipt')
        validate_order(self.ROBOT, order)
        if (order['order_id'] != self.task_id or order['map_id'] != self._allocation['map_id']
                or order['nodes'] != self._allocation['nodes']
                or order['action_at_end'] != self.action_at_end or receipt.get('new') is not True
                or receipt.get('order_id') != self.task_id or receipt.get('seq') != order['seq']
                or not isinstance(receipt.get('digest'), str) or not receipt['digest']):
            raise ValueError('simulated order identity differs from allocation')
        return order, receipt

    def _confirm_waiting(self):
        waiting = self._waiting
        stored = self.runtime.inbox.get(self.ROBOT, waiting['event_id'])
        if stored is not None and stored.get('state') == 'pending' and self.wait_for_reservation:
            if self._next_retry is None or self.clock() >= self._next_retry:
                self._next_retry = self.clock() + .25
                result = self.runtime.receiver.retry_occupied_event(self.ROBOT, waiting['event_id'])
                if result.get('status') == 'occupied':
                    self._status = 'waiting_reservation'
                    if self.reroute_session is not None and self.reroute_session.begin(self.task_id):
                        self._status = 'rerouting'
                        self._reset_deadline()
                elif result.get('status') == 'not_occupied':
                    if self._status == 'waiting_reservation':
                        raise ValueError('reservation wait evidence disappeared')
                elif result.get('status') == 'advanced':
                    stored = self.runtime.inbox.get(self.ROBOT, waiting['event_id'])
                else:
                    raise ValueError('invalid reservation retry result')
        if stored is None or stored.get('state') == 'pending':
            return False
        result = stored.get('result')
        if stored.get('state') != 'applied' or not isinstance(result, dict):
            raise ValueError('simulated clearance event was not applied')
        if result.get('task_id') != self.task_id:
            raise ValueError('simulated event task mismatch')
        if waiting['final']:
            if result.get('status') != 'finished' or result.get('next_seq') is not None:
                raise ValueError('invalid final simulated progress')
            # This describes only synthetic event/stop evidence, not physical
            # robot identity, localization, clearance or a drive authorization.
            self._finished_evidence = copy.deepcopy(waiting['evidence'])
            self._status = 'pickup_reached'
            self._waiting = None
            return False
        if result.get('status') != 'advanced' or result.get('next_seq') != self._expected_seq + 1:
            raise ValueError('invalid subsequent simulated progress')
        self._segment_start = waiting['stop']
        if 'end_index' in waiting:
            self._segment_start_index = waiting['end_index']
        self._expected_seq += 1
        self._waiting = None
        self._status = 'awaiting_order'
        self._reset_deadline()
        return True

    def _publish_stopped(self, state, order, receipt):
        if (receipt.get('state') != 'completed' or state.get('current_node') != order['released_until']
                or order['seq'] != self._expected_seq):
            raise ValueError('simulated stop not independently confirmed')
        path, stop = order['nodes'], order['released_until']
        field = getattr(self.runtime, '_field_simulation', False) is True
        start_index = self._segment_start_index if field else path.index(self._segment_start)
        if path[start_index] != self._segment_start:
            raise ValueError('simulated visit cursor differs from stop')
        segment = (plan_occurrence_segment(self.map, path, start_index) if field
                   else plan_segment(self.map, path[start_index:]))
        if segment.released_until != stop:
            raise ValueError('stop exceeds reserved segment')
        end_index = segment.end_index if field else path.index(stop)
        if field and state.get('visit_index') != end_index:
            raise ValueError('simulated stopped visit differs from reservation')
        final = end_index == len(path) - 1
        keep = ({('node', stop)} if final
                else set((plan_occurrence_segment(self.map, path, end_index) if field
                          else plan_segment(self.map, path[end_index:])).resources))
        cleared = sorted(set(segment.resources) - keep)
        challenge = self.runtime.begin(self.task_id, self.session)['challenge']
        self.runtime.complete(self.task_id, self.session, challenge, self.ROBOT, self.task_id,
                              order['seq'], order['map_id'], stop, receipt['digest'], True)
        event_id = '{}-{}'.format(self.task_id, order['seq'])
        event = {'type': 'rear_clear', 'event_id': event_id, 'robot_session': self.session,
                 'order_id': self.task_id, 'seq': order['seq'], 'map_id': order['map_id'],
                 'stopped_at': stop, 'cleared_resources': [list(item) for item in cleared]}
        # Mark the transition before the one and only publish attempt.
        evidence = {'task_id': self.task_id, 'session': self.session, 'robot_id': self.ROBOT,
                    'seq': order['seq'], 'stopped_at': stop, 'event_id': event_id,
                    'map_id': order['map_id'], 'digest': receipt['digest']}
        self._waiting = {'event_id': event_id, 'stop': stop, 'final': final,
                         'evidence': evidence}
        if field:
            self._waiting['end_index'] = end_index
        self._next_retry = self.clock() + .25
        self._status = 'waiting_progress'
        self._reset_deadline()
        self.robot.publish_event(event)

    def tick(self, dt_seconds):
        try:
            valid = (type(dt_seconds) in (int, float) and math.isfinite(dt_seconds)
                     and dt_seconds >= 0)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError('elapsed time must be finite and nonnegative')
        with self.lock:
            if self._status in ('idle', 'unknown', 'pickup_reached'):
                return self.snapshot()
            try:
                if (self.robot.ready is not True or self.runtime.status().get('ready') is not True
                        or self.clock() >= self._deadline):
                    raise RuntimeError('isolated simulation not ready or timed out')
                if self._status == 'waiting_reservation' and self._allocation is None:
                    if self.robot.tick(0) != self._robot_state:
                        raise ValueError('idle simulation state changed during reservation wait')
                    if self.clock() >= self._next_retry:
                        self._prepare_once()
                    return self.snapshot()
                if self._status == 'rerouting':
                    evidence = self.reroute_session.step(dt_seconds)
                    self._robot_state = copy.deepcopy(self.robot.executor.snapshot())
                    position = (self._robot_state.get('current_node'), repr(self._robot_state.get('pose')))
                    if position != self._last_position:
                        self._last_position = position
                        self._reset_deadline()
                    if evidence is not None:
                        self._finished_evidence = copy.deepcopy(evidence)
                        self._waiting = None
                        self._status = 'pickup_reached'
                    return self.snapshot()
                if self._waiting and not self._confirm_waiting():
                    return self.snapshot()
                state = self.robot.tick(dt_seconds)
                if not isinstance(state, dict):
                    raise ValueError('invalid simulated robot state')
                self._robot_state = copy.deepcopy(state)
                position = (state.get('current_node'), repr(state.get('pose')))
                if position != self._last_position:
                    self._last_position = position
                    self._reset_deadline()
                if state.get('status') == 'idle' and state.get('receipt') is None:
                    self._status = 'awaiting_order'
                    return self.snapshot()
                if self._expected_seq == 1 and self._is_preceding_stop(state):
                    self._status = 'awaiting_order'
                    return self.snapshot()
                order, receipt = self._order_and_receipt(state)
                if (order['seq'] == self._expected_seq - 1 and state.get('status') == 'stopped'
                        and state.get('current_node') == self._segment_start):
                    self._status = 'awaiting_order'
                    return self.snapshot()
                if order['seq'] != self._expected_seq:
                    raise ValueError('unexpected simulated order sequence')
                if getattr(self.runtime, '_field_simulation', False) is True:
                    nodes = order['nodes']
                    if nodes[self._segment_start_index] != self._segment_start:
                        raise ValueError('simulated visit cursor differs from stop')
                    segment = plan_occurrence_segment(self.map, nodes, self._segment_start_index)
                else:
                    segment = plan_segment(self.map, order['nodes'][order['nodes'].index(self._segment_start):])
                if order['released_until'] != segment.released_until:
                    raise ValueError('simulated release differs from reservation')
                if state.get('status') == 'moving':
                    self._status = 'moving'
                elif state.get('status') == 'stopped':
                    self._publish_stopped(state, order, receipt)
                else:
                    raise ValueError('invalid simulated robot state')
            except Exception:
                self._unknown()
            return self.snapshot()
