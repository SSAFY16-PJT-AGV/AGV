"""Durable web order IDs bound to the same team-shelf slot journal.

The two databases are deliberately not presented as one transaction. An
interrupted dispatch or a mismatch blocks recovery and preserves claims.
"""
import uuid

from server.order_persistence import PersistentOrderStore, _text
from server.team_shelf_order_slots import TeamShelfOrderSlots


class TeamShelfPersistentOrders(PersistentOrderStore):
    def __init__(self, path, map_id, catalog, slots):
        if type(slots) is not TeamShelfOrderSlots:
            raise ValueError('explicit team shelf journal required')
        self.slots = slots
        super().__init__(path, map_id, catalog)
        self.bind_pickup_id(slots.creation_id)

    def submit_approved(self, product_id, request_key, *, threshold):
        """Persist only an approval's identity, not its temporary authority."""
        _text(product_id, 'product ID')
        _text(request_key, 'idempotency key', 128)
        if product_id not in self.products or type(threshold) is not int or not 1 <= threshold <= 4:
            raise ValueError('invalid synthetic replenishment approval')
        fingerprint = 'synthetic-replenishment:{}:{}'.format(product_id, threshold)
        with self._write() as db:
            old = db.execute('SELECT request_id,product_id,state,result_json,error,fingerprint '
                             'FROM orders WHERE request_key=?', (request_key,)).fetchone()
            if old is not None:
                if old[5] != fingerprint:
                    raise ValueError('request key belongs to a different order')
                return self._order(old[:5])
            request_id = uuid.uuid4().hex
            db.execute('INSERT INTO orders(request_id,request_key,fingerprint,product_id,state) '
                       "VALUES (?,?,?,?,'queued')", (request_id, request_key, fingerprint, product_id))
            db.execute("INSERT INTO order_events(request_id,state) VALUES (?,'queued')", (request_id,))
            return self._order((request_id, product_id, 'queued', None, None), created=True)

    def get_approval(self, request_id):
        """Return durable approval parameters, or None for an ordinary order."""
        _text(request_id, 'request ID')
        with self._read() as db:
            row = db.execute('SELECT product_id,fingerprint FROM orders WHERE request_id=?',
                             (request_id,)).fetchone()
        if row is None:
            raise KeyError(request_id)
        product, fingerprint = row
        if fingerprint == 'product:' + product:
            return None
        for threshold in range(1, 5):
            if fingerprint == 'synthetic-replenishment:{}:{}'.format(product, threshold):
                return product, threshold
        raise ValueError('durable simulated order fingerprint is invalid')

    def _validate_assignment(self, db, request_id, product_id, result):
        product = self.products[product_id]
        claim = self.slots.get(request_id)
        if (result['reservation_id'] != request_id
                or claim['product_type'] != product['product_type']
                or claim['state'] != 'reserved'
                or claim['destination_shelf'] != product['shelf_id']
                or result['shelf_id'] != claim['destination_shelf']
                or result['slot_index'] != (claim['destination_slot_id'] - 1) % 4):
            raise ValueError('assigned order does not match its team slot claim')


def check_recovered_team_orders(journal, slots):
    """Cross-check independent journals; unknown work cannot auto-resume."""
    if type(journal) is not TeamShelfPersistentOrders or journal.slots is not slots:
        raise ValueError('bound team journals required')
    if journal.pickup_id != slots.creation_id:
        raise ValueError('team slot journal identity differs')
    orders = {row['request_id']: row for row in journal.list_orders()}
    saved = {row['request_id']: row for row in slots.inspect()['orders']}
    if set(saved) - set(orders):
        raise ValueError('team slot claim has no durable web order')
    blocked = False
    for request_id, order in orders.items():
        slot = saved.get(request_id)
        state = order['state']
        if state == 'unknown':
            blocked = True
            if slot is not None and slot['state'] != 'unknown':
                raise ValueError('interrupted order and team slot claim disagree')
        elif state == 'completed':
            result = order.get('result') or {}
            if (slot is None or slot['state'] != 'completed'
                    or slot['product_type'] != journal.products[order['product_id']]['product_type']
                    or result.get('reservation_id') != request_id
                    or result.get('shelf_id') != slot['destination_shelf']
                    or result.get('slot_index') != (slot['destination_slot_id'] - 1) % 4):
                raise ValueError('completed order and team slot claim disagree')
        elif state == 'queued':
            if slot is not None:
                raise ValueError('queued order already has a team slot claim')
        elif state == 'failed':
            if slot is not None and slot['state'] != 'failed':
                raise ValueError('failed order and team slot claim disagree')
        else:
            raise ValueError('unfinished order was not marked unknown')
    return not blocked


def check_recovered_team_snapshot(slots, world_id, observed):
    """Compare unclaimed recorded slots to the same live simulated world."""
    if (type(slots) is not TeamShelfOrderSlots or not isinstance(world_id, str)
            or not isinstance(slots.world_id, str) or slots.world_id.lower() != world_id.lower()
            or not isinstance(observed, list) or len(observed) != 24):
        raise ValueError('saved simulated world differs')
    rows = {}
    for row in observed:
        if (not isinstance(row, dict) or set(row) != {'slot_id', 'state', 'product_type'}
                or type(row['slot_id']) is not int or row['slot_id'] in rows):
            raise ValueError('invalid simulated slot snapshot')
        rows[row['slot_id']] = (row['state'], row['product_type'])
    saved = slots.inspect()
    claims = {row['slot_id'] for row in saved['claims']}
    for slot in saved['slots']:
        slot_id = slot['slot_id']
        if slot_id not in rows:
            raise ValueError('simulated slot is missing')
        if slot_id not in claims:
            if slot['state'] == 'unknown' or rows[slot_id] != (slot['state'], slot['product_type']):
                raise ValueError('simulated slot observation differs from saved state')
    return True


def check_completed_action_bindings(journal, slots, read_grab_ack):
    """Cross-check slot actions against durable fleet handoff and place ACK."""
    if type(journal) is not TeamShelfPersistentOrders or journal.slots is not slots or not callable(read_grab_ack):
        raise ValueError('bound team journals and fleet handoff reader required')
    for order in journal.list_orders():
        if order['state'] != 'completed':
            continue
        result = order.get('result') or {}
        checkpoint = result.get('checkpoint')
        bindings = slots.action_bindings(order['request_id'])
        if not isinstance(checkpoint, dict) or len(bindings) != 2:
            raise ValueError('completed team action binding is missing')
        by_phase = {row['phase']: row for row in bindings}
        if set(by_phase) != {'pick', 'place'} or any(row['state'] != 'applied' for row in bindings):
            raise ValueError('completed team actions are not confirmed')
        product = journal.products[order['product_id']]
        if (checkpoint.get('robot_id') != result.get('assigned_robot')
                or checkpoint.get('twin_id') not in ('AGV_01', 'AGV_02')
                or checkpoint.get('product') != product['product_type']
                or checkpoint.get('destination') != product['destination_node']
                or checkpoint.get('shelf') != result.get('shelf_id')
                or checkpoint.get('slot') != result.get('slot_index')):
            raise ValueError('team checkpoint and web assignment differ')
        for phase, task in (('pick', checkpoint.get('pickup_task')), ('place', checkpoint.get('delivery_task'))):
            if by_phase[phase]['order_id'] != task or by_phase[phase]['agv_id'] != checkpoint['twin_id']:
                raise ValueError('team action and robot checkpoint differ')
        pick, place = by_phase['pick'], by_phase['place']
        expected_grab = {'order_id': pick['order_id'], 'action_id': pick['action_id'],
                         'agv_id': pick['agv_id'], 'action': 'grab', 'applied': True}
        expected_place = {'order_id': place['order_id'], 'action_id': place['action_id'],
                          'agv_id': place['agv_id'], 'action': 'place', 'applied': True}
        if read_grab_ack(checkpoint) != expected_grab or checkpoint.get('action_ack') != expected_place:
            raise ValueError('team actions differ from durable fleet acknowledgements')
    return True


def check_completed_reroute_handoffs(journal, slots, db):
    """Cross-check every completed reroute handoff against both web journals.

    This is database consistency evidence for isolated simulation, not a
    signature or a live-world proof. The caller must also verify the same
    Unreal world, all slots, pose and carried state before permitting orders.
    """
    import json
    if not check_recovered_team_orders(journal, slots):
        raise ValueError('uncertain team order blocks reroute recovery')
    orders = [row for row in journal.list_orders() if row['state'] == 'completed']
    by_task = {}
    for row in orders:
        checkpoint = (row.get('result') or {}).get('checkpoint')
        if not isinstance(checkpoint, dict):
            raise ValueError('completed checkpoint is missing')
        for phase, key in (('pick', 'pickup_task'), ('place', 'delivery_task')):
            task = checkpoint.get(key)
            if not isinstance(task, str) or not task or task in by_task:
                raise ValueError('completed task binding is ambiguous')
            by_task[task] = (row, checkpoint, phase)

    def grab_ack(checkpoint):
        row = db.execute('SELECT target_task_id,robot_id,robot_session,ack_json '
                         'FROM sim_handoff_transfers WHERE source_task_id=?',
                         (checkpoint['pickup_task'],)).fetchone()
        if row is None or row[:3] != (checkpoint['delivery_task'], checkpoint['robot_id'], checkpoint['session']):
            raise ValueError('saved simulated pickup handoff differs')
        return json.loads(row[3])

    check_completed_action_bindings(journal, slots, grab_ack)
    rerouted = db.execute("SELECT g.task_id,t.status FROM sim_reroute_generations g "
                          "JOIN sim_reroute_finishes f ON f.task_id=g.task_id "
                          "JOIN tasks t ON t.task_id=g.task_id").fetchall()
    for task, status in rerouted:
        entry = by_task.get(task)
        if entry is None:
            raise ValueError('rerouted handoff has no completed team order')
        order, checkpoint, phase = entry
        binding = {row['phase']: row for row in slots.action_bindings(order['request_id'])}[phase]
        expected = {'order_id': task, 'action_id': binding['action_id'], 'agv_id': binding['agv_id'],
                    'action': 'grab' if phase == 'pick' else 'place', 'applied': True}
        if status == 'ASSIGNED':
            if phase != 'place' or checkpoint['action_ack'] != expected:
                raise ValueError('terminal rerouted delivery action differs')
            continue
        if status != 'DONE':
            raise ValueError('rerouted task is not a completed handoff')
        transfer = db.execute('SELECT target_task_id,robot_id,robot_session,ack_json '
                              'FROM sim_handoff_transfers WHERE source_task_id=?', (task,)).fetchone()
        if transfer is None or transfer[1:3] != (checkpoint['robot_id'], checkpoint['session']):
            raise ValueError('rerouted handoff robot or session differs')
        ack = json.loads(transfer[3])
        if ack != expected:
            raise ValueError('rerouted handoff action differs from team slot binding')
        if phase == 'pick':
            if transfer[0] != checkpoint['delivery_task']:
                raise ValueError('rerouted pickup target differs')
        else:
            target = by_task.get(transfer[0])
            if (ack != checkpoint['action_ack'] or target is None or target[2] != 'pick'
                    or target[1]['robot_id'] != checkpoint['robot_id']
                    or target[1]['session'] != checkpoint['session']):
                raise ValueError('rerouted delivery target differs')
    return True


class TeamShelfRerouteRecovery:
    """Bound startup validator; live Unreal verification remains mandatory."""
    def __init__(self, journal, slots):
        if type(journal) is not TeamShelfPersistentOrders or journal.slots is not slots:
            raise ValueError('bound durable team journals required')
        self.journal, self.slots = journal, slots

    def validate(self, store, db):
        from server.sim_handoff_store import SimHandoffStore
        if (type(store) is not SimHandoffStore or not store.reopened
                or self.journal.simulation_id != store.creation_id
                or not isinstance(self.journal.world_id, str)
                or not isinstance(self.slots.world_id, str)
                or self.journal.world_id.lower() != self.slots.world_id.lower()):
            raise ValueError('persistent simulation, world and team slots must match')
        return check_completed_reroute_handoffs(self.journal, self.slots, db)
