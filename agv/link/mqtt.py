# -*- coding: utf-8 -*-
"""로봇 쪽 MQTT 연결(설계 문서 3장 토픽).

    구독  agv/{id}/order   QoS 1  서버 주문
    발행  agv/{id}/state   QoS 0, retained  상태(5Hz 기본)
          agv/{id}/event   QoS 1  이벤트(지금은 rear_clear만 - 서버가 다른 종류를 받으면 오류로 봄)
          agv/{id}/conn    QoS 1, retained  online / offline(LWT: 끊기면 브로커가 offline을 대신 발행)

paho-mqtt 1.x(젯슨 Python 3.6)와 2.x를 다 쓸 수 있다. 젯슨 설치: pip3 install --user "paho-mqtt<2"
접속 설정은 서버와 같은 이름의 환경변수(MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD,
MQTT_TLS_CA)나 config link 항목으로 준다. 받은 주문은 큐에 넣기만 하고, 주행 루프가 poll()로 꺼내
처리한다(네트워크 스레드에서 주행 상태를 건드리지 않음).
"""
import json
import os
import threading
import time
import uuid

try:
    import paho.mqtt.client as mqtt
except ImportError:      # 설치 안 됨: MqttLink를 만들 때 안내
    mqtt = None


class LinkUnavailable(RuntimeError):
    pass


def settings_from(cfg=None, env=None):
    """config(link 항목)와 환경변수에서 접속 설정. 환경변수가 우선."""
    cfg = dict(cfg or {})
    env = os.environ if env is None else env
    out = {
        "host": env.get("MQTT_HOST") or cfg.get("host") or "127.0.0.1",
        "port": int(env.get("MQTT_PORT") or cfg.get("port") or 1883),
        "username": env.get("MQTT_USERNAME") or cfg.get("username"),
        "password": env.get("MQTT_PASSWORD") or cfg.get("password"),
        "tls_ca": env.get("MQTT_TLS_CA") or cfg.get("tls_ca"),
        "keepalive": int(cfg.get("keepalive") or 10),
    }
    return out


class MqttLink(object):
    def __init__(self, robot_id, host="127.0.0.1", port=1883, username=None, password=None,
                 tls_ca=None, keepalive=10, client_id=None, log=None,
                 reconcile_isolated=False, reconcile_key=None):
        """log: 연결 소식을 바로 받을 함수(네트워크 스레드에서 불림, print 정도만). 로그 파일에는
        LinkController가 pop_notes()로 주행 루프에서 남긴다."""
        if mqtt is None:
            raise LinkUnavailable(u'paho-mqtt가 없어요. 젯슨에서 pip3 install --user "paho-mqtt<2"')
        self.robot_id = robot_id
        self.topic = lambda kind: "agv/%s/%s" % (robot_id, kind)
        self.host, self.port, self.keepalive = host, int(port), int(keepalive)
        if not reconcile_isolated and os.environ.get('AGV_RECONCILE_ISOLATED') == '1':
            reconcile_isolated = True
            try:
                reconcile_key = bytes.fromhex(os.environ.get('AGV_RECONCILE_KEY_HEX', ''))
            except ValueError:
                raise ValueError('invalid isolated reconciliation key')
        if type(reconcile_isolated) is not bool:
            raise ValueError('invalid reconciliation mode')
        if reconcile_isolated:
            if host != '127.0.0.1' or not isinstance(reconcile_key, bytes) or len(reconcile_key) < 32:
                raise ValueError('reconciliation requires loopback and an isolated 32-byte key')
        elif reconcile_key is not None:
            raise ValueError('reconciliation requires explicit isolated mode')
        self.reconcile_key = reconcile_key if reconcile_isolated else None
        self._reconcile_queue = []
        self._reconcile_ready = False
        self._reconcile_mid = None
        self._signal_lock = threading.Lock()
        self._disconnect_signal = 0
        self._event_credentials = (username, password, tls_ca)
        self._log = log
        self._lock = threading.RLock()
        self._connection_generation = 0
        self._disconnect_generation = 0
        self._disconnect_observed = threading.Event()
        self._orders = []
        self._notes = []
        self.connected = False
        self.ever_connected = False
        self.disconnected_at = None
        self.dropped_retained = 0
        cid = client_id or "agv-%s" % robot_id
        if hasattr(mqtt, "CallbackAPIVersion"):     # paho 2.x
            self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid, clean_session=True)
        else:
            self.c = mqtt.Client(client_id=cid, clean_session=True)
        if username:
            self.c.username_pw_set(username, password)
        if tls_ca:
            self.c.tls_set(ca_certs=tls_ca)
        elif password:
            raise ValueError(u"비밀번호를 쓰려면 MQTT_TLS_CA(TLS)가 필요해요")
        self.c.will_set(self.topic("conn"), "offline", qos=1, retain=True)
        self.c.reconnect_delay_set(1, 5)
        self.c.on_connect = self._on_connect
        self.c.on_disconnect = self._on_disconnect
        self.c.on_message = self._on_message
        self.c.on_subscribe = self._on_subscribe

    def _note(self, msg, **kw):
        # 네트워크 스레드에서 부름: 로그 파일은 주행 루프가 쓰도록 모아 둔다(pop_notes)
        with self._lock:
            self._notes.append((msg, kw))
        if self._log is not None:
            self._log(msg, **kw)

    def pop_notes(self):
        with self._lock:
            out, self._notes = self._notes, []
        return out

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self._note("link_connect_failed", rc=rc)
            return
        with self._lock:
            self._connection_generation += 1
            self._reconcile_ready = False
            self._reconcile_mid = None
            self._reconcile_queue = []
            self.connected = True
            self.ever_connected = True
            self.disconnected_at = None
        client.publish(self.topic("conn"), "online", qos=1, retain=True)
        client.subscribe(self.topic("order"), qos=1)
        if self.reconcile_key is not None:
            rc, mid = client.subscribe([(self.topic('reconcile/request'), 1),
                                        (self.topic('reconcile/commit'), 1)])
            with self._lock:
                if rc == 0 and type(mid) is int and mid > 0:
                    self._reconcile_mid = (self._connection_generation, mid)
        self._note("link_connected", host=self.host, port=self.port)

    def _on_subscribe(self, client, userdata, mid, granted_qos):
        with self._lock:
            if self._reconcile_mid != (self._connection_generation, mid):
                return
            self._reconcile_mid = None
            self._reconcile_ready = list(granted_qos) == [1, 1]

    def _on_disconnect(self, client, userdata, rc):
        # This must happen before waiting for the control/MQTT lock, which may
        # be held while an event waits for its separate connection's PUBACK.
        with self._signal_lock:
            self._disconnect_signal += 1
            self._disconnect_observed.set()
        with self._lock:
            self.connected = False
            self.disconnected_at = time.time()
            self._disconnect_generation += 1
            self._connection_generation += 1
            self._orders = []  # never replay orders from a lost connection
            self._reconcile_ready = False
            self._reconcile_queue = []
        self._note("link_disconnected", rc=rc)

    def _on_message(self, client, userdata, msg):
        if msg.topic in (self.topic('reconcile/request'), self.topic('reconcile/commit')):
            from .reconcile import MAX_MESSAGE
            if (type(msg.retain) is not bool or msg.retain or type(msg.qos) is not int or msg.qos != 1
                    or not isinstance(msg.payload, bytes) or len(msg.payload) > MAX_MESSAGE):
                return
            with self._lock:
                if self.connected and self._reconcile_ready and len(self._reconcile_queue) < 16:
                    with self._signal_lock:
                        signal = self._disconnect_signal
                    self._reconcile_queue.append((self._connection_generation, signal,
                                                  msg.topic.rsplit('/', 1)[-1], bytes(msg.payload)))
            return
        if msg.topic != self.topic("order"):
            return
        if msg.retain:
            # 주문은 retained로 오지 않는다(남아 있던 옛 주문). 실행하지 않는다
            self.dropped_retained += 1
            return
        with self._lock:
            if self.connected and not self._disconnect_observed.is_set() and len(self._orders) < 64:
                self._orders.append((self._connection_generation, bytes(msg.payload)))

    def start(self):
        self.c.connect_async(self.host, self.port, keepalive=self.keepalive)
        self.c.loop_start()
        return self

    def wait_connected(self, timeout=5.0):
        t0 = time.time()
        while not self.connected and time.time() - t0 < timeout:
            time.sleep(0.05)
        return self.connected

    def poll(self):
        """받은 주문 payload(bytes) 목록을 꺼낸다."""
        with self._lock:
            out, self._orders = self._orders, []
        return [payload for _, payload in out]

    def disconnect_pending(self):
        """Sticky callback-start signal; only a fresh link clears it."""
        return self._disconnect_observed.is_set()

    def disconnect_generation(self):
        """Count observed disconnects even when a reconnect clears the timestamp."""
        with self._lock:
            return self._disconnect_generation

    def run_with_disconnect_fence(self, handler):
        """Serialize a short control operation with connection callbacks."""
        with self._lock:
            return handler()

    def poll_with_generation(self):
        """Atomically return orders tagged with the connection they arrived on."""
        with self._lock:
            out, self._orders = self._orders, []
            return out

    def poll_reconcile(self):
        with self._lock:
            out, self._reconcile_queue = self._reconcile_queue, []
            return out

    def process_reconcile(self, generation, signal, kind, payload, handler):
        with self._lock:
            with self._signal_lock:
                valid = (self.connected and self._reconcile_ready and generation == self._connection_generation
                         and signal == self._disconnect_signal)
            if not valid:
                return False
            response = handler(kind, payload, generation)
            if response is None:
                return False
        # Never queue a QoS 1 response on the auto-reconnecting main client.
        return self.publish_reconcile_once(response, generation, signal)

    def _reconcile_current(self, generation, signal):
        with self._lock:
            with self._signal_lock:
                return (self.connected and self._reconcile_ready and generation == self._connection_generation
                        and signal == self._disconnect_signal)

    def publish_reconcile_once(self, response, generation, signal, timeout=3.0):
        return self._publish_once('reconcile/response', response,
                                  lambda: self._reconcile_current(generation, signal),
                                  'agv-reconcile-', timeout)

    def process_if_current(self, generation, payload, handler):
        """Serialize processing with disconnect and reject stale queued input.

        This fences callbacks already observed here; it cannot detect a
        physical network loss before the MQTT callback is delivered.
        """
        with self._lock:
            if (not self.connected or self._disconnect_observed.is_set()
                    or generation != self._connection_generation):
                return False
            handler(payload)
            return not self._disconnect_observed.is_set()

    def publish_state(self, state):
        if self.connected:
            self.c.publish(self.topic("state"), json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                           qos=0, retain=True)

    def publish_event(self, event):
        """QoS 1. 연결이 끊긴 동안 낸 것도 paho가 큐에 두었다가 다시 붙으면 보낸다. 반환: 큐에 넣었는지."""
        info = self.c.publish(self.topic("event"), json.dumps(event, ensure_ascii=False, separators=(",", ":")),
                              qos=1, retain=False)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def publish_event_once(self, event, timeout=3.0):
        """Use one temporary connection, require PUBACK, and never reconnect.

        A lost acknowledgement is uncertain, not evidence of non-delivery.
        This prevents this process from automatically replaying that event;
        it does not promise exactly-once delivery or physical stop proof.
        """
        return self._publish_once('event', event,
                                  lambda: self.connected and not self.disconnect_pending(),
                                  'agv-event-', timeout)

    def _publish_once(self, kind, payload, valid, prefix, timeout):
        if not valid():
            return False
        username, password, tls_ca = self._event_credentials
        client_id = prefix + uuid.uuid4().hex[:16]
        if hasattr(mqtt, "CallbackAPIVersion"):
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id, clean_session=True,
                                 reconnect_on_failure=False)
        else:
            client = mqtt.Client(client_id=client_id, clean_session=True)
        connected, acked = [], set()
        def on_connect(_client, _userdata, _flags, code):
            connected.append(code == 0)
        def on_publish(_client, _userdata, mid):
            acked.add(mid)
        client.on_connect = on_connect
        client.on_publish = on_publish
        try:
            if username:
                client.username_pw_set(username, password)
            if tls_ca:
                client.tls_set(ca_certs=tls_ca)
            if hasattr(client, "connect_timeout"):
                client.connect_timeout = timeout
            deadline = time.monotonic() + timeout
            if client.connect(self.host, self.port, keepalive=self.keepalive) != mqtt.MQTT_ERR_SUCCESS:
                return False
            while not connected:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or client.loop(timeout=min(0.1, remaining)) != mqtt.MQTT_ERR_SUCCESS:
                    return False
            if not connected[0] or not valid():
                return False
            info = client.publish(self.topic(kind), json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                                  qos=1, retain=False)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                return False
            while info.mid not in acked:
                remaining = deadline - time.monotonic()
                if (not valid() or remaining <= 0
                        or client.loop(timeout=min(0.1, remaining)) != mqtt.MQTT_ERR_SUCCESS):
                    return False
            return True
        except (OSError, RuntimeError, ValueError):
            return False
        finally:
            try:
                client.disconnect()
            except (OSError, RuntimeError):
                pass

    def stop(self):
        try:
            if self.connected:
                # 정상 종료: LWT는 안 나가므로 직접 offline을 남긴다
                self.c.publish(self.topic("conn"), "offline", qos=1, retain=True).wait_for_publish(timeout=2)
            self.c.disconnect()
        finally:
            self.c.loop_stop()


class FakeLink(object):
    """시험용: 브로커 없이 주문을 넣고 발행한 것을 모은다(MqttLink와 같은 모양)."""

    def __init__(self, robot_id="sim"):
        self.robot_id = robot_id
        self.connected = True
        self.ever_connected = True
        self.disconnected_at = None
        self.inbox = []
        self.states = []
        self.events = []

    def send(self, order):
        self.inbox.append(json.dumps(order).encode("utf-8") if isinstance(order, dict) else order)

    def poll(self):
        out, self.inbox = self.inbox, []
        return out

    def pop_notes(self):
        return []

    def publish_state(self, state):
        if self.connected:
            self.states.append(state)

    def publish_event_once(self, event):
        return self.publish_event(event)

    def publish_event(self, event):
        self.events.append(event)
        return True

    def start(self):
        return self

    def stop(self):
        pass
