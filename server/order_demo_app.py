"""Separate, loopback-only web ordering demo for a synthetic AGV.

This process publishes simulated observations, never physical robot commands.
Launch it via the isolated demo launcher, not the production server.
"""
from contextlib import asynccontextmanager, ExitStack, nullcontext
import copy
import json
import logging
import os
from pathlib import Path
import threading
import time
import tempfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr
import paho.mqtt.client as mqtt

from server.mqtt_client import start_mqtt, stop_mqtt
from server.order_demo_actions import DemoActionSession
from server.order_demo_admission import (AdmissionBusy, AdmissionConflict, AdmissionOutcomeUnknown,
                                         AdmissionStopped, OrderAdmission)
from server.twin_websocket import router as twin_router

PRODUCTS = (
    {'id': 'red', 'label': '빨간 상품', 'product_type': 'Red', 'pickup_node': 'D01',
     'destination_node': 'D04', 'shelf_id': 'SHELF_RED', 'slot_index': 0},
    {'id': 'blue', 'label': '파란 상품', 'product_type': 'Blue', 'pickup_node': 'D06',
     'destination_node': 'D05', 'shelf_id': 'SHELF_BLUE', 'slot_index': 0},
)

# Labels for local simulated observations only; they do not authorize a real
# robot to move or assert its physical state.
SIMULATION_STATUS = {'moving': 'MOVING', 'waiting': 'WAITING', 'failed': 'ERROR',
                     'unknown': 'ERROR', 'idle': 'IDLE', 'completed': 'IDLE'}


class DemoRuntime:
    def __init__(self, engine, port, nodes, observer=None, *, snapshot_max_age=1.0, clock=time.monotonic,
                 auto_recovery=False):
        self.engine = engine
        self.port = port
        self._nodes = nodes
        self.observer = observer
        self.lock = threading.RLock()
        if not callable(clock) or not isinstance(snapshot_max_age, (int, float)) or snapshot_max_age <= 0:
            raise ValueError('a positive snapshot maximum age and clock are required')
        self.clock = clock
        self.snapshot_max_age = snapshot_max_age
        self._snapshot_lock = threading.Lock()
        try:
            self._cached_snapshot = copy.deepcopy(engine.snapshot())
        except Exception:
            self._cached_snapshot = {'status': 'unknown'}
        self._snapshot_at = self.clock()
        self._snapshot_valid = True
        self._cached_ready = False
        self.auto_recovery = auto_recovery is True
        self._recovering = threading.Event()
        self._recovery_checkpoints = None
        self._recovery_owner = None
        self.actions = DemoActionSession(engine, self.lock, can_dispatch=self._ready,
                                         on_change=self._invalidate_snapshot,
                                         pause_on_unavailable=self.auto_recovery)
        self.admission = OrderAdmission()
        self.connected = threading.Event()
        self.stop_event = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.thread = None

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            self.connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()

    def start(self):
        self.client.connect('127.0.0.1', self.port, keepalive=15)
        self.client.loop_start()
        if not self.connected.wait(10):
            self.client.loop_stop()
            self.client.disconnect()
            raise RuntimeError('isolated demo publisher failed to connect')
        self.thread = threading.Thread(target=self._run, name='order-demo', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self._invalidate_snapshot()
        self.admission.shutdown()
        # Never tear down shared MQTT/DB resources while an accepted request
        # is still using them; it remains unknown after shutdown.
        self.admission.join()
        if self.thread is not None:
            self.thread.join()
        self.client.disconnect()
        self.client.loop_stop()

    def nodes(self):
        return [dict(node) for node in self._nodes]

    def _ready(self):
        return (self._transport_ready()
                and (not self.auto_recovery or self.actions.available_fast()))

    def _transport_ready(self):
        if self.stop_event.is_set() or self._recovering.is_set() or not self.connected.is_set():
            return False
        try:
            if getattr(self.engine, 'control_ready', True) is not True:
                return False
        except Exception:
            return False
        if self.observer is None:
            return True
        try:
            state = self.observer.snapshot()
            return state.get('connected') is True and state.get('ready') is True
        except Exception:
            return False

    def _refresh_waiting_snapshot(self):
        # The launcher must be able to see broker readiness before starting
        # Unreal. This cached observation does not authorize any tick or order.
        with self.lock:
            state = self.engine.snapshot()
            states = state.get('robots', [state])
            if (not self.actions.available_fast() and self._transport_ready()
                    and all(item.get('status') in ('idle', 'completed') for item in states)):
                self._store_snapshot(state, True)

    def _tick_if_ready(self, elapsed):
        with self.lock:
            if self.auto_recovery:
                with self.actions.motion_guard() as owner_available:
                    if not owner_available or not self._ready():
                        return None
                    state = self.engine.tick(elapsed)
                    self._store_snapshot(state, self._ready())
                    return state
            if not self._ready():
                return None
            state = self.engine.tick(elapsed)
            self._store_snapshot(state, self._ready())
            return state
    def _invalidate_snapshot(self):
        with self._snapshot_lock:
            self._snapshot_valid = False

    def _store_snapshot(self, state, ready):
        snapshot = copy.deepcopy(state)
        with self._snapshot_lock:
            self._cached_snapshot = snapshot
            self._snapshot_at = self.clock()
            self._snapshot_valid = True
            self._cached_ready = ready is True

    def snapshot(self):
        # Observation must not wait for MQTT/DB operations held under the
        # control lock. Cached state is never used for dispatch or safety.
        acquired = self.lock.acquire(blocking=False)
        try:
            with self._snapshot_lock:
                result = copy.deepcopy(self._cached_snapshot)
                age = max(0.0, self.clock() - self._snapshot_at)
                stale = not acquired or not self._snapshot_valid or age > self.snapshot_max_age
                ready = self._cached_ready
        finally:
            if acquired:
                self.lock.release()
        result['stale'] = stale
        result['connected'] = not stale and ready and self.connected.is_set() and not self.stop_event.is_set()
        result['unreal_connected'] = not stale and self.actions.available_fast()
        if stale or not result['connected'] or not result['unreal_connected']:
            result['accepting_orders'] = False
        return result

    def create_order(self, pickup, destination):
        # The admission worker may wait for the control lock after its HTTP
        # caller receives a pending ID. The operation remains exactly-once.
        self.lock.acquire()
        try:
            with self.actions.motion_guard() if self.auto_recovery else nullcontext(True) as owner_available:
                if not owner_available or self.stop_event.is_set() or not self._ready():
                    raise ConnectionError('isolated broker unavailable')
                self._invalidate_snapshot()
                try:
                    result = self.engine.create_order(pickup, destination)
                    self._store_snapshot(self.engine.snapshot(), self._ready())
                    return result
                except Exception as exc:
                    raise AdmissionOutcomeUnknown('simulated order outcome is unknown') from exc
        finally:
            self.lock.release()

    def create_product_order(self, product_id):
        product = next((item for item in PRODUCTS if item['id'] == product_id), None)
        if product is None:
            raise ValueError('unknown product')
        self.lock.acquire()
        try:
            with self.actions.motion_guard() if self.auto_recovery else nullcontext(True) as owner_available:
                if (not owner_available or self.stop_event.is_set() or not self._ready()
                        or not self.actions.available()):
                    raise ConnectionError('authenticated Unreal connection unavailable')
                self._invalidate_snapshot()
                try:
                    result = self.engine.create_product_order(product['product_type'], product['pickup_node'],
                                                              product['destination_node'], product['shelf_id'],
                                                              product['slot_index'])
                    self._store_snapshot(self.engine.snapshot(), self._ready())
                    return result
                except Exception as exc:
                    raise AdmissionOutcomeUnknown('simulated order outcome is unknown') from exc
        finally:
            self.lock.release()

    def _submit(self, operation, fingerprint, request_key=None):
        # A request accepted by admission is never silently cancelled or
        # retried. The operation itself rejects before mutation if busy.
        return self.admission.submit(operation, fingerprint, request_key=request_key)

    def submit_product_order(self, product_id, request_key=None):
        if not any(item['id'] == product_id for item in PRODUCTS):
            raise ValueError('unknown product')
        return self._submit(lambda: self.create_product_order(product_id),
                            'product:' + product_id, request_key)

    def submit_order(self, pickup, destination, request_key=None):
        return self._submit(lambda: self.create_order(pickup, destination),
                            json.dumps(['nodes', pickup, destination], ensure_ascii=True), request_key)

    def _enter_recovery(self):
        with self.lock:
            if not self._recovering.is_set():
                if (not self.actions.available_fast() and
                        all(item.snapshot()['status'] in ('idle', 'completed')
                            for item in self.engine.engines.values())):
                    return
                self._recovering.set()
                self._recovery_owner = self.actions.owner if self.actions.available_fast() else None
                uncertain = self.actions.action_id if self.actions.sent_at is not None else None
                self.engine.suspend(uncertain)
                if self._recovery_owner is None:
                    self.engine.action_unknown()
                self.actions.action_id = self.actions.observed_at = self.actions.sent_at = None
                self._invalidate_snapshot()

    def _try_recovery(self):
        """Recover only this running fleet's frozen, in-memory simulation."""
        from server.sim_order_authorization import SimOrderAuthorizer
        with self.lock:
            if self.stop_event.is_set():
                return False
            if (self._recovery_owner is None or not self.actions.available_fast()
                    or self.actions.owner is not self._recovery_owner):
                self.engine.action_unknown()
                return False
            engines = self.engine.engines
            # An ambiguous in-flight Unreal action or workflow remains unknown.
            if any(item.snapshot()['status'] in ('unknown', 'failed') for item in engines.values()):
                return False
            if self._recovery_checkpoints is None:
                self._recovery_checkpoints = {
                    key: (item.robot.connected_recovery_checkpoint() if item.robot.ready
                          else item.robot.recovery_checkpoint()) for key, item in engines.items()}
            checkpoints = self._recovery_checkpoints
            runtime = next(iter(engines.values())).runtime
            if any(item.runtime is not runtime for item in engines.values()):
                raise RuntimeError('fleet runtime changed during recovery')
            if not runtime.status()['ready']:
                runtime.reconnect()
            if self.stop_event.is_set():
                return False
            # Freeze and revoke both robots' previous proofs even when only
            # one transport dropped; each active segment is then revalidated.
            for key in engines:
                runtime.invalidate_robot(key)
            for key, item in engines.items():
                if not item.robot.ready:
                    authorizer = SimOrderAuthorizer(runtime, robot_id=key)
                    item.robot.reconnect(authorization_guard=authorizer.guard)
                if self.stop_event.is_set():
                    return False
            if not self.connected.is_set():
                self.client.loop_stop()
                if self.client.reconnect() != mqtt.MQTT_ERR_SUCCESS:
                    raise ConnectionError('demo publisher reconnect failed')
                self.client.loop_start()
                if not self.connected.wait(5):
                    raise ConnectionError('demo publisher reconnect timed out')
            if self.observer is not None:
                observation = self.observer.snapshot()
                if not observation.get('connected') or not observation.get('ready'):
                    return False
            if (self.stop_event.is_set() or not self.actions.available_fast()
                    or self.actions.owner is not self._recovery_owner):
                self.engine.action_unknown()
                return False
            if any(not item.robot.ready or not item.robot.check_recovery_checkpoint(checkpoints[key])
                   for key, item in engines.items()) or not runtime.status()['ready']:
                return False
            for key, item in engines.items():
                if not item.robot.check_recovery_checkpoint(checkpoints[key]):
                    raise RuntimeError('simulated checkpoint changed during recovery')
                state = item.robot.executor.snapshot()
                if item.snapshot()['status'] == 'suspended':
                    order = state.get('order')
                    if state.get('status') != 'moving' or not isinstance(order, dict):
                        raise RuntimeError('only confirmed in-progress segments can resume automatically')
                    runtime.reconcile_moving(key, order['order_id'], item.SESSION, item.robot, checkpoints[key])
            if (self.stop_event.is_set() or not self.actions.available_fast()
                    or self.actions.owner is not self._recovery_owner):
                self.engine.action_unknown()
                return False
            if any(not item.robot.ready or not item.robot.check_recovery_checkpoint(checkpoints[key])
                   for key, item in engines.items()) or not runtime.status()['ready']:
                return False
            if not self.engine.resume_after_reconnect():
                raise RuntimeError('fleet state could not resume')
            if (self.stop_event.is_set() or not self.actions.available_fast()
                    or self.actions.owner is not self._recovery_owner):
                self.engine.action_unknown()
                return False
            self._recovery_checkpoints = None
            self._recovery_owner = None
            self._recovering.clear()
            return True

    def _run(self):
        try:
            previous = time.monotonic()
            while not self.stop_event.is_set():
                now = time.monotonic()
                if self._ready():
                    state = self._tick_if_ready(min(now - previous, 0.5))
                    if state is None:
                        previous = now
                        continue
                    states = state.get('robots') if state.get('mode') == 'fleet' else [dict(state, robot_id='sim-r1')]
                    if (not isinstance(states, list) or not states or
                            any(item.get('robot_id') not in ('sim-r1', 'sim-r2') for item in states)):
                        raise ValueError('invalid isolated robot states')
                    for item in states:
                        payload = {'map_id': 'twin-validation-v1',
                                   'last_node': item['current_node'],
                                   'status': SIMULATION_STATUS.get(item['status'], 'ERROR'),
                                   'battery': item['battery'], 'pose': item['pose']}
                        result = self.client.publish('agv/{}/state'.format(item['robot_id']), json.dumps(payload),
                                                     qos=0, retain=False)
                        if result.rc != mqtt.MQTT_ERR_SUCCESS:
                            self.connected.clear()
                            if self.auto_recovery:
                                self._enter_recovery()
                            else:
                                with self.lock:
                                    self.engine.action_unknown()
                            break
                else:
                    if self.auto_recovery:
                        if not self.actions.available_fast():
                            self._refresh_waiting_snapshot()
                        self._enter_recovery()
                        if self._recovering.is_set():
                            try:
                                self._try_recovery()
                            except Exception:
                                # A disconnected test-owned broker can take time to
                                # return. No tick or new order is allowed meanwhile.
                                logging.getLogger(__name__).debug('fleet recovery pending')
                    else:
                        with self.lock:
                            self.engine.action_unknown()
                            self._invalidate_snapshot()
                previous = now
                self.stop_event.wait(0.2)
        except Exception:
            logging.getLogger(__name__).error('isolated demo worker stopped')
            self.connected.clear()
            self._invalidate_snapshot()
            with self.lock:
                if hasattr(self.engine, 'action_unknown'):
                    self.engine.action_unknown()


@asynccontextmanager
async def lifespan(app):
    # Only an explicitly configured, isolated local demo can start this app.
    if (os.environ.get('AGV_ORDER_DEMO_ENABLED') != '1'
            or os.environ.get('PYTHON_DOTENV_DISABLED') != '1'
            or os.environ.get('MQTT_HOST') != '127.0.0.1'
            or any(os.environ.get(key) for key in ('MQTT_USERNAME', 'MQTT_PASSWORD', 'MQTT_TLS_CA'))):
        raise RuntimeError('explicit isolated local demo configuration required')
    port = int(os.environ['MQTT_PORT'])
    if not 1 <= port <= 65535:
        raise ValueError('invalid broker port')
    raw = Path(os.environ['AGV_ORDER_DEMO_MAP']).read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('demo map too large')
    map_data = json.loads(raw.decode('utf-8'))
    nodes = [{'id': node['id'], 'type': node.get('type', '')} for node in map_data['nodes']]
    with ExitStack() as cleanup:
        if os.environ.get('AGV_ORDER_COORDINATED') == '1':
            # A fresh, disposable DB and dedicated robot are only for the
            # launcher-owned isolated broker; never share them with hardware.
            from server.order_sim_map import make_order_sim_map
            from server.sim_handoff_store import SimHandoffStore
            from server.sim_runtime import SimRuntime
            from server.sim_order_authorization import SimOrderAuthorizer
            from server.order_product_control import ProductControlEngine
            from server.order_fleet import FleetProductControlEngine
            from sim.order_inbox import SimOrderInbox
            from sim.order_executor import SimOrderExecutor
            from sim.mqtt_order_robot import SimMqttOrderRobot
            folder = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix='agv-web-workflow-')))
            sim_map = make_order_sim_map(map_data)
            db_path = folder / 'server.sqlite3'
            fleet_enabled = os.environ.get('AGV_ORDER_FLEET') == '1'
            if fleet_enabled:
                starts = {'sim-r1': 'J01', 'sim-r2': 'J05'}
                if not set(starts.values()).issubset({node['id'] for node in sim_map['nodes']}):
                    raise ValueError('fleet demo start nodes are absent from the map')
                store = SimHandoffStore.create_new(db_path, simulation_enabled=True, idle_positions=starts)
            else:
                starts = {'sim-r1': sim_map['nodes'][0]['id']}
                store = SimHandoffStore.create_new(db_path, simulation_enabled=True)
            # Fixed bounded task/session bindings keep the existing event and
            # recovery allowlists immutable during this isolated run.
            prefixes = {'sim-r1': 'web', 'sim-r2': 'r2-web'}
            session_names = {'sim-r1': 'demo-session', 'sim-r2': 'demo-session-r2'}
            sessions = {}
            for robot_id in starts:
                prefix = prefixes[robot_id]
                task_ids = ['{}-pickup'.format(prefix), '{}-delivery'.format(prefix)]
                for index in range(2, 17):
                    task_ids.extend(('{}-{}-pickup'.format(prefix, index), '{}-{}-delivery'.format(prefix, index)))
                sessions[robot_id] = {task: session_names[robot_id] for task in task_ids}
            runtime = SimRuntime(db_path, sim_map, sessions, port=port,
                                 client_id='web-order-workflow', simulation_store=store)
            runtime.start()
            cleanup.callback(runtime.stop)
            map_id = '{}@v{}'.format(sim_map['map_id'], sim_map['version'])
            product_lookup = {item['product_type']: dict(item) for item in PRODUCTS}
            engines = {}
            for robot_id, start in starts.items():
                filename = '{}.sqlite3'.format(robot_id) if fleet_enabled else 'robot.sqlite3'
                inbox = SimOrderInbox(folder / filename, robot_id, map_id)
                inbox.initialize()
                authorizer = SimOrderAuthorizer(runtime, robot_id=robot_id)
                robot = SimMqttOrderRobot(SimOrderExecutor(inbox, sim_map, start),
                                          port, authorization_guard=authorizer.guard)
                robot.start()
                cleanup.callback(robot.stop)
                engines[robot_id] = ProductControlEngine(
                    runtime, robot, sim_map, product_lookup, robot_id=robot_id,
                    twin_id={'sim-r1': 'AGV_01', 'sim-r2': 'AGV_02'}[robot_id],
                    session=session_names[robot_id], task_prefix=prefixes[robot_id],
                    wait_for_reservation=fleet_enabled)
            engine = FleetProductControlEngine(engines, sim_map) if fleet_enabled else engines['sim-r1']
        else:
            from server.order_demo_engine import OrderDemoEngine
            engine = OrderDemoEngine(map_data)
        service = start_mqtt()
        cleanup.callback(stop_mqtt, service)
        demo = DemoRuntime(engine, port, nodes, service,
                           auto_recovery=(os.environ.get('AGV_ORDER_COORDINATED') == '1'
                                          and os.environ.get('AGV_ORDER_FLEET') == '1'))
        app.state.mqtt_service = service
        try:
            demo.start()
            cleanup.callback(demo.stop)
            app.state.demo = demo
            app.state.demo_actions = demo.actions
            yield
        finally:
            app.state.demo = None
            app.state.demo_actions = None


app = FastAPI(title='격리 가상 주문 시연', lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)
app.include_router(twin_router)


def _demo(request, *, mutate=False):
    # A fixed host and origin prevent another website from submitting local jobs.
    if (request.client is None or request.client.host not in ('127.0.0.1', '::1')
            or request.headers.getlist('host') != ['127.0.0.1:8000']):
        raise HTTPException(403, '로컬 시연에서만 사용할 수 있습니다')
    if mutate and request.headers.getlist('origin') != ['http://127.0.0.1:8000']:
        raise HTTPException(403, '같은 화면에서 주문해 주세요')
    runtime = getattr(request.app.state, 'demo', None)
    if runtime is None:
        raise HTTPException(503, '시연 서버가 준비되지 않았습니다')
    return runtime


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    pickup_node: StrictStr
    destination_node: StrictStr


class ProductOrderRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    product_id: StrictStr


@app.get('/order-demo/', response_class=HTMLResponse)
def page(request: Request):
    _demo(request)
    from server.order_demo_ui import HTML_PAGE
    return HTMLResponse(HTML_PAGE, headers={'Cache-Control': 'no-store'})


@app.get('/order-demo/api/nodes')
def nodes(request: Request):
    return {'nodes': _demo(request).nodes()}


@app.get('/order-demo/api/status')
def status(request: Request):
    return _demo(request).snapshot()


@app.get('/order-demo/api/products')
def products(request: Request):
    _demo(request)
    return {'products': [dict(product) for product in PRODUCTS]}


@app.get('/order-demo/api/submissions/{request_id}')
def submission(request_id: str, request: Request):
    demo = _demo(request)
    try:
        return demo.admission.get(request_id)
    except (KeyError, ValueError):
        raise HTTPException(404, '요청을 찾을 수 없습니다') from None


def _request_key(request):
    values = request.headers.getlist('idempotency-key')
    if len(values) > 1:
        raise HTTPException(422, '요청 키가 올바르지 않습니다')
    return values[0] if values else None


def _submission_response(result):
    if isinstance(result, dict) and result.get('status') in ('pending', 'unknown') and result.get('request_id'):
        return JSONResponse(result, status_code=202)
    return result


@app.post('/order-demo/api/product-orders')
def product_order(body: ProductOrderRequest, request: Request):
    demo = _demo(request, mutate=True)
    try:
        submit = getattr(demo, 'submit_product_order', None)
        result = (submit(body.product_id, _request_key(request)) if submit is not None
                  else demo.create_product_order(body.product_id))
        return _submission_response(result)
    except AdmissionConflict:
        raise HTTPException(409, '요청 키가 다른 주문에 사용됐습니다') from None
    except (AdmissionBusy, AdmissionStopped):
        raise HTTPException(503, '가상 주문을 접수할 수 없습니다') from None
    except ValueError:
        raise HTTPException(422, '알 수 없는 상품입니다') from None
    except RuntimeError:
        raise HTTPException(409, '가상 작업을 계속할 수 없습니다') from None
    except ConnectionError:
        raise HTTPException(503, 'Unreal 연결이 준비되지 않았습니다') from None


@app.post('/order-demo/api/orders')
def order(body: OrderRequest, request: Request):
    demo = _demo(request, mutate=True)
    try:
        submit = getattr(demo, 'submit_order', None)
        result = (submit(body.pickup_node, body.destination_node, _request_key(request)) if submit is not None
                  else demo.create_order(body.pickup_node, body.destination_node))
        return _submission_response(result)
    except AdmissionConflict:
        raise HTTPException(409, '요청 키가 다른 주문에 사용됐습니다') from None
    except (AdmissionBusy, AdmissionStopped):
        raise HTTPException(503, '가상 주문을 접수할 수 없습니다') from None
    except ValueError:
        raise HTTPException(422, '선택한 지점으로 경로를 만들 수 없습니다') from None
    except RuntimeError:
        raise HTTPException(409, '현재 가상 주문을 처리 중입니다') from None
    except ConnectionError:
        raise HTTPException(503, '가상 브로커에 연결되지 않았습니다') from None
