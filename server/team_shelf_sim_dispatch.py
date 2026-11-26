"""Connect a simulated 24-slot reservation to the existing MQTT navigator.

This adapter publishes only a simulated *navigation* order. It cannot perform
slot-specific pickup or placement: the current Unreal action protocol has no
source slot ID. PUBACK is not evidence of arrival, pickup, or physical safety.
"""
import json
from pathlib import Path

from server.sim_runtime import SimRuntime
from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.field_map_planner import plan_field_route


class TeamShelfSimDispatch:
    """Keep both slot claims across DB boundaries and uncertain publication."""

    def __init__(self, slots, runtime):
        if type(slots) is not TeamShelfOrderSlots or type(runtime) is not SimRuntime:
            raise ValueError('explicit simulated slot journal and runtime are required')
        source = Path(__file__).resolve().parent / 'examples/mat_ssafy_v3.json'
        original = json.loads(source.read_text(encoding='utf-8'))
        if (runtime.host != '127.0.0.1' or not runtime._field_simulation
                or runtime.map_data != original):
            raise ValueError('original v3 loopback field simulation required')
        self.slots, self.runtime = slots, runtime

    def validate_candidate(self, task_id, candidate):
        """Check static binding and the explicit direction before reserving."""
        if (not isinstance(candidate, dict)
                or set(candidate) != {'robot_id', 'node', 'incoming_edge', 'available'}
                or not isinstance(candidate['robot_id'], str)
                or not candidate['robot_id'].startswith('sim-')
                or candidate['robot_id'] == 'sim-' or candidate['available'] is not True
                or type(task_id) is not str or not task_id):
            raise ValueError('one available simulated robot candidate is required')
        mapped = self.runtime._tasks.get(task_id)
        if mapped is None or mapped[0] != candidate['robot_id']:
            raise ValueError('task is not bound to the simulated robot')
        route = plan_field_route(self.runtime.map_data, map_ref='mat_ssafy_v3@v0',
                                 incoming_edge=candidate['incoming_edge'], goal=candidate['node'])
        if route.nodes != (candidate['node'],):
            raise ValueError('candidate direction does not terminate at its node')
        plan_field_route(self.runtime.map_data, map_ref='mat_ssafy_v3@v0',
                         incoming_edge=candidate['incoming_edge'], goal='D01')

    def reserve_and_publish_pickup(self, session, request_id, product_type, task_id, candidate):
        """Reserve a pair and publish at most one pickup-navigation attempt."""
        self.validate_candidate(task_id, candidate)
        self.slots.reserve(session, request_id, product_type)
        return self.publish_reserved_pickup(session, request_id, task_id, candidate)

    def publish_reserved_pickup(self, session, request_id, task_id, candidate):
        """Publish navigation for an existing pair after prior validation.

        The slot journal and runtime DB are separate: on any failure or lost
        response the order becomes unknown and neither slot is released. A
        restart also turns unfinished orders unknown. The caller must not
        interpret the return value as a pickup action or an Unreal result.
        """
        self.validate_candidate(task_id, candidate)
        order = self.slots.get(request_id)
        self.slots.begin_navigation(session, request_id, task_id)
        try:
            self.runtime.create_task(task_id)
            navigation = self.runtime.prepare_field(task_id, order['source_node'], [candidate], {'type': 'none'})
            ack = self.runtime.publish(task_id)
            if not isinstance(ack, dict) or ack.get('task_id') != task_id or ack.get('state') != 'acked':
                raise RuntimeError('simulation broker acknowledgement is unavailable')
            self.slots.finish_navigation(session, request_id, task_id, published=True)
        except BaseException as exc:
            try:
                self.slots.finish_navigation(session, request_id, task_id, published=False)
            except Exception as record_error:
                exc.add_note('slot journal could not record uncertain navigation: ' + type(record_error).__name__)
            raise
        return {'slots': order, 'navigation': navigation, 'broker_ack': ack}
