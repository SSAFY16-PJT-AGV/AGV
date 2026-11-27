"""Durable web order IDs bound to the same team-shelf slot journal.

The two databases are deliberately not presented as one transaction. An
interrupted dispatch or a mismatch blocks recovery and preserves claims.
"""
from server.order_persistence import PersistentOrderStore
from server.team_shelf_order_slots import TeamShelfOrderSlots


class TeamShelfPersistentOrders(PersistentOrderStore):
    def __init__(self, path, map_id, catalog, slots):
        if type(slots) is not TeamShelfOrderSlots:
            raise ValueError('explicit team shelf journal required')
        self.slots = slots
        super().__init__(path, map_id, catalog)
        self.bind_pickup_id(slots.creation_id)

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
