"""Explicit slot-aware protocol for an isolated team-shelf twin experiment.

The legacy Unreal client does not implement this message type; it must not
fall back to ``robot.action`` because that older grab ignores the source slot.
No physical robot or existing web runtime is connected by this module.
"""
import json
import math
from pathlib import Path
import secrets
import threading
import time

from server.team_shelf_order_slots import TeamShelfOrderSlots


class TeamShelfTwinSession:
    """Correlate each simulated action and slot to one world and owner.

    ``arrival_guard`` belongs to the trusted simulation caller and must verify
    its own runtime's current stopped position and task before returning True.
    This class does not itself prove arrival, authenticate the owner, perform
    WebSocket I/O, or authorize physical movement.
    """

    def __init__(self, slots, session, world_id, owner, arrival_guard, *, timeout=10.0):
        if (type(slots) is not TeamShelfOrderSlots or type(session) is not str or not session
                or type(world_id) is not str or not world_id or owner is None
                or not callable(arrival_guard) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 120):
            raise ValueError('explicit simulated world, owner and arrival guard required')
        with slots._write() as db:
            slots._active(db, session)
            if db.execute('SELECT world_id FROM meta WHERE singleton=1').fetchone() != (world_id,):
                raise ValueError('simulation world differs from the bound slot session')
        data = json.loads((Path(__file__).resolve().parent / 'examples/mat_ssafy_v3.json')
                          .read_text(encoding='utf-8'))
        self._nodes = {node['id']: (node['x'], node['y']) for node in data['nodes']}
        self.slots, self.session, self.world_id = slots, session, world_id
        self.owner, self.arrival_guard, self.timeout = owner, arrival_guard, float(timeout)
        self._pending = {}
        self._lock = threading.RLock()

    def begin(self, request_id, agv_id, action):
        with self._lock:
            if self.owner is None or agv_id not in ('AGV_01', 'AGV_02') or action not in ('pick', 'place'):
                raise ValueError('active twin owner, simulated robot and valid action required')
            self._expire()
            if self.owner is None:
                raise ValueError('twin session expired')
            if any(row['data']['agv_id'] == agv_id for row in self._pending.values()):
                raise ValueError('simulated robot already has a pending action')
            order = self.slots.get(request_id)
            if order['state'] != ('reserved' if action == 'pick' else 'carried'):
                raise ValueError('slot order is not ready for that action')
            prefix = 'source' if action == 'pick' else 'destination'
            node, shelf, slot = (order[prefix + '_node'], order[prefix + '_shelf'],
                                 order[prefix + '_slot_id'])
            if self.arrival_guard(request_id, agv_id, action, node) is not True:
                raise ValueError('simulated robot arrival is not confirmed')
            x, y = self._nodes[node]
            action_id = secrets.token_hex(16)
            data = {'world_id': self.world_id, 'request_id': request_id, 'action_id': action_id,
                    'agv_id': agv_id, 'action': action, 'shelf_id': shelf, 'slot_id': slot,
                    'slot_index': (slot - 1) % 4, 'product_type': order['product_type'],
                    'expected_x': x, 'expected_y': y}
            # Commit the attempt before handing the message to any transport.
            self.slots.mark_dispatched(self.session, request_id, action, action_id)
            self._pending[action_id] = {'data': data, 'deadline': time.monotonic() + self.timeout}
            return {'type': 'team.shelf.action.v1', 'data': dict(data)}

    def receive(self, owner, message):
        return self.receive_with_effect(owner, message, lambda _data: True)

    def receive_with_effect(self, owner, message, apply_runtime_result):
        """Apply a correlated simulated runtime result before releasing slots.

        The runtime and slot journal are separate databases. If applying the
        result or subsequently writing the journal fails, unfinished orders
        are invalidated and their claims remain held. A caller still needs a
        fresh, authenticated owner and its own stop-evidence validation.
        """
        if not callable(apply_runtime_result):
            raise ValueError('explicit simulated runtime callback required')
        with self._lock:
            if owner is None or owner is not self.owner:
                return False
            self._expire()
            if self.owner is None or not isinstance(message, dict) or set(message) != {'type', 'data'}:
                return False
            data = message['data']
            if (message['type'] != 'team.shelf.action.result.v1' or not isinstance(data, dict)
                    or type(data.get('action_id')) is not str or type(data.get('applied')) is not bool):
                return False
            entry = self._pending.get(data['action_id'])
            if entry is None or set(data) != set(entry['data']) | {'applied'}:
                return False
            for key, expected in entry['data'].items():
                actual = data[key]
                if key in ('expected_x', 'expected_y'):
                    if type(actual) not in (int, float) or not math.isfinite(actual) or actual != expected:
                        return False
                elif type(actual) is not type(expected) or actual != expected:
                    return False
            try:
                if apply_runtime_result(dict(data)) is not True:
                    raise RuntimeError('simulated runtime did not accept action result')
                self.slots.complete_action(self.session, data['request_id'], data['action_id'], data['applied'])
            except BaseException as exc:
                try:
                    self.slots.end_session(self.session)
                except Exception as failure:
                    exc.add_note('slot-session invalidation failed: ' + type(failure).__name__)
                self.owner = None
                self._pending.clear()
                raise
            del self._pending[data['action_id']]
            return True

    def _expire(self):
        if self.owner is not None and any(time.monotonic() >= row['deadline'] for row in self._pending.values()):
            self.slots.end_session(self.session)
            self.owner = None
            self._pending.clear()

    def disconnect(self, owner):
        with self._lock:
            if owner is None or owner is not self.owner:
                return False
            self.slots.end_session(self.session)
            self.owner = None
            self._pending.clear()
            return True
