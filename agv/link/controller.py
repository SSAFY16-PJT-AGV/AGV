# -*- coding: utf-8 -*-
"""서버 주문으로 움직이기: MQTT 연결(MqttLink)과 주행(ExploreRunner + Navigator)을 잇는다.

주행 루프에서 프레임마다 step()을 부른다. 하는 일:
  1. 받은 주문 처리: 새 작업이면 Navigator.set_route(nodes, released_until) 후 재개, 같은 작업의 다음
     주문이면 Navigator.extend(released_until)(대기 중이었으면 재개). 안 되면 거절하고 상태 메시지의
     rejected에 이유를 싣는다(서버가 이벤트 토픽에 다른 종류를 받으면 오류로 봐서 이벤트로는 안 보냄).
  2. 해제 노드에 서면 rear_clear 이벤트를 한 번 보낸다(서버 브랜치 제안 형식). 마지막 노드면 작업 끝.
  3. 상태를 state_hz로(바뀌면 바로) 보낸다: 마지막 노드, 엣지, 진행률, 자세(mm, 도), 상태, 배터리.
  4. 브로커 연결이 link_loss_s 넘게 끊기면 그 자리에 선다(설계 7.3절 5번). 다시 붙어도 저절로 가지
     않는다(resume_after_reconnect를 켜면 남은 해제 구간을 이어 감) - 재동기화 규약이 정해지면 바꾼다.

처음에는 서 있다(state IDLE, last_node = 놓인 노드). 로봇은 시작 노드 조금 앞(라인 위)에 놓고,
첫 주문의 첫 노드는 그 시작 노드여야 한다. 그다음 새 작업은 이전 작업의 마지막 노드에서 시작해야 한다.

상태 값: IDLE(주문 없음·작업 끝) | MOVING | WAITING(해제 노드에서 다음 해제를 기다림) | ERROR(error에 이유).
표준 라이브러리만 쓴다. Python 3.6 호환.
"""
import copy
import json
import math
import time
import uuid

from .geometry import MapGeometry
from .orders import OrderBook, OrderRejected
from .segments import SegmentRules

NAV_ERRORS = ("nav_off_route", "nav_spur_missed", "nav_gave_up")


def map_ref_of(navmap):
    return "%s@v%s" % (navmap.map_id, navmap.version if navmap.version is not None else 0)


class LinkController(object):
    def __init__(self, link, nav, raw_map, start_node=None, waiting="zones", state_hz=5.0,
                 link_loss_s=1.0, resume_after_reconnect=False, battery=None, log=None, clock=time.time):
        self.link = link
        self.nav = nav
        self.book = OrderBook(map_ref_of(nav.m))
        self.geom = MapGeometry(raw_map)
        self.rules = SegmentRules(raw_map, waiting)
        self.start_node = start_node
        self.state_period = 1.0 / float(state_hz) if state_hz else 0.2
        self.link_loss_s = float(link_loss_s)
        self.resume_after_reconnect = resume_after_reconnect
        self.battery = battery
        self._log = log
        self.clock = clock
        self.session = uuid.uuid4().hex[:16]
        self.runner = None
        self.error = None
        self.rejected = None
        self._event_n = 0
        self._stopped = set()           # rear_clear를 보낸 (order_id, seq)
        self._last_pub = None
        self._last_status = None
        self.n_orders = 0
        self._anchor = None             # (노드, 경로 순번, odo): 마지막으로 선 해제 노드
        get_generation = getattr(link, "disconnect_generation", None)
        self._seen_disconnect_generation = get_generation() if get_generation is not None else None
        self._strict_disconnect_latched = False

    # ---------- 연결 ----------
    def attach(self, runner):
        """runner를 만든 직후 부른다. 첫 주문까지 서 있게 한다."""
        self.runner = runner
        runner.hold("wait_order")
        self._note("link_wait_order", start=self.start_node, map=self.book.map_ref, session=self.session)

    def _note(self, msg, **kw):
        if self._log is not None:
            self._log(msg, **kw)

    def _consume_disconnect(self):
        get_generation = getattr(self.link, "disconnect_generation", None)
        if get_generation is None:
            return
        current = get_generation()
        previous = self._seen_disconnect_generation
        self._seen_disconnect_generation = current
        pending = getattr(self.link, "disconnect_pending", lambda: False)()
        if not pending and (previous is None or previous == current):
            return
        self._strict_disconnect_latched = True
        if self.error is None:
            self.error = "link_lost"
            self._note("link_lost", state=None if self.runner is None else self.runner.state)
        runner = self.runner
        if runner is not None and runner.state != "hold" and runner.hold("link_lost"):
            self._note("link_lost_hold")

    def _handle_fenced(self, payload):
        # Called while MqttLink holds its re-entrant connection lock.
        self._consume_disconnect()
        self._handle(payload)

    def _watch_stop_fenced(self):
        self._consume_disconnect()
        self._watch_stop()

    # ---------- 한 스텝 ----------
    def step(self, now=None):
        now = self.clock() if now is None else now
        for msg, kw in getattr(self.link, "pop_notes", lambda: [])():
            self._note(msg, **kw)
        self._consume_disconnect()
        self._watch_link(now)
        poll_tagged = getattr(self.link, "poll_with_generation", None)
        process = getattr(self.link, "process_if_current", None)
        if poll_tagged is not None and process is not None:
            for generation, payload in poll_tagged():
                if not process(generation, payload, self._handle_fenced):
                    self._note("link_stale_order_discarded")
        else:
            for payload in self.link.poll():
                self._handle_fenced(payload)
        self._consume_disconnect()
        self._watch_link(now)
        fence = getattr(self.link, "run_with_disconnect_fence", None)
        if fence is None:
            self._watch_stop_fenced()
        else:
            fence(self._watch_stop_fenced)
        st = self.state(now)
        if (self._last_pub is None or now - self._last_pub >= self.state_period - 1e-6
                or st["status"] != self._last_status):
            self.link.publish_state(st)
            self._last_pub = now
            self._last_status = st["status"]
        return st

    def here(self):
        """로봇이 서 있는 노드(주문의 첫 노드와 맞춰 봄). 모르면 None."""
        r = self.runner
        if r is None or r.state != "hold":
            return None
        if self.nav.at_node is not None:
            return self.nav.at_node
        if r.hold_reason == "wait_order" and self.book.current is None:
            return self.start_node
        return None

    def _reject(self, payload_order, reason):
        oid = seq = None
        if isinstance(payload_order, dict):
            oid, seq = payload_order.get("order_id"), payload_order.get("seq")
        self.rejected = {"order_id": oid, "seq": seq, "reason": reason}
        self._note("link_order_rejected", order_id=oid, seq=seq, reason=reason)

    def _handle(self, payload):
        r = self.runner
        try:
            d = self.book.check(payload)
        except OrderRejected as e:
            order = None
            try:
                order = json.loads(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
            except Exception:
                pass
            self._reject(order, e.args[0])
            return
        o = d.order
        if d.kind == "duplicate":
            self._note("link_order_duplicate", order_id=o["order_id"], seq=o["seq"])
            return
        if getattr(self.link, "disconnect_pending", lambda: False)():
            self._strict_disconnect_latched = True
            self.error = "link_lost"
            self._reject(o, "order requires explicit recovery after disconnect")
            return
        # A previously queued first order must not start after link loss.
        # Reconnect races are additionally fenced by MqttLink's queue lock.
        if not self.link.connected:
            self._reject(o, "order requires a connected link")
            return
        if r is None or r.finished:
            self._reject(o, "robot stopped (%s)" % (r.finish_reason if r is not None else "not ready"))
            return
        if self.error is not None:
            self._reject(o, "robot in error: %s" % self.error)
            return
        try:
            if d.kind == "new":
                here = self.here()
                if here is None or here != o["nodes"][0]:
                    raise OrderRejected("robot is at %s, order starts at %s" % (here, o["nodes"][0]))
                self.nav.hold_at_end = True       # 마지막 노드에 닿아도 끝내지 않고 다음 작업을 기다림
                self.nav.set_route(o["nodes"], o["released_until"])
            else:
                if getattr(self.nav, "_strict_repeat", False):
                    previous = self.book.current
                    key = (previous["order_id"], previous["seq"])
                    if (not self.link.connected or r.state != "hold" or r.hold_reason != "nav_hold"
                            or self.nav.at_node != previous["released_until"]
                            or key not in self._stopped):
                        raise OrderRejected("repeated route requires stop before extend")
                self.nav.extend(o["released_until"])
        except (OrderRejected, ValueError) as e:
            self._reject(o, e.args[0] if e.args else str(e))
            return
        strict = getattr(self.nav, "_strict_repeat", False)
        pending = getattr(self.link, "disconnect_pending", lambda: False)
        def cancel(reason):
            self.error = reason
            if reason == "link_lost":
                self._strict_disconnect_latched = True
            cancel_start = getattr(r, "cancel_unstarted_motion", None)
            if cancel_start is not None:
                cancel_start(reason)
            else:
                r.hold(reason)
            self._reject(o, "route start is uncertain")
        if not self.link.connected or pending():
            cancel("link_lost")
            return
        if strict:
            if r.state != "hold" or r.hold_reason not in ("wait_order", "nav_hold"):
                cancel("order_unconfirmed")
                return
            snapshot = copy.deepcopy(self.book.__dict__)
            try:
                arm = getattr(r, "arm_unstarted_disconnect_guard", None)
                if arm is not None:
                    arm(pending)
                if r.resume() is False or r.state == "hold":
                    raise RuntimeError("runner could not resume")
                if pending():
                    self.book.__dict__.clear(); self.book.__dict__.update(snapshot)
                    cancel("link_lost")
                    return
                self.book.commit(d)
                if (self.book.current != o or self.book.current_digest != d.digest
                        or self.book.seen.get((o["order_id"], o["seq"])) != d.digest):
                    raise RuntimeError("order commit did not match decision")
                if pending():
                    self.book.__dict__.clear(); self.book.__dict__.update(snapshot)
                    cancel("link_lost")
                    return
            except Exception:
                self.book.__dict__.clear(); self.book.__dict__.update(snapshot)
                cancel("link_lost" if pending() else "order_unconfirmed")
                return
        else:
            self.book.commit(d)
        if d.kind == "new":
            # 이전 작업에서 선 노드의 순번은 새 경로에서 의미가 없다. 새 경로의 첫 노드에 서 있다.
            self._anchor = (o["nodes"][0], 0, r.odo)
        self.n_orders += 1
        self.rejected = None
        self._note("link_order", kind=d.kind, order_id=o["order_id"], seq=o["seq"],
                   release=o["released_until"], nodes=o["nodes"])
        if not strict:
            if not self.link.connected or pending():
                cancel("link_lost")
                return
            if r.state == "hold" and r.hold_reason in ("wait_order", "nav_hold"):
                arm = getattr(r, "arm_unstarted_disconnect_guard", None)
                if arm is not None:
                    arm(pending)
                r.resume()
            else:
                r.policy_changed()

    # ---------- 정지·끊김 ----------
    def _watch_stop(self):
        r, b = self.runner, self.book
        if r is None or b.current is None:
            return
        if getattr(self.nav, "_strict_repeat", False) and self.error is not None:
            return
        o = b.current
        key = (o["order_id"], o["seq"])
        release = o["released_until"]
        if r.state == "hold" and r.hold_reason in NAV_ERRORS and self.error is None:
            self.error = r.hold_reason
            self._note("link_error", reason=self.error)
        if key in self._stopped or r.state != "hold" or r.hold_reason != "nav_hold" \
                or self.nav.at_node != release:
            return
        if not self.link.connected or getattr(self.link, "disconnect_pending", lambda: False)():
            self.error = "event_unconfirmed"
            self._note("link_event_unconfirmed", order_id=o["order_id"], seq=o["seq"])
            return
        if getattr(self.nav, "_strict_repeat", False) and not self.link.connected:
            self.error = "event_unconfirmed"
            self._note("link_event_unconfirmed", order_id=o["order_id"], seq=o["seq"])
            return
        if (getattr(self.nav, "_strict_repeat", False)
                and getattr(self.link, "disconnect_pending", lambda: False)()):
            self.error = "event_unconfirmed"
            self._note("link_event_unconfirmed", order_id=o["order_id"], seq=o["seq"])
            return
        previous_anchor = self._anchor
        self._stopped.add(key)
        self._anchor = (release, b.release_i, r.odo)
        self._event_n += 1
        ev = {"type": "rear_clear", "event_id": "%s-%d" % (self.session, self._event_n),
              "robot_session": self.session, "order_id": o["order_id"], "seq": o["seq"],
              "map_id": o["map_id"], "stopped_at": release,
              "cleared_resources": self.rules.cleared(o["nodes"], b.segment_start_i, b.release_i)}
        try:
            if getattr(self.nav, "_strict_repeat", False):
                one_shot = getattr(self.link, "publish_event_once", None)
                accepted = one_shot(ev) if one_shot is not None else False
            else:
                accepted = self.link.publish_event(ev)
        except Exception:
            if not getattr(self.nav, "_strict_repeat", False):
                raise
            self._stopped.discard(key)
            self._anchor = previous_anchor
            self.error = "event_unconfirmed"
            self._note("link_event_unconfirmed", order_id=o["order_id"], seq=o["seq"])
            return
        if (getattr(self.nav, "_strict_repeat", False)
                and (accepted is not True or getattr(self.link, "disconnect_pending", lambda: False)())):
            self._stopped.discard(key)
            self._anchor = previous_anchor
            self.error = "event_unconfirmed"
            self._note("link_event_unconfirmed", order_id=o["order_id"], seq=o["seq"])
            return
        final = b.at_final_release
        if final:
            b.mark_finished()
        self._note("link_stopped", order_id=o["order_id"], seq=o["seq"], node=release, final=final)

    def _watch_link(self, now):
        r, L = self.runner, self.link
        if r is None or r.finished:
            return
        if not L.connected:
            gone = L.disconnected_at
            if gone is None or now - gone < self.link_loss_s:
                return
            if self.error not in (None, "link_lost"):
                return
            if self.error is None:
                self._note("link_lost", state=r.state)
            self.error = "link_lost"
            # 라인 추종 중이면 바로, 회전·후진·주차 중이면 그 동작이 끝나는 대로 선다
            if r.state != "hold" and r.hold("link_lost"):
                self._note("link_lost_hold")
            return
        if self.error != "link_lost":
            return
        if self._strict_disconnect_latched or getattr(self.nav, "_strict_repeat", False):
            # Keep the disconnect latched even if the link returns during a
            # pivot/reverse/park action that cannot stop immediately. As soon
            # as the runner can hold, stop it and require manual recovery.
            if r.state != "hold" and r.hold("link_lost"):
                self._note("link_lost_hold")
            return
        if r.state == "hold" and r.hold_reason == "link_lost":
            # 구간 가운데에 섰음: 남은 구간을 이어 갈지는 재동기화 규약이 정할 일(기본: 그대로 섬)
            if self.resume_after_reconnect and not getattr(self.nav, "_strict_repeat", False):
                self.error = None
                self._note("link_restored", resumed=True)
                r.resume()
        else:
            self.error = None       # 노드에 서 있다가 끊겼다 붙음: 잃은 것 없음
            self._note("link_restored", resumed=False)

    # ---------- 상태 메시지 ----------
    def status(self):
        r = self.runner
        if r is None:
            return "IDLE"
        if r.finished or self.error is not None:
            return "ERROR"
        if r.state == "hold":
            if r.hold_reason == "wait_order":
                return "IDLE"
            if r.hold_reason == "nav_hold":
                return "IDLE" if self.book.finished else "WAITING"
            return "ERROR"
        return "MOVING"

    def _behind_anchor(self):
        """Navigator 위치가 마지막으로 선 해제 노드보다 뒤인가. runner는 교차 판정 동안(합류 뒤 30cm)
        이벤트를 보류해서, 그 사이 Navigator 위치가 한두 노드 늦다. 중간 노드(via)에서 다시 출발할
        때도 다음 노드를 볼 때까지 via 앞 엣지에 머문다. 그때는 선 노드에서 잰 이동거리로 대신한다."""
        a, o = self._anchor, self.book.current
        if a is None or o is None or self.nav.route is None:
            return False
        return getattr(self.nav, "_route_pos", a[1]) < a[1] and a[1] < len(o["nodes"]) - 1

    def _anchor_estimate(self, odo):
        node, i, odo0 = self._anchor
        nodes = self.book.current["nodes"]
        eid = self._route_edge(node, nodes[i + 1])
        if eid is None:
            return node, None, nodes[i + 1], None, self.geom.node_pose(node), "node"
        return self._walk(eid, i, (odo - odo0) if odo is not None else 0.0)

    def _route_edge(self, a, b):
        m = self.nav.m
        es = [e for e in m.out.get(a, []) if m.edges[e]["to"] == b]
        if self.nav.allowed:
            es = [e for e in es if e in self.nav.allowed] or es
        return es[0] if es else None

    def _walk(self, eid, from_i, along):
        """주행 엣지 eid(경로 순번 from_i의 노드에서 출발) 위 along mm 지점. 엣지 끝을 넘으면 경로를 따라
        다음 엣지로 이어 가되 해제 노드에서 멈춘다(runner가 합류 뒤 이벤트를 보류하는 동안 Navigator
        위치가 늦는 것을 메움). along이 음수면(노드 판정 지점이 차체보다 앞) 엣지 시작 방향 반대로 뺀다.
        반환: (last, [from, to], next, 진행률, (x, y, th), src)."""
        m, b = self.nav.m, self.book
        route = b.current["nodes"] if b.current is not None else None
        release_i = b.release_i if b.current is not None else None
        i = from_i
        while True:
            e = m.edges[eid]
            L = e["len"] or 1.0
            if along < 0:
                h = self.geom.heading_at_start(eid)
                x, y = self.geom.nodes.get(e["from"], (None, None))
                if h is None or x is None:
                    return e["from"], [e["from"], e["to"]], e["to"], 0.0, None, None
                t = math.radians(h)
                return (e["from"], [e["from"], e["to"]], e["to"], 0.0,
                        (x + along * math.cos(t), y + along * math.sin(t), h), "interp")
            nxt_eid = None
            if along > L and route is not None and i is not None and release_i is not None \
                    and i + 1 < release_i and route[i + 1] == e["to"]:
                nxt_eid = self._route_edge(route[i + 1], route[i + 2])
            if nxt_eid is None:
                frac = max(0.0, min(1.0, along / L))
                return e["from"], [e["from"], e["to"]], e["to"], frac, self.geom.along(eid, frac), "interp"
            along -= L
            eid, i = nxt_eid, i + 1

    def _route_i(self, node):
        """경로에서 Navigator가 지난 순번 근처의 node 순번."""
        b = self.book
        if b.current is None or self.nav.route is None:
            return None
        nodes = b.current["nodes"]
        p = getattr(self.nav, "_route_pos", 0)
        for k in range(max(0, p - 1), min(len(nodes), p + 3)):
            if nodes[k] == node:
                return k
        return None

    def state(self, now=None):
        now = self.clock() if now is None else now
        r, nav = self.runner, self.nav
        odo = r.odo if r is not None else None
        w = nav.where(odo) or {}
        last, edge, nxt, frac, pose = w.get("last_node"), None, w.get("next_node"), None, None
        eid = w.get("edge")
        src = None
        sp = nav.spur
        if sp is not None and sp.get("state") == "parked":
            last, nxt = sp["node"], None
            p = self.geom.node_pose(sp["node"])
            if p is not None:
                pose, src = p, "node"
        elif sp is not None and sp.get("state") == "exiting" and not sp.get("exit_hyps"):
            # 스퍼 안에서 후진 중: 스퍼 끝을 떠나 갈림 지점(부모 패치)으로 가는 중
            last, nxt = sp["node"], sp["parent"]
            p = self.geom.node_pose(sp["node"])
            if p is not None:
                pose, src = p, "spur"
        elif sp is not None and sp.get("state") == "exiting":
            # 후진을 마치고 갈림 지점에서 본선 쪽으로 도는 중: 다음은 고른 본선 엣지의 끝
            last, nxt = sp["parent"], nav.m.edges[sp["exit_hyps"][0].edge]["to"]
            p = self.geom.node_pose(sp["parent"])
            if p is not None:
                pose, src = p, "spur"
        elif sp is not None and (sp.get("state") == "entering" or (
                sp.get("state") == "armed" and (w.get("last_node") == sp["parent"] or self.here() == sp["parent"]))):
            # 스퍼로 들어가는 중(갈림 지점을 지나 돌기 전에 다음 본선 엣지로 넘어간 것처럼 보이는 동안 포함)
            last, nxt = sp["parent"], sp["node"]
            p = self.geom.node_pose(sp["parent"])
            if p is not None:
                pose, src = p, "spur"
        elif r is not None and r.state == "hold" and self.here() is not None:
            last = self.here()
            nxt = None
            h = self.geom.heading_at_end(eid) if (eid and nav.m.edges[eid]["to"] == last) else None
            if h is None and eid:
                h = self.geom.heading_at_start(eid)
            p = self.geom.node_pose(last, h)
            if p is not None:
                pose, src = p, ("start" if self.book.current is None else "node")
        elif self._behind_anchor():
            last, edge, nxt, frac, pose, src = self._anchor_estimate(odo)
        elif eid is not None:
            e = nav.m.edges[eid]
            edge = [e["from"], e["to"]]
            if w.get("along_mm") is not None and w.get("edge_mm"):
                last, edge, nxt, frac, pose, src = self._walk(eid, self._route_i(e["from"]), w["along_mm"])
            elif last is None and self.start_node is not None:
                last = self.start_node
        out = {
            "ts": round(now, 3), "map_id": self.book.map_ref, "robot_session": self.session,
            "last_node": last, "edge": edge, "next_node": nxt,
            "progress": None if frac is None else round(frac, 3),
            "dir": w.get("dir"),
            "pose": None if pose is None else {
                "x": round(pose[0], 1), "y": round(pose[1], 1),
                "th": None if pose[2] is None else round(pose[2], 1), "src": src},
            "status": self.status(),
            "carrying": None,
            "battery": self.battery() if self.battery is not None else None,
            "order_id": None, "seq": None, "released_until": None,
            "error": self.error or (r.finish_reason if r is not None and r.finished else None),
            "rejected": self.rejected,
        }
        if self.book.current is not None:
            o = self.book.current
            out.update({"order_id": o["order_id"], "seq": o["seq"], "released_until": o["released_until"]})
        return out
