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
                 tls_ca=None, keepalive=10, client_id=None, log=None):
        """log: 연결 소식을 바로 받을 함수(네트워크 스레드에서 불림, print 정도만). 로그 파일에는
        LinkController가 pop_notes()로 주행 루프에서 남긴다."""
        if mqtt is None:
            raise LinkUnavailable(u'paho-mqtt가 없어요. 젯슨에서 pip3 install --user "paho-mqtt<2"')
        self.robot_id = robot_id
        self.topic = lambda kind: "agv/%s/%s" % (robot_id, kind)
        self.host, self.port, self.keepalive = host, int(port), int(keepalive)
        self._log = log
        self._lock = threading.Lock()
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
        self.connected = True
        self.ever_connected = True
        self.disconnected_at = None
        client.publish(self.topic("conn"), "online", qos=1, retain=True)
        client.subscribe(self.topic("order"), qos=1)
        self._note("link_connected", host=self.host, port=self.port)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        self.disconnected_at = time.time()
        self._note("link_disconnected", rc=rc)

    def _on_message(self, client, userdata, msg):
        if msg.topic != self.topic("order"):
            return
        if msg.retain:
            # 주문은 retained로 오지 않는다(남아 있던 옛 주문). 실행하지 않는다
            self.dropped_retained += 1
            return
        with self._lock:
            if len(self._orders) < 64:
                self._orders.append(bytes(msg.payload))

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
        return out

    def publish_state(self, state):
        if self.connected:
            self.c.publish(self.topic("state"), json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                           qos=0, retain=True)

    def publish_event(self, event):
        """QoS 1. 연결이 끊긴 동안 낸 것도 paho가 큐에 두었다가 다시 붙으면 보낸다. 반환: 큐에 넣었는지."""
        info = self.c.publish(self.topic("event"), json.dumps(event, ensure_ascii=False, separators=(",", ":")),
                              qos=1, retain=False)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

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

    def publish_event(self, event):
        self.events.append(event)
        return True

    def start(self):
        return self

    def stop(self):
        pass
