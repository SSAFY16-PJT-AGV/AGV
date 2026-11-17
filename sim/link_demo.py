# -*- coding: utf-8 -*-
"""가상 매트의 로봇을 실제 MQTT 브로커에 붙여 서버 주문으로 움직인다(실물과 같은 runner·Navigator·agv/link 코드).

브로커(mosquitto 등)를 먼저 띄우고:

    # 로봇만: 서버(feature/server-communication)나 손으로 보낸 주문을 기다림
    python3 sim/link_demo.py --host 127.0.0.1 --port 1883

    # 작은 배차기까지: 경로를 구간으로 나눠 rear_clear를 받을 때마다 다음 해제를 보냄
    python3 sim/link_demo.py --dispatch P_orange,S1,M1,P_purple,S2,V_E15,M2,P_green \\
        --releases P_purple,V_E15,P_green --then P_green,M2,S2,P_purple,M1,S1,P_orange,D_P_orange

    # 주문 하나를 손으로(mosquitto_pub)
    mosquitto_pub -t agv/sim-r1/order -q 1 -m '{"order_id":"T1","seq":1,"map_id":"mat_ssafy_v2@v0",
        "nodes":["P_orange","S1","M3","P_red"],"released_until":"P_red","action_at_end":{"type":"none"}}'

토픽: agv/<id>/order(받음), agv/<id>/state(5Hz), agv/<id>/event(rear_clear), agv/<id>/conn(online/offline).
로봇 ID 기본값 sim-r1은 서버 브랜치의 트윈 매핑(AGV_TWIN_ROBOT_MAP {"sim-r1":"AGV_01"})과 맞춘 것이다.
--battery는 가상 배터리 값이다(서버 트윈 브리지가 battery 없는 상태를 버려서 시연용으로 넣음).
결과 폴더에 events.jsonl(주행 로그)과 link.jsonl(보낸 상태·이벤트, --dispatch면 보낸 주문)이 남는다.
"""
import argparse
import io
import json
import os
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sim", "mat"))

import run_sim  # noqa: E402
from agv.link import LinkController, MqttLink, settings_from  # noqa: E402
from agv.nav import NavMap, Navigator  # noqa: E402


class Dispatcher(object):
    """서버 대신 주문을 구간별로 보내는 작은 배차기(시험용). 예약·충돌 방지는 안 함."""

    def __init__(self, link_settings, robot_id, map_ref, jobs, log):
        import paho.mqtt.client as mqtt
        self.robot_id, self.map_ref, self.jobs, self.log = robot_id, map_ref, list(jobs), log
        if hasattr(mqtt, "CallbackAPIVersion"):
            self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="link-demo-dispatch")
        else:
            self.c = mqtt.Client(client_id="link-demo-dispatch")
        st = link_settings
        if st["username"]:
            self.c.username_pw_set(st["username"], st["password"])
        if st["tls_ca"]:
            self.c.tls_set(ca_certs=st["tls_ca"])
        self.c.on_connect = lambda c, u, f, rc: c.subscribe("agv/%s/event" % robot_id, qos=1)
        self.c.on_message = self._on_event
        self.c.connect(st["host"], st["port"], 10)
        self.c.loop_start()
        self.job = None
        self.k = 0
        self.n = 0
        self.done = threading.Event()

    def _send(self):
        oid, nodes, rel = self.job
        o = {"order_id": oid, "seq": self.k + 1, "map_id": self.map_ref, "nodes": nodes,
             "released_until": rel[self.k], "action_at_end": {"type": "none"}}
        self.c.publish("agv/%s/order" % self.robot_id, json.dumps(o), qos=1)
        self.log({"dir": "order", "msg": o})
        print(u"주문 %s#%d -> %s" % (oid, o["seq"], o["released_until"]))

    def next_job(self):
        if not self.jobs:
            self.done.set()
            return
        self.n += 1
        nodes, rel = self.jobs.pop(0)
        self.job, self.k = ("D%d" % self.n, nodes, rel), 0
        self._send()

    def _on_event(self, c, u, msg):
        ev = json.loads(msg.payload.decode("utf-8"))
        if ev.get("type") != "rear_clear" or self.job is None or ev["order_id"] != self.job[0] \
                or ev["seq"] != self.k + 1:
            return
        print(u"정지 %s#%d at %s, 놓아줄 자원 %d개" % (ev["order_id"], ev["seq"], ev["stopped_at"],
                                                 len(ev["cleared_resources"])))
        time.sleep(1.0)
        if self.k + 1 < len(self.job[2]):
            self.k += 1
            self._send()
        else:
            self.next_job()

    def stop(self):
        self.c.loop_stop()
        self.c.disconnect()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--robot-id", default="sim-r1")
    ap.add_argument("--map", default=os.path.join(ROOT, "maps", "mat_ssafy_v2.json"))
    ap.add_argument("--mat", default=os.path.join(ROOT, "sim", "mat", "mats", "ssafy_v2.json"))
    ap.add_argument("--start", default="P_orange", help="놓은 자리 바로 앞 노드(가상 매트 기본 출발점은 주황 앞)")
    ap.add_argument("--waiting", default="zones", choices=["zones", "all"],
                    help="rear_clear 자원 계산의 대기 노드: zones(맵 waiting 구역) | all(서버 시연 가정)")
    ap.add_argument("--battery", type=float, default=0.8, help="가상 배터리(0~1). 음수면 안 보냄")
    ap.add_argument("--dispatch", default=None, help="작은 배차기로 보낼 경로(쉼표)")
    ap.add_argument("--releases", default=None, help="--dispatch 경로의 해제 노드들(쉼표). 없으면 끝 노드 한 번")
    ap.add_argument("--then", default=None, help="첫 작업 뒤 이어서 보낼 경로(쉼표, 끝 노드까지 한 번에 해제)")
    ap.add_argument("--max-time", type=float, default=600.0)
    ap.add_argument("--fast", action="store_true", help="실시간보다 빠르게(기본은 실제 시간에 맞춤)")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "link_demo"))
    ap.add_argument("--seed", type=int, default=2)
    a = ap.parse_args(argv)

    st = settings_from({"host": a.host, "port": a.port})
    if a.host:
        st["host"] = a.host
    if a.port:
        st["port"] = a.port
    with io.open(a.map, encoding="utf-8") as f:
        raw = json.load(f)
    nmap = NavMap(raw)
    nav = Navigator(nmap, start=a.start, stop_offset_mm=80, hold_at_end=True)
    if not os.path.isdir(a.out):
        os.makedirs(a.out)
    lf = io.open(os.path.join(a.out, "link.jsonl"), "w", encoding="utf-8")
    lock = threading.Lock()

    def log_line(d):
        with lock:
            lf.write(json.dumps(dict(d, t=round(time.time(), 3)), ensure_ascii=False) + u"\n")
            lf.flush()

    link = MqttLink(a.robot_id, st["host"], st["port"], st["username"], st["password"], st["tls_ca"]).start()
    pub_state, pub_event = link.publish_state, link.publish_event
    link.publish_state = lambda s: (log_line({"dir": "state", "msg": s}), pub_state(s))
    link.publish_event = lambda e: (log_line({"dir": "event", "msg": e}), pub_event(e))[1]
    if not link.wait_connected(5.0):
        print(u"브로커 %s:%d에 못 붙었어요" % (st["host"], st["port"]))
        return 2
    notes = []
    ctl = LinkController(link, nav, raw, start_node=a.start, waiting=a.waiting,
                         battery=(lambda: a.battery) if a.battery >= 0 else None,
                         log=lambda m, **k: (notes.append(m), print(u"[ctl] %s %s" % (
                             m, json.dumps(k, ensure_ascii=False)[:200]))))
    print(u"로봇 %s, 맵 %s, 첫 노드 %s - 주문을 기다려요" % (a.robot_id, ctl.book.map_ref, a.start))

    disp = None
    if a.dispatch:
        nodes = [n.strip() for n in a.dispatch.split(",") if n.strip()]
        rel = [n.strip() for n in a.releases.split(",")] if a.releases else [nodes[-1]]
        jobs = [(nodes, rel)]
        if a.then:
            t = [n.strip() for n in a.then.split(",") if n.strip()]
            jobs.append((t, [t[-1]]))
        disp = Dispatcher(st, a.robot_id, ctl.book.map_ref, jobs, log_line)

    state = {"runner": None, "t0": time.time()}

    def on_step(runner, t):
        if state["runner"] is None:
            state["runner"] = runner
            ctl.attach(runner)
            if disp is not None:
                disp.next_job()
        if not a.fast:
            lag = state["t0"] + t - time.time()
            if lag > 0:
                time.sleep(lag)
        s = ctl.step(now=time.time())
        if disp is not None and disp.done.is_set() and s["status"] == "IDLE":
            runner.stop("dispatch_done")
    try:
        ev, gt = run_sim.run(a.mat, a.out, seed=a.seed, max_time_s=a.max_time, policy=nav, on_step=on_step)
        print(u"끝: %s" % gt["finish_reason"])
    except KeyboardInterrupt:
        pass
    finally:
        if state["runner"] is not None:
            ctl.step(now=time.time())
        if disp is not None:
            disp.stop()
        link.stop()
        lf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
