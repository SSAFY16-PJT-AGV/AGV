"""Fixed six-shelf catalog for an opt-in, nonphysical v3 world."""
import hashlib
import json

from server.field_twin_mapping import load_field_twin_mapping
from server.team_shelf_order_slots import PRODUCT_SHELVES


def load_team_shelf_mapping(path, unreal_map):
    field, mapping = load_field_twin_mapping(path, unreal_map)
    if mapping.map_ref != 'mat_ssafy_v3@v0':
        raise ValueError('team shelf mode requires the v3 simulation map')
    # The slot protocol uses Unreal's v3 metre coordinates verbatim. Verify all
    # stops, not only the two anchors, before enabling this specific mode.
    for i in range(1, 7):
        node = 'D%02d' % i
        mapping._require_near(mapping._field[node], mapping._unreal[node], .000001)
        converted = mapping.pose({'x': mapping._field[node][0], 'y': mapping._field[node][1], 'th': 0})
        if abs(converted['x'] - mapping._unreal[node][0]) > .000001 or abs(converted['y'] - mapping._unreal[node][1]) > .000001:
            raise ValueError('team shelf positions differ from slot protocol')
    labels = {'milk': '우유', 'tissue': '휴지', 'egg': '달걀', 'juice': '주스'}
    mapping.products = [{'id': product, 'label': labels[product], 'product_type': product,
                         'pickup_node': 'D01', 'destination_node': 'D' + shelf[1:],
                         'shelf_id': shelf, 'slot_index': 0, 'slot_count': 4}
                        for product, shelf in PRODUCT_SHELVES.items()]
    mapping.identity = hashlib.sha256((mapping.identity + json.dumps(mapping.products, sort_keys=True,
        ensure_ascii=True)).encode('ascii')).hexdigest()
    return field, mapping
