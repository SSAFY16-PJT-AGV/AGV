"""An in-process simulated world for the isolated web demo only.

This does not connect to Unreal or any physical robot. Its memory is lost on
process exit; a saved database must never be used to reconstruct this world.
"""
import copy
import logging
import math
import threading
import time


class SyntheticPeer:
    def __init__(self, actions, observer, products, initial_poses, world_id, *, field_mapping=None):
        if (not isinstance(world_id, str) or len(world_id) != 32
                or any(c not in '0123456789abcdef' for c in world_id)
                or set(initial_poses) != {'sim-r1', 'sim-r2'}):
            raise ValueError('a fresh isolated simulated world is required')
        self.actions, self.observer, self.world_id = actions, observer, world_id
        if field_mapping is not None:
            from server.field_twin_mapping import FieldTwinMapping
            if not isinstance(field_mapping, FieldTwinMapping):
                raise ValueError('a validated explicit synthetic field mapping is required')
            if products != field_mapping.products:
                raise ValueError('synthetic products differ from the explicit field mapping')
            field_to_world = {node: world for world, node in field_mapping.pickup_sources.items()}
            if (len(field_to_world) != len(field_mapping.pickup_sources)
                    or set(field_to_world) != {item['pickup_node'] for item in products}):
                raise ValueError('field pickup source mapping differs from product catalog')
        else:
            field_to_world = {item['pickup_node']: item['pickup_node'] for item in products}
        self._field_mapping = field_mapping
        self._map_ref = field_mapping.map_ref if field_mapping is not None else 'twin-validation-v1'
        self._stop = threading.Event()
        self._effect_lock = threading.Lock()
        self._thread = None
        self._robots, self._pose_at = {}, {}
        self._mapping = {'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}
        for robot, twin in self._mapping.items():
            pose = initial_poses[robot]
            if (not isinstance(pose, dict) or set(pose) != {'agv_id', 'x', 'y', 'yaw'}
                    or pose['agv_id'] != twin
                    or any(type(pose[k]) not in (int, float) or not math.isfinite(pose[k])
                           for k in ('x', 'y', 'yaw'))):
                raise ValueError('valid initial synthetic robot poses required')
            if field_mapping is not None:
                start = field_mapping.robots[robot]['node']
                x, y = field_mapping._field[start]
                expected = field_mapping.pose({'x': x, 'y': y, 'th': 0.})
                if any(abs(pose[key] - expected[key]) > 1e-9 for key in ('x', 'y', 'yaw')):
                    raise ValueError('synthetic initial pose differs from the explicit field mapping')
            self._robots[twin] = dict(pose, carrying=False, interpolating=False)
            self._pose_at[twin] = 0.
        self._products = {}
        self._sources = {}
        self._slots = {}
        for item in products:
            kind, source, shelf, size = (item['product_type'], field_to_world[item['pickup_node']],
                                         item['shelf_id'], item['slot_count'])
            if (kind in self._products or source in self._sources or shelf in self._slots
                    or any(type(v) is not str or not v for v in (kind, source, shelf))
                    or type(size) is not int or size != 6):
                raise ValueError('unambiguous six-slot synthetic product catalog required')
            self._products[kind], self._sources[source] = (source, shelf), kind
            self._slots[shelf] = [False] * size
        if not self._products:
            raise ValueError('synthetic product catalog required')
        self._stock = {source: 3 for source in self._sources}
        self._carrying = {twin: None for twin in self._robots}
        self._seen = set()

    def _refresh_poses(self):
        for robot, twin in self._mapping.items():
            state = self.observer.twin_observation(robot)
            seen = state.get('observation') if isinstance(state, dict) else None
            live = seen.get('state') if isinstance(seen, dict) else None
            pose = live.get('pose') if isinstance(live, dict) else None
            received = state.get('received_at') if isinstance(state, dict) else None
            if (not isinstance(state, dict) or state.get('connected') is not True
                    or state.get('ready') is not True or not isinstance(seen, dict)
                    or seen.get('state_received_live') is not True or not isinstance(live, dict)
                    or live.get('map_id') != self._map_ref or not isinstance(pose, dict)
                    or type(received) not in (int, float)
                    or not 0 <= time.monotonic() - received <= 1.
                    or any(type(pose.get(k)) not in (int, float) or not math.isfinite(pose[k])
                           for k in ('x', 'y', 'th'))):
                self._pose_at[twin] = 0.
                continue
            try:
                converted = (self._field_mapping.pose(pose) if self._field_mapping is not None else
                             {'x': pose['x'] / 1000, 'y': pose['y'] / 1000, 'yaw': pose['th']})
            except (ValueError, TypeError, KeyError, OverflowError):
                self._pose_at[twin] = 0.
                continue
            self._robots[twin].update(converted)
            self._pose_at[twin] = received

    def _snapshot(self, request_id):
        return {'type': 'shelf.snapshot', 'data': {
            'request_id': request_id, 'world_id': self.world_id,
            'robots': copy.deepcopy(list(self._robots.values())),
            'shelves': [{'shelf_id': shelf, 'product_type': kind, 'slots': list(self._slots[shelf])}
                        for kind, (_, shelf) in self._products.items()],
            'pickups': [{'source_id': source, 'product_type': kind, 'available': self._stock[source]}
                        for source, kind in self._sources.items()]}}

    def _apply(self, action):
        if not isinstance(action, dict):
            raise ValueError('invalid synthetic action')
        name = action.get('action')
        expected = {'order_id', 'action_id', 'agv_id', 'action', 'result', 'expected_x', 'expected_y'}
        expected |= {'product_type'} if name == 'grab' else {'shelf_id', 'slot_index'} if name == 'place' else set()
        if (name not in ('grab', 'place') or set(action) != expected
                or action.get('result') != 'success'
                or any(type(action.get(key)) is not str or not action[key] or len(action[key]) > 128
                       for key in ('order_id', 'action_id', 'agv_id'))
                or action['action_id'] in self._seen or action['agv_id'] not in self._robots
                or any(type(action.get(k)) not in (int, float) or not math.isfinite(action[k])
                       for k in ('expected_x', 'expected_y'))):
            raise ValueError('invalid or duplicate synthetic action')
        twin = action['agv_id']
        robot = self._robots[twin]
        if (not 0 <= time.monotonic() - self._pose_at[twin] <= 1.
                or math.hypot(robot['x'] - action['expected_x'], robot['y'] - action['expected_y']) > .25):
            raise ValueError('fresh synthetic position is unavailable')
        if name == 'grab':
            product = action['product_type']
            if product not in self._products or self._carrying[twin] is not None:
                raise ValueError('synthetic grab state differs')
            source, _ = self._products[product]
            if self._stock[source] < 1:
                raise ValueError('synthetic pickup is empty')
            self._seen.add(action['action_id'])
            self._stock[source] -= 1
            self._carrying[twin] = product
            robot['carrying'] = True
        else:
            shelf, slot = action['shelf_id'], action['slot_index']
            product = self._carrying[twin]
            if (product not in self._products or shelf != self._products[product][1]
                    or type(slot) is not int or not 0 <= slot < len(self._slots[shelf])
                    or self._slots[shelf][slot]):
                raise ValueError('synthetic place state differs')
            self._seen.add(action['action_id'])
            self._slots[shelf][slot] = True
            self._carrying[twin] = None
            robot['carrying'] = False
        return {'type': 'robot.action.result', 'data': {
            key: action[key] for key in ('order_id', 'action_id', 'agv_id', 'action')} | {'applied': True}}

    def _apply_after_observation(self, action):
        if (not isinstance(action, dict) or action.get('agv_id') not in self._robots
                or any(type(action.get(key)) not in (int, float) or not math.isfinite(action[key])
                       for key in ('expected_x', 'expected_y'))):
            raise ValueError('invalid synthetic action pose')
        twin = action['agv_id']
        while not self._stop.is_set():
            # Lock ordering matches DemoActionSession.poll()/receive(). Waiting
            # for the next broker observation always happens outside the guard.
            with self._effect_lock, self.actions.synthetic_effect_guard(self, action) as available:
                if self._stop.is_set() or not available or self.actions.can_dispatch() is not True:
                    return None
                self._refresh_poses()
                if (self._stop.is_set() or not self.actions.available_fast()
                        or self.actions.can_dispatch() is not True):
                    return None
                robot = self._robots[twin]
                age = time.monotonic() - self._pose_at[twin]
                if (0 <= age <= 1. and
                        math.hypot(robot['x'] - action['expected_x'], robot['y'] - action['expected_y']) <= .25):
                    # Observation reads may themselves block; recheck the
                    # original action deadline/owner immediately before the
                    # effect while keeping the same reentrant fences held.
                    with self.actions.synthetic_effect_guard(self, action) as current:
                        if self._stop.is_set() or not current:
                            return None
                        age = time.monotonic() - self._pose_at[twin]
                        if 0 <= age <= 1.:
                            return self._apply(action)
            self._stop.wait(.05)
        return None

    def start(self):
        if self._thread is not None or not self.actions.attach(self, True, source='synthetic'):
            raise RuntimeError('synthetic owner could not attach')
        self._thread = threading.Thread(target=self._run, name='synthetic-world', daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self.actions.detach(self)
            raise
        return self

    def _run(self):
        try:
            while not self._stop.is_set():
                self._refresh_poses()
                message = self.actions.poll(self)
                if message is not None:
                    kind, data = message.get('type'), message.get('data')
                    if kind == 'shelf.snapshot.request':
                        reply = self._snapshot(data['request_id'])
                    elif kind == 'robot.action':
                        reply = self._apply_after_observation(data)
                        if reply is None:
                            break
                    else:
                        raise ValueError('unexpected synthetic request')
                    if self._stop.is_set() or not self.actions.receive(self, reply):
                        raise RuntimeError('synthetic reply was not accepted')
                self._stop.wait(.05)
        except Exception:
            logging.getLogger(__name__).exception('synthetic world stopped; outcome may be unknown')
        finally:
            self.actions.invalidate_owner(self)
            self.actions.detach(self)

    def stop(self):
        self._stop.set()
        self.actions.invalidate_owner(self)
        if self._thread is not None:
            self._thread.join()
