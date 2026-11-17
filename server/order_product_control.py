"""One isolated web product order using existing reservation/MQTT workflows.

This is a synthetic demonstration. Its stops and Unreal acknowledgements do
not establish physical position, clearance or permission to move a real AGV.
"""
import copy
import math
import threading
import uuid

from server.order_pickup_workflow import PickupWorkflow
from server.allocation_store import _id
from server.routing import MapGraph
from server.sim_handoff_store import SimHandoffCoordinatorStore


class ProductControlEngine:
    """Coordinate simulated product legs with bounded, atomic handoffs."""
    PICKUP_TASK = 'web-pickup'
    DELIVERY_TASK = 'web-delivery'
    SESSION = 'demo-session'
    ROBOT = 'sim-r1'
    TWIN = 'AGV_01'
    MAX_ORDERS = 16

    def __init__(self, runtime, robot, sim_map, product_lookup, *,
                 robot_id='sim-r1', twin_id='AGV_01', session='demo-session', task_prefix='web',
                 wait_for_reservation=False):
        if not isinstance(product_lookup, dict) or not product_lookup:
            raise ValueError('explicit simulated product mapping required')
        for name, value in (('robot_id', robot_id), ('twin_id', twin_id),
                            ('session', session), ('task_prefix', task_prefix)):
            _id(value, name)
        if not robot_id.startswith('sim-') or robot_id == 'sim-':
            raise ValueError('isolated simulated robot required')
        self.ROBOT, self.TWIN, self.SESSION = robot_id, twin_id, session
        self.task_prefix = task_prefix
        if type(wait_for_reservation) is not bool:
            raise ValueError('reservation waiting must be explicit')
        self.wait_for_reservation = wait_for_reservation
        self.runtime, self.robot = runtime, robot
        self.map = copy.deepcopy(sim_map)
        self.graph = MapGraph(self.map)
        self.positions = {node['id']: tuple(node['pos']) for node in self.map['nodes']}
        self.products = copy.deepcopy(product_lookup)
        self.lock = threading.RLock()
        self._cycle = 0
        self._status, self._phase = 'idle', None
        self._product = self._pickup = self._destination = None
        self._shelf = self._slot = None
        self._route, self._pending = [], None
        self._pickup_workflow = self._delivery_workflow = None
        self._current_leg = None
        self._pickup_evidence = self._delivery_evidence = self._last_place_ack = None
        self._pickup_task = self._delivery_task = None
        self._suspended_state = None

    @property
    def control_ready(self):
        try:
            return self.robot.ready is True and self.runtime.status().get('ready') is True
        except Exception:
            return False

    def _robot_snapshot(self):
        state = self.robot.executor.snapshot()
        if not isinstance(state, dict) or not isinstance(state.get('current_node'), str):
            raise ValueError('simulated robot state unavailable')
        pose = state.get('pose')
        if not isinstance(pose, dict) or any(type(pose.get(key)) not in (int, float)
                                              or not math.isfinite(pose[key]) for key in ('x', 'y', 'th')):
            raise ValueError('simulated robot pose unavailable')
        return state

    def snapshot(self):
        with self.lock:
            try:
                robot = self._robot_snapshot()
                current, pose = robot['current_node'], copy.deepcopy(robot['pose'])
            except Exception:
                current, pose = None, None
            return {'status': self._status, 'phase': self._phase, 'mode': 'coordinated',
                    'orders_remaining': self.MAX_ORDERS - self._cycle,
                    'order_id': self._pickup_task if self._product else None,
                    'pickup_node': self._pickup, 'destination_node': self._destination,
                    'product_type': self._product, 'route': list(self._route),
                    'current_node': current, 'pose': pose, 'battery': 0.8,
                    'pending_action': copy.deepcopy(self._pending)}

    def create_order(self, pickup_node, destination_node):
        raise RuntimeError('direct node orders are not supported in reservation mode')

    def create_product_order(self, product_type, pickup, destination, shelf, slot):
        with self.lock:
            if self._status not in ('idle', 'completed') or self._cycle >= self.MAX_ORDERS:
                raise RuntimeError('simulated product order unavailable')
            expected = self.products.get(product_type)
            if (not isinstance(expected, dict) or expected.get('pickup_node') != pickup
                    or expected.get('destination_node') != destination
                    or expected.get('shelf_id') != shelf or type(slot) is not int
                    or expected.get('slot_index') != slot):
                raise ValueError('product is not in the configured simulated catalog')
            if not self.control_ready:
                raise RuntimeError('isolated workflow is not ready')
            state = self._robot_snapshot()
            previous = None
            if self._cycle == 0:
                if state.get('status') != 'idle' or state.get('order') is not None or state.get('receipt') is not None:
                    raise RuntimeError('simulated robot is not independently idle')
            else:
                try:
                    evidence = self._validate_finished(self._delivery_evidence, self._delivery_task, self._destination)
                    ack = copy.deepcopy(self._last_place_ack)
                    if (not isinstance(ack, dict) or ack.get('order_id') != self._delivery_task
                            or ack.get('agv_id') != self.TWIN or ack.get('action') != 'place'
                            or ack.get('applied') is not True or not isinstance(ack.get('action_id'), str)
                            or not ack['action_id']):
                        raise ValueError('previous simulated place acknowledgement unavailable')
                    previous = (self._delivery_task, self._destination, evidence, ack)
                except Exception:
                    self._mark_unknown()
                    raise RuntimeError('previous simulated stop cannot be confirmed') from None
            try:
                first = self.graph.shortest_path(state['current_node'], pickup).nodes
                second = self.graph.shortest_path(pickup, destination).nodes
                if len(first) < 2 or len(second) < 2:
                    raise ValueError('each simulated leg must contain an edge')
            except ValueError:
                raise
            self._route = list(first) + list(second[1:])
            self._cycle += 1
            prefix = self.task_prefix if self._cycle == 1 else '{}-{}'.format(self.task_prefix, self._cycle)
            self._pickup_task = '{}-pickup'.format(prefix)
            self._delivery_task = '{}-delivery'.format(prefix)
            self._product, self._pickup, self._destination = product_type, pickup, destination
            self._shelf, self._slot = shelf, slot
            self._pickup_evidence = self._delivery_evidence = self._last_place_ack = None
            self._status, self._phase = 'moving', 'to_pickup'
            self._current_leg = 'pickup'
            try:
                options = {}
                if previous is not None:
                    source, stop, evidence, ack = previous
                    challenge = self.runtime.begin(source, self.SESSION)['challenge']
                    if not isinstance(challenge, str) or not challenge:
                        raise ValueError('fresh simulated challenge unavailable')
                    proof = self.runtime.complete(source, self.SESSION, challenge, self.ROBOT,
                                                  source, evidence['seq'], evidence['map_id'], stop,
                                                  evidence['digest'], True)
                    if not isinstance(proof, dict) or proof.get('reconciled') is not True:
                        raise ValueError('fresh simulated proof unavailable')
                    adapter = SimHandoffCoordinatorStore(
                        self.runtime.store, target_task_id=self._pickup_task, source_task_id=source,
                        robot_id=self.ROBOT, robot_session=self.SESSION, seq=evidence['seq'],
                        stopped_at=stop, event_id=evidence['event_id'], action_ack=ack,
                        expected_twin_id=self.TWIN, gate=self.runtime.gate,
                        progress=self.runtime.progress, receiver=self.runtime.receiver,
                        simulation_enabled=True)
                    options = {'accept_preceding': (source, stop), 'prepare_options': {'handoff_store': adapter}}
                self._pickup_workflow = PickupWorkflow(
                    self.runtime, self.robot, self.map, self.products,
                    simulation_enabled=True, task_id=self._pickup_task, session=self.SESSION,
                    robot_id=self.ROBOT, wait_for_reservation=self.wait_for_reservation, **options)
                result = self._pickup_workflow.submit(product_type)
                if result.get('status') == 'unknown':
                    self._mark_unknown()
            except Exception:
                self._mark_unknown()
            return self.snapshot()

    def _mark_unknown(self):
        self._status, self._phase, self._pending = 'unknown', 'unknown', None
        self._suspended_state = None

    def suspend(self, uncertain_action_id=None):
        """Freeze an in-process demo without inventing an Unreal result."""
        with self.lock:
            if uncertain_action_id is not None and self._pending is not None:
                if self._pending.get('action_id') == uncertain_action_id:
                    self._mark_unknown()
                    return False
            if self._status == 'suspended':
                return True
            if self._status not in ('moving', 'waiting'):
                return self._status in ('idle', 'completed')
            workflow = self._pickup_workflow if self._current_leg == 'pickup' else self._delivery_workflow
            # A partially submitted rear-clear or allocation cannot safely be
            # retried merely because the transport has reconnected.
            if workflow is None or (hasattr(workflow, 'pause_for_reconnect')
                                    and not workflow.pause_for_reconnect()):
                self._mark_unknown()
                return False
            self._suspended_state = (self._status, self._phase)
            self._status, self._phase = 'suspended', 'connection_lost'
            return True

    def resume_after_reconnect(self):
        with self.lock:
            if self._status != 'suspended' or self._suspended_state is None or not self.control_ready:
                return False
            workflow = self._pickup_workflow if self._current_leg == 'pickup' else self._delivery_workflow
            if hasattr(workflow, 'resume_after_reconnect') and not workflow.resume_after_reconnect():
                self._mark_unknown()
                return False
            self._status, self._phase = self._suspended_state
            self._suspended_state = None
            return True

    def action_unknown(self):
        with self.lock:
            if self._status not in ('moving', 'waiting', 'suspended'):
                return False
            self._mark_unknown()
            return True

    def _validate_finished(self, evidence, task, node):
        if (not isinstance(evidence, dict) or evidence.get('task_id') != task
                or evidence.get('session') != self.SESSION or evidence.get('robot_id') != self.ROBOT
                or type(evidence.get('seq')) is not int or evidence['seq'] < 1
                or evidence.get('stopped_at') != node
                or evidence.get('map_id') != '{}@v{}'.format(self.graph.map_id, self.graph.version)
                or not isinstance(evidence.get('event_id'), str) or not evidence['event_id']
                or not isinstance(evidence.get('digest'), str) or not evidence['digest']):
            raise ValueError('simulated final event evidence mismatch')
        state = self._robot_snapshot()
        order, receipt = state.get('order'), state.get('receipt')
        if (state.get('status') != 'stopped' or state['current_node'] != node
                or not isinstance(order, dict) or not isinstance(receipt, dict)
                or order.get('order_id') != task or order.get('map_id') != evidence['map_id']
                or order.get('seq') != evidence['seq'] or order.get('released_until') != node
                or not isinstance(order.get('nodes'), list) or not order['nodes'] or order['nodes'][-1] != node
                or receipt.get('new') is not True or receipt.get('state') != 'completed'
                or receipt.get('order_id') != task or receipt.get('seq') != evidence['seq']
                or receipt.get('digest') != evidence['digest']):
            raise ValueError('simulated stop and final event evidence mismatch')
        expected = self.positions[node]
        if (abs(state['pose']['x'] - expected[0]) > 0.01
                or abs(state['pose']['y'] - expected[1]) > 0.01):
            raise ValueError('simulated pose differs from stopped node')
        return copy.deepcopy(evidence)

    def _begin_action(self, name):
        task = self._pickup_task if name == 'grab' else self._delivery_task
        pending = {'order_id': task, 'action_id': uuid.uuid4().hex,
                   'agv_id': self.TWIN, 'action': name}
        if name == 'grab':
            pending['product_type'] = self._product
        else:
            pending.update(shelf_id=self._shelf, slot_index=self._slot)
        self._pending = pending
        self._status, self._phase = 'waiting', 'waiting_' + name

    def tick(self, dt_seconds):
        return self._tick(dt_seconds, pause_on_disconnect=False)

    def tick_recoverable(self, dt_seconds):
        """Fleet-only tick which can freeze a verifiably unambiguous outage."""
        return self._tick(dt_seconds, pause_on_disconnect=True)

    def _tick(self, dt_seconds, *, pause_on_disconnect):
        if type(dt_seconds) not in (int, float) or not math.isfinite(dt_seconds) or dt_seconds < 0:
            raise ValueError('elapsed time must be finite and nonnegative')
        with self.lock:
            if self._status not in ('moving', 'waiting'):
                return self.snapshot()
            if not self.control_ready:
                if pause_on_disconnect and self._pending is None:
                    try:
                        # The robot checkpoint rejects queued commands,
                        # ambiguous event publication, or any non-recoverable
                        # failure. It is a simulation-only in-process check.
                        if self.robot.ready:
                            self.robot.connected_recovery_checkpoint()
                        else:
                            self.robot.recovery_checkpoint()
                        if not self.suspend():
                            self._mark_unknown()
                    except Exception:
                        self._mark_unknown()
                else:
                    self._mark_unknown()
                return self.snapshot()
            if self._pending is not None:
                return self.snapshot()
            try:
                pickup = self._current_leg == 'pickup'
                workflow = self._pickup_workflow if pickup else self._delivery_workflow
                result = workflow.tick(dt_seconds)
                if not isinstance(result, dict) or result.get('status') == 'unknown':
                    raise RuntimeError('simulated workflow outcome unknown')
                if result.get('status') == 'waiting_reservation':
                    self._status, self._phase = 'waiting', 'reservation_wait'
                else:
                    self._status, self._phase = 'moving', 'to_pickup' if pickup else 'to_destination'
                if result.get('status') == 'pickup_reached':
                    task = self._pickup_task if pickup else self._delivery_task
                    node = self._pickup if pickup else self._destination
                    evidence = self._validate_finished(result.get('finished_evidence'), task, node)
                    if pickup:
                        self._pickup_evidence = evidence
                    else:
                        self._delivery_evidence = evidence
                    self._begin_action('grab' if pickup else 'place')
            except Exception:
                self._mark_unknown()
            return self.snapshot()

    def apply_action_result(self, order_id, action_id, agv_id, action, applied):
        with self.lock:
            expected = self._pending
            if (self._status != 'waiting' or expected is None or type(applied) is not bool
                    or any(expected.get(key) != value for key, value in (
                        ('order_id', order_id), ('action_id', action_id),
                        ('agv_id', agv_id), ('action', action)))):
                return False
            self._pending = None
            if not applied:
                self._status, self._phase = 'failed', 'failed'
                return True
            if action == 'place':
                try:
                    self._validate_finished(self._delivery_evidence, self._delivery_task, self._destination)
                    self._last_place_ack = {'order_id': order_id, 'action_id': action_id,
                                            'agv_id': agv_id, 'action': action, 'applied': True}
                    self._status, self._phase = 'completed', 'done'
                except Exception:
                    self._mark_unknown()
                return True
            try:
                evidence = self._validate_finished(self._pickup_evidence, self._pickup_task, self._pickup)
                ack = {'order_id': order_id, 'action_id': action_id, 'agv_id': agv_id,
                       'action': action, 'applied': True}
                adapter = SimHandoffCoordinatorStore(
                    self.runtime.store, target_task_id=self._delivery_task,
                    source_task_id=self._pickup_task, robot_id=self.ROBOT,
                    robot_session=self.SESSION, seq=evidence['seq'], stopped_at=self._pickup,
                    event_id=evidence['event_id'], action_ack=ack, expected_twin_id=self.TWIN,
                    gate=self.runtime.gate, progress=self.runtime.progress,
                    receiver=self.runtime.receiver, simulation_enabled=True)
                products = {self._product: {'pickup_node': self._destination}}
                self._delivery_workflow = PickupWorkflow(
                    self.runtime, self.robot, self.map, products, simulation_enabled=True,
                    task_id=self._delivery_task, session=self.SESSION, robot_id=self.ROBOT,
                    wait_for_reservation=self.wait_for_reservation,
                    action_at_end={'type': 'unload'}, accept_preceding=(self._pickup_task, self._pickup),
                    prepare_options={'handoff_store': adapter})
                result = self._delivery_workflow.submit(self._product)
                if result.get('status') == 'unknown':
                    raise RuntimeError('delivery allocation outcome unknown')
                self._status, self._phase = 'moving', 'to_destination'
                self._current_leg = 'delivery'
            except Exception:
                self._mark_unknown()
            return True
