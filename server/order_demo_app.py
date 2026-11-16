"""Separate, loopback-only web ordering demo for a synthetic AGV.

This process publishes simulated observations, never physical robot commands.
Launch it via the isolated demo launcher, not the production server.
"""
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, StrictStr
import paho.mqtt.client as mqtt

from server.mqtt_client import start_mqtt, stop_mqtt
from server.order_demo_actions import DemoActionSession
from server.twin_websocket import router as twin_router

PRODUCTS = (
    {'id': 'red', 'label': '빨간 상품', 'product_type': 'Red', 'pickup_node': 'D01',
     'destination_node': 'D04', 'shelf_id': 'SHELF_RED', 'slot_index': 0},
    {'id': 'blue', 'label': '파란 상품', 'product_type': 'Blue', 'pickup_node': 'D06',
     'destination_node': 'D05', 'shelf_id': 'SHELF_BLUE', 'slot_index': 0},
)


class DemoRuntime:
    def __init__(self, engine, port, nodes, observer=None):
        self.engine = engine
        self.port = port
        self._nodes = nodes
        self.observer = observer
        self.lock = threading.RLock()
        self.actions = DemoActionSession(engine, self.lock, can_dispatch=self._ready)
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
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.client.disconnect()
        self.client.loop_stop()

    def nodes(self):
        return [dict(node) for node in self._nodes]

    def _ready(self):
        if not self.connected.is_set():
            return False
        if self.observer is None:
            return True
        try:
            state = self.observer.snapshot()
            return state.get('connected') is True and state.get('ready') is True
        except Exception:
            return False

    def snapshot(self):
        with self.lock:
            result = self.engine.snapshot()
        result['connected'] = self._ready()
        result['unreal_connected'] = self.actions.available()
        return result

    def create_order(self, pickup, destination):
        with self.lock:
            if not self._ready():
                raise ConnectionError('isolated broker unavailable')
            return self.engine.create_order(pickup, destination)

    def create_product_order(self, product_id):
        product = next((item for item in PRODUCTS if item['id'] == product_id), None)
        if product is None:
            raise ValueError('unknown product')
        with self.lock:
            if not self._ready() or not self.actions.available():
                raise ConnectionError('authenticated Unreal connection unavailable')
            return self.engine.create_product_order(product['product_type'], product['pickup_node'],
                                                    product['destination_node'], product['shelf_id'],
                                                    product['slot_index'])

    def _run(self):
        try:
            previous = time.monotonic()
            while not self.stop_event.is_set():
                now = time.monotonic()
                if self._ready():
                    with self.lock:
                        state = self.engine.tick(min(now - previous, 0.5))
                    payload = {'map_id': 'twin-validation-v1',
                               'last_node': state['current_node'],
                               'status': 'MOVING' if state['status'] == 'moving' else 'IDLE',
                               'battery': state['battery'], 'pose': state['pose']}
                    result = self.client.publish('agv/sim-r1/state', json.dumps(payload),
                                                 qos=0, retain=False)
                    if result.rc != mqtt.MQTT_ERR_SUCCESS:
                        self.connected.clear()
                        with self.lock:
                            self.engine.action_unknown()
                else:
                    with self.lock:
                        self.engine.action_unknown()
                previous = now
                self.stop_event.wait(0.2)
        except Exception:
            logging.getLogger(__name__).error('isolated demo worker stopped')
            self.connected.clear()
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
    from server.order_demo_engine import OrderDemoEngine
    map_data = json.loads(raw.decode('utf-8'))
    engine = OrderDemoEngine(map_data)
    nodes = [{'id': node['id'], 'type': node.get('type', '')} for node in map_data['nodes']]
    service = start_mqtt()
    demo = DemoRuntime(engine, port, nodes, service)
    app.state.mqtt_service = service
    try:
        demo.start()
        app.state.demo = demo
        app.state.demo_actions = demo.actions
        yield
    finally:
        app.state.demo = None
        app.state.demo_actions = None
        try:
            demo.stop()
        finally:
            stop_mqtt(service)


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


@app.post('/order-demo/api/product-orders')
def product_order(body: ProductOrderRequest, request: Request):
    demo = _demo(request, mutate=True)
    try:
        return demo.create_product_order(body.product_id)
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
        return demo.create_order(body.pickup_node, body.destination_node)
    except ValueError:
        raise HTTPException(422, '선택한 지점으로 경로를 만들 수 없습니다') from None
    except RuntimeError:
        raise HTTPException(409, '현재 가상 주문을 처리 중입니다') from None
    except ConnectionError:
        raise HTTPException(503, '가상 브로커에 연결되지 않았습니다') from None
