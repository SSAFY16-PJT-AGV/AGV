"""Explicit coordinate and inventory mapping for an isolated simulated twin.

This transformation is calibrated from caller-supplied synthetic node pairs;
it is not evidence that the team map matches a real world or Unreal scene.
"""
import copy
import hashlib
import json
import math
from pathlib import Path

from server.field_map_planner import plan_field_route
from server.routing import MapGraph


def _number(value, name, *, bound=1_000_000):
    if type(value) not in (int, float) or abs(value) > bound or not math.isfinite(value):
        raise ValueError('invalid ' + name)
    return float(value)


def _name(value, name):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
        raise ValueError('invalid ' + name)
    return value


def _fingerprint(data):
    raw = json.dumps(data, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode('ascii')).hexdigest()


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate configuration key')
        result[key] = value
    return result


class FieldTwinMapping:
    """Validate two map snapshots and bind a single similarity transform."""

    def __init__(self, field_map, unreal_map, config):
        field_map, unreal_map, config = copy.deepcopy((field_map, unreal_map, config))
        if not isinstance(config, dict):
            raise ValueError('explicit simulated mapping is required')
        keys = {'field_map_sha256', 'unreal_map_id', 'reflect_y', 'tolerance_m',
                'anchors', 'robots', 'products'}
        if set(config) != keys:
            raise ValueError('invalid simulated mapping fields')
        graph = MapGraph(field_map)
        if field_map.get('unit') != 'mm' or _fingerprint(field_map) != config['field_map_sha256']:
            raise ValueError('field map does not match the explicitly selected snapshot')
        self.map_ref = '{}@v{}'.format(graph.map_id, graph.version)
        if (not isinstance(unreal_map, dict) or unreal_map.get('coordinate_unit') != 'meter'
                or unreal_map.get('map_id') != _name(config['unreal_map_id'], 'Unreal map')):
            raise ValueError('Unreal map does not match the explicitly selected snapshot')
        if type(config['reflect_y']) is not bool:
            raise ValueError('reflection must be explicit')
        self.reflect_y = config['reflect_y']
        tolerance = _number(config['tolerance_m'], 'mapping tolerance', bound=.25)
        if tolerance <= 0:
            raise ValueError('mapping tolerance must be positive')
        self._field = {}
        for node in field_map['nodes']:
            key = _name(node.get('id'), 'field node')
            pos = node.get('pos')
            if key in self._field or not isinstance(pos, list) or len(pos) != 2:
                raise ValueError('invalid field nodes')
            self._field[key] = tuple(_number(x, 'field node position') for x in pos)
        self._unreal = {}
        if not isinstance(unreal_map.get('nodes'), list):
            raise ValueError('Unreal nodes required')
        for node in unreal_map['nodes']:
            key = _name(node.get('id'), 'Unreal node')
            if key in self._unreal:
                raise ValueError('duplicate Unreal node')
            self._unreal[key] = (_number(node.get('x'), 'Unreal x', bound=1000),
                                 _number(node.get('y'), 'Unreal y', bound=1000))
        anchors = config['anchors']
        if not isinstance(anchors, list) or not 2 <= len(anchors) <= 128:
            raise ValueError('at least two explicit mapping anchors are required')
        if any(not isinstance(a, dict) or set(a) != {'field_node', 'unreal_node'} for a in anchors):
            raise ValueError('invalid mapping anchor')
        try:
            pairs = [(self._field[a['field_node']], self._unreal[a['unreal_node']]) for a in anchors]
        except (KeyError, TypeError):
            raise ValueError('mapping anchor node is missing') from None
        if len({a['field_node'] for a in anchors}) != len(anchors) or len({a['unreal_node'] for a in anchors}) != len(anchors):
            raise ValueError('mapping anchors must be unique')
        (a, u), (b, v) = pairs[:2]
        dx, dy = b[0] - a[0], (b[1] - a[1]) * (-1 if self.reflect_y else 1)
        ux, uy = v[0] - u[0], v[1] - u[1]
        source, target = math.hypot(dx, dy), math.hypot(ux, uy)
        if source < 1e-6 or target < 1e-9:
            raise ValueError('distinct mapping anchor positions required')
        self._scale = target / source
        if not 1e-6 <= self._scale <= 1:
            raise ValueError('unreasonable simulated mapping scale')
        angle = math.atan2(uy, ux) - math.atan2(dy, dx)
        self._cos, self._sin, self._yaw = math.cos(angle), math.sin(angle), math.degrees(angle)
        self._source, self._target = a, u
        for point, target_point in pairs:
            self._require_near(point, target_point, tolerance)
        robots = config['robots']
        if not isinstance(robots, dict) or set(robots) != {'sim-r1', 'sim-r2'}:
            raise ValueError('exactly two isolated robot starting states required')
        for robot in robots.values():
            if not isinstance(robot, dict) or set(robot) != {'node', 'incoming_edge'}:
                raise ValueError('invalid robot starting state')
        if robots['sim-r1']['node'] == robots['sim-r2']['node']:
            raise ValueError('robot start nodes must be distinct')
        for robot in robots.values():
            route = plan_field_route(field_map, map_ref=self.map_ref,
                                     incoming_edge=robot['incoming_edge'], goal=robot['node'])
            if route.nodes != (robot['node'],):
                raise ValueError('incoming edge must end at robot start node')
        products = config['products']
        if not isinstance(products, list) or not 1 <= len(products) <= 128:
            raise ValueError('explicit simulated product mapping required')
        expected = {'id', 'label', 'product_type', 'pickup_node', 'destination_node',
                    'unreal_pickup_node', 'unreal_destination_node', 'shelf_id', 'slot_index', 'slot_count'}
        self.products, self.pickup_sources, self._pickup_types = [], {}, {}
        product_ids, field_pickups, product_types, shelf_ids = set(), set(), set(), set()
        for product in products:
            if not isinstance(product, dict) or set(product) != expected:
                raise ValueError('invalid simulated product mapping')
            for key in expected - {'slot_index', 'slot_count'}:
                _name(product[key], key)
            if (product['id'] in product_ids or product['pickup_node'] in field_pickups
                    or product['product_type'] in product_types or product['shelf_id'] in shelf_ids
                    or product['unreal_pickup_node'] in self.pickup_sources):
                raise ValueError('ambiguous product or pickup source')
            if (type(product['slot_count']) is not int or not 1 <= product['slot_count'] <= 128
                    or type(product['slot_index']) is not int
                    or not 0 <= product['slot_index'] < product['slot_count']):
                raise ValueError('invalid simulated shelf slots')
            try:
                self._require_near(self._field[product['pickup_node']], self._unreal[product['unreal_pickup_node']], tolerance)
                self._require_near(self._field[product['destination_node']], self._unreal[product['unreal_destination_node']], tolerance)
            except KeyError:
                raise ValueError('mapped product node is missing') from None
            product_ids.add(product['id'])
            field_pickups.add(product['pickup_node'])
            product_types.add(product['product_type'])
            shelf_ids.add(product['shelf_id'])
            self.pickup_sources[product['unreal_pickup_node']] = product['pickup_node']
            self._pickup_types[product['unreal_pickup_node']] = product['product_type']
            self.products.append({key: value for key, value in product.items()
                                  if key not in ('unreal_pickup_node', 'unreal_destination_node')})
        self.robots = robots
        self.identity = _fingerprint({'field_map': field_map, 'unreal_map': unreal_map, 'config': config})

    def _xy(self, x, y):
        dx = x - self._source[0]
        dy = (y - self._source[1]) * (-1 if self.reflect_y else 1)
        return (self._target[0] + self._scale * (self._cos * dx - self._sin * dy),
                self._target[1] + self._scale * (self._sin * dx + self._cos * dy))

    def _require_near(self, point, expected, tolerance):
        actual = self._xy(*point)
        if math.hypot(actual[0] - expected[0], actual[1] - expected[1]) > tolerance:
            raise ValueError('simulated map correspondence exceeds tolerance')

    def pose(self, pose):
        if not isinstance(pose, dict):
            raise ValueError('simulated field pose required')
        x = _number(pose.get('x'), 'pose x')
        y = _number(pose.get('y'), 'pose y')
        yaw = _number(pose.get('th'), 'pose yaw', bound=3600)
        x, y = self._xy(x, y)
        if not all(math.isfinite(v) and abs(v) <= 1000 for v in (x, y)):
            raise ValueError('transformed pose is outside simulated bounds')
        return {'x': x, 'y': y, 'yaw': (self._yaw + (-yaw if self.reflect_y else yaw) + 180) % 360 - 180}

    def pickups_to_field(self, pickups):
        if not isinstance(pickups, list) or len(pickups) > 128:
            raise ValueError('Unreal pickup inventory required')
        result, seen, matched = [], set(), set()
        for item in pickups:
            if not isinstance(item, dict) or set(item) != {'source_id', 'product_type', 'available'}:
                raise ValueError('invalid Unreal pickup source')
            source = _name(item['source_id'], 'pickup source')
            _name(item['product_type'], 'pickup product')
            count = item['available']
            if (source in seen or type(count) is not int or not 0 <= count <= 1000000):
                raise ValueError('invalid or duplicate Unreal pickup source')
            seen.add(source)
            if source not in self.pickup_sources:
                continue
            if item['product_type'] != self._pickup_types[source]:
                raise ValueError('Unreal pickup product differs from mapping')
            matched.add(source)
            result.append(dict(item, source_id=self.pickup_sources[source]))
        if matched != set(self.pickup_sources):
            raise ValueError('configured Unreal pickup source is missing')
        return result


def load_field_twin_mapping(path, unreal_map):
    """Read an explicit local field map and matching simulated correspondence."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('field mapping configuration is too large')
    config = json.loads(raw.decode('utf-8'), object_pairs_hook=_object_pairs)
    if not isinstance(config, dict) or not isinstance(config.get('field_map_file'), str):
        raise ValueError('explicit field map file is required')
    map_path = (path.parent / config.pop('field_map_file')).resolve()
    raw_map = map_path.read_bytes()
    if len(raw_map) > 4 * 1024 * 1024:
        raise ValueError('field map is too large')
    field_map = json.loads(raw_map.decode('utf-8'), object_pairs_hook=_object_pairs)
    return field_map, FieldTwinMapping(field_map, unreal_map, config)
