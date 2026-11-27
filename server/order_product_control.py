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
from server.field_map_planner import plan_field_route
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
                 wait_for_reservation=False, defer_delivery_handoff=False, reroute_session=None):
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
        if type(defer_delivery_handoff) is not bool:
            raise ValueError('deferred handoff must be explicit')
        self.defer_delivery_handoff = defer_delivery_handoff
        self.runtime, self.robot = runtime, robot
        self.reroute_session = reroute_session
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
        self._deferred_grab = None

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
                    'product_type': self._product, 'shelf_id': self._shelf, 'slot_index': self._slot,
                    'route': list(self._route),
                    'current_node': current, 'pose': pose, 'battery': 0.8,
                    'pending_action': copy.deepcopy(self._pending)}

    def export_completed_checkpoint(self):
        """Return a completed simulation checkpoint for an independent journal.

        Intermediate and uncertain actions cannot be exported for automatic
        resumption. This observation is not a live Unreal-world proof.
        """
        with self.lock:
            if self._status != 'completed' or self._phase != 'done' or self._pending is not None:
                raise ValueError('only a confirmed completed simulation may be checkpointed')
            evidence = self._validate_finished(self._delivery_evidence, self._delivery_task, self._destination)
            ack = copy.deepcopy(self._last_place_ack)
            if (not isinstance(ack, dict) or set(ack) != {'order_id', 'action_id', 'agv_id', 'action', 'applied'}
                    or ack['order_id'] != self._delivery_task or ack['agv_id'] != self.TWIN
                    or ack['action'] != 'place' or ack['applied'] is not True):
                raise ValueError('confirmed simulated place acknowledgement required')
            _id(ack['action_id'], 'action_id')
            return {'version': 1, 'robot_id': self.ROBOT, 'twin_id': self.TWIN,
                    'session': self.SESSION, 'task_prefix': self.task_prefix, 'cycle': self._cycle,
                    'pickup_task': self._pickup_task, 'delivery_task': self._delivery_task,
                    'product': self._product, 'pickup': self._pickup, 'destination': self._destination,
                    'shelf': self._shelf, 'slot': self._slot, 'route': copy.deepcopy(self._route),
                    'evidence': evidence, 'action_ack': ack}

    def restore_completed_checkpoint(self, checkpoint):
        """Rebuild only a finished simulated delivery after durable DB checks.

        Callers must separately verify the current Unreal world, actor pose and
        carried item before enabling ticks, new orders or action publication.
        """
        with self.lock:
            if (self._status != 'idle' or self._cycle != 0 or self._pending is not None
                    or not isinstance(checkpoint, dict)):
                raise ValueError('fresh idle controller and completed checkpoint required')
            keys = {'version', 'robot_id', 'twin_id', 'session', 'task_prefix', 'cycle',
                    'pickup_task', 'delivery_task', 'product', 'pickup', 'destination',
                    'shelf', 'slot', 'route', 'evidence', 'action_ack'}
            if (set(checkpoint) != keys or type(checkpoint['version']) is not int or checkpoint['version'] != 1
                    or checkpoint['robot_id'] != self.ROBOT or checkpoint['twin_id'] != self.TWIN
                    or checkpoint['session'] != self.SESSION or checkpoint['task_prefix'] != self.task_prefix
                    or type(checkpoint['cycle']) is not int or not 1 <= checkpoint['cycle'] <= self.MAX_ORDERS):
                raise ValueError('simulation checkpoint identity differs')
            cycle = checkpoint['cycle']
            prefix = self.task_prefix if cycle == 1 else '{}-{}'.format(self.task_prefix, cycle)
            if (checkpoint['pickup_task'] != prefix + '-pickup'
                    or checkpoint['delivery_task'] != prefix + '-delivery'):
                raise ValueError('simulation checkpoint cycle differs from task identity')
            if not isinstance(checkpoint['product'], str):
                raise ValueError('simulation checkpoint product is invalid')
            product = self.products.get(checkpoint['product'])
            slot = checkpoint['slot']
            if (not isinstance(product, dict) or product.get('pickup_node') != checkpoint['pickup']
                    or product.get('destination_node') != checkpoint['destination']
                    or product.get('shelf_id') != checkpoint['shelf'] or type(slot) is not int
                    or not ((type(product.get('slot_count')) is int and 0 <= slot < product['slot_count'])
                            or (product.get('slot_count') is None and product.get('slot_index') == slot))):
                raise ValueError('simulation checkpoint product differs from catalog')
            route, evidence, ack = checkpoint['route'], checkpoint['evidence'], checkpoint['action_ack']
            if (not isinstance(route, list) or not route or route[-1] != checkpoint['destination']
                    or any(not isinstance(node, str) or node not in self.positions for node in route)
                    or not isinstance(evidence, dict) or not isinstance(ack, dict)
                    or set(evidence) != {'task_id', 'session', 'robot_id', 'seq', 'stopped_at',
                                         'event_id', 'map_id', 'digest'}
                    or set(ack) != {'order_id', 'action_id', 'agv_id', 'action', 'applied'}
                    or evidence.get('task_id') != checkpoint['delivery_task']
                    or evidence.get('stopped_at') != checkpoint['destination']
                    or evidence.get('session') != self.SESSION or evidence.get('robot_id') != self.ROBOT
                    or type(evidence.get('seq')) is not int or evidence['seq'] < 1
                    or evidence.get('map_id') != '{}@v{}'.format(self.graph.map_id, self.graph.version)
                    or not isinstance(evidence.get('event_id'), str) or not evidence['event_id']
                    or not isinstance(evidence.get('digest'), str) or not evidence['digest']
                    or ack.get('order_id') != checkpoint['delivery_task'] or ack.get('agv_id') != self.TWIN
                    or not isinstance(ack.get('action_id'), str) or not ack['action_id']
                    or ack.get('action') != 'place' or ack.get('applied') is not True):
                raise ValueError('simulation checkpoint completion evidence differs')
            restored = self.runtime.restore_completed_executor(
                self.robot.executor, task_id=checkpoint['delivery_task'], session=self.SESSION,
                event_id=evidence.get('event_id'), action_ack=ack, expected_twin_id=self.TWIN)
            if restored != evidence:
                raise ValueError('checkpoint differs from verified database evidence')
            self._validate_finished(restored, checkpoint['delivery_task'], checkpoint['destination'])
            self._cycle, self._status, self._phase = cycle, 'completed', 'done'
            self._pickup_task, self._delivery_task = checkpoint['pickup_task'], checkpoint['delivery_task']
            self._product, self._pickup, self._destination = checkpoint['product'], checkpoint['pickup'], checkpoint['destination']
            self._shelf, self._slot, self._route = checkpoint['shelf'], slot, copy.deepcopy(route)
            self._delivery_evidence, self._last_place_ack = copy.deepcopy(evidence), copy.deepcopy(ack)
            return self.snapshot()

    def create_order(self, pickup_node, destination_node):
        raise RuntimeError('direct node orders are not supported in reservation mode')

    def _field_route_to(self, node):
        current, incoming = self.robot.executor.field_stop_direction()
        route = plan_field_route(self.map, map_ref='{}@v{}'.format(self.graph.map_id, self.graph.version),
                                 incoming_edge=incoming, goal=node)
        if route.nodes[0] != current:
            raise ValueError('field direction differs from simulated current node')
        return route

    def _plan_product_legs(self, current, pickup, destination):
        if getattr(self.runtime, '_field_simulation', False) is True:
            first = self._field_route_to(pickup)
            if first.nodes[0] != current or not first.edge_ids:
                raise ValueError('field pickup route must start at a verified stop')
            second = plan_field_route(self.map, map_ref='{}@v{}'.format(self.graph.map_id, self.graph.version),
                                      incoming_edge=first.edge_ids[-1], goal=destination)
            if not second.nodes or second.nodes[0] != pickup:
                raise ValueError('field delivery direction differs from pickup')
            return first, second
        return self.graph.shortest_path(current, pickup), self.graph.shortest_path(pickup, destination)

    def create_product_order(self, product_type, pickup, destination, shelf, slot):
        with self.lock:
            if self._status not in ('idle', 'completed') or self._cycle >= self.MAX_ORDERS:
                raise RuntimeError('simulated product order unavailable')
            expected = self.products.get(product_type)
            count = expected.get('slot_count') if isinstance(expected, dict) else None
            valid_slot = (type(slot) is int and
                          ((type(count) is int and 1 <= count <= 128 and 0 <= slot < count)
                           or (count is None and expected.get('slot_index') == slot))) if isinstance(expected, dict) else False
            if (not isinstance(expected, dict) or expected.get('pickup_node') != pickup
                    or expected.get('destination_node') != destination
                    or expected.get('shelf_id') != shelf or not valid_slot):
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
                first, second = self._plan_product_legs(state['current_node'], pickup, destination)
                if len(first.nodes) < 2 or len(second.nodes) < 2:
                    raise ValueError('each simulated leg must contain an edge')
            except ValueError:
                raise
            self._route = list(first.nodes) + list(second.nodes[1:])
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
                    reroute_guard = getattr(self._delivery_workflow, 'reroute_handoff_guard', None)
                    if reroute_guard is None:
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
                        simulation_enabled=True, reroute_guard=reroute_guard)
                    options = {'accept_preceding': (source, stop), 'prepare_options': {'handoff_store': adapter}}
                self._pickup_workflow = PickupWorkflow(
                    self.runtime, self.robot, self.map, self.products,
                    simulation_enabled=True, task_id=self._pickup_task, session=self.SESSION,
                    robot_id=self.ROBOT, wait_for_reservation=self.wait_for_reservation,
                    reroute_session=self.reroute_session, **options)
                result = self._pickup_workflow.submit(product_type)
                if result.get('status') == 'unknown':
                    self._mark_unknown()
            except Exception:
                self._mark_unknown()
            return self.snapshot()

    def _mark_unknown(self):
        self._status, self._phase, self._pending = 'unknown', 'unknown', None
        self._suspended_state = None
        self._deferred_grab = None

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
        executor = getattr(self.robot, 'executor', None)
        recovered = (state.get('recovered') is True and callable(getattr(executor, 'matches_recovered', None))
                     and executor.matches_recovered(evidence))
        if (state.get('status') != 'stopped' or state['current_node'] != node
                or not isinstance(order, dict) or not isinstance(receipt, dict)
                or order.get('order_id') != task or order.get('map_id') != evidence['map_id']
                or order.get('seq') != evidence['seq'] or order.get('released_until') != node
                or not isinstance(order.get('nodes'), list) or not order['nodes'] or order['nodes'][-1] != node
                or (receipt.get('new') is not True and not recovered) or receipt.get('state') != 'completed'
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
            if self._deferred_grab is not None:
                ack = self._deferred_grab
                self._deferred_grab = None
                self._start_delivery(ack)
                return self.snapshot()
            try:
                pickup = self._current_leg == 'pickup'
                workflow = self._pickup_workflow if pickup else self._delivery_workflow
                result = workflow.tick(dt_seconds)
                if not isinstance(result, dict) or result.get('status') == 'unknown':
                    raise RuntimeError('simulated workflow outcome unknown')
                if result.get('status') == 'waiting_reservation':
                    self._status, self._phase = 'waiting', 'reservation_wait'
                elif result.get('status') == 'rerouting':
                    motion = result.get('reroute_motion')
                    if motion not in ('waiting', 'moving'):
                        raise ValueError('virtual reroute motion state is unavailable')
                    self._status = motion
                    self._phase = 'reroute_moving' if motion == 'moving' else 'reroute_wait'
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
            ack = {'order_id': order_id, 'action_id': action_id,
                   'agv_id': agv_id, 'action': action, 'applied': True}
            if self.defer_delivery_handoff:
                self._deferred_grab = ack
                self._status, self._phase = 'waiting', 'delivery_pending'
                return True
            self._start_delivery(ack)
            return True

    def _start_delivery(self, ack):
        """Begin the next simulated leg after the caller's action fence."""
        try:
            evidence = self._validate_finished(self._pickup_evidence, self._pickup_task, self._pickup)
            adapter = SimHandoffCoordinatorStore(
                self.runtime.store, target_task_id=self._delivery_task,
                source_task_id=self._pickup_task, robot_id=self.ROBOT,
                robot_session=self.SESSION, seq=evidence['seq'], stopped_at=self._pickup,
                event_id=evidence['event_id'], action_ack=ack, expected_twin_id=self.TWIN,
                gate=self.runtime.gate, progress=self.runtime.progress,
                receiver=self.runtime.receiver, simulation_enabled=True,
                reroute_guard=getattr(self._pickup_workflow, 'reroute_handoff_guard', None))
            products = {self._product: {'pickup_node': self._destination}}
            self._delivery_workflow = PickupWorkflow(
                self.runtime, self.robot, self.map, products, simulation_enabled=True,
                task_id=self._delivery_task, session=self.SESSION, robot_id=self.ROBOT,
                wait_for_reservation=self.wait_for_reservation,
                reroute_session=self.reroute_session,
                action_at_end={'type': 'unload'}, accept_preceding=(self._pickup_task, self._pickup),
                prepare_options={'handoff_store': adapter})
            result = self._delivery_workflow.submit(self._product)
            if result.get('status') == 'unknown':
                raise RuntimeError('delivery allocation outcome unknown')
            self._status, self._phase = 'moving', 'to_destination'
            self._current_leg = 'delivery'
        except Exception:
            self._mark_unknown()
