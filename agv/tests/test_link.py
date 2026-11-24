# -*- coding: utf-8 -*-
"""서버 연동(agv/link): 주문 검사, 자세 계산, rear_clear 자원, 통일 맵, 가상 매트에서 주문으로 움직이기.

브로커는 쓰지 않는다(FakeLink). 실제 MQTT 왕복은 sim/link_demo.py로 따로 본다.
서버 브랜치(feature/server-communication)를 받아 둔 폴더를 AGV_SERVER_REPO로 주면 rear_clear 자원과
통일 맵을 서버 코드(server/segments.py, server/routing.py)로도 확인한다.
"""
import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sim", "mat"))
sys.path.insert(0, os.path.join(ROOT, "mapping"))

from agv.link import FakeLink, LinkController, MapGeometry, OrderBook, OrderRejected, SegmentRules  # noqa: E402
from agv.nav import NavMap, Navigator  # noqa: E402
from unify_map import unify  # noqa: E402

try:
    import run_sim  # noqa: E402
    from mat import Mat  # noqa: E402
except ImportError:
    run_sim = None

SERVER = os.environ.get("AGV_SERVER_REPO")
if SERVER:
    sys.path.insert(0, SERVER)
try:
    if not SERVER:
        raise ImportError
    from server.routing import MapGraph  # noqa: E402
    from server.segments import plan_segment  # noqa: E402
except Exception:
    MapGraph = plan_segment = None

SIM_MAP = os.path.join(ROOT, "maps", "sim_ssafy_v2.json")
MAT_MAP = os.path.join(ROOT, "maps", "mat_ssafy_v2.json")
MAT_V2 = os.path.join(ROOT, "sim", "mat", "mats", "ssafy_v2.json")


def load(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def order(seq, rel, nodes, oid="T1", ref="m@v1", act="none"):
    return {"order_id": oid, "seq": seq, "map_id": ref, "nodes": nodes, "released_until": rel,
            "action_at_end": {"type": act}}


class TestOrderBook(unittest.TestCase):
    R = ["A", "B", "C", "D"]

    def test_new_extend_duplicate_finish(self):
        b = OrderBook("m@v1")
        d = b.check(order(1, "B", self.R))
        self.assertEqual(d.kind, "new")
        b.commit(d)
        self.assertEqual(b.check(json.dumps(order(1, "B", self.R)).encode()).kind, "duplicate")
        with self.assertRaises(OrderRejected):          # 같은 순번, 다른 내용
            b.check(order(1, "C", self.R))
        with self.assertRaises(OrderRejected):          # 순번 건너뜀
            b.check(order(3, "D", self.R))
        with self.assertRaises(OrderRejected):          # 해제를 줄이거나 그대로
            b.check(order(2, "B", self.R))
        with self.assertRaises(OrderRejected):          # 경로를 바꿈
            b.check(order(2, "D", ["A", "B", "X", "D"]))
        with self.assertRaises(OrderRejected):          # 끝나기 전 새 작업
            b.check(order(1, "B", ["D", "C", "B"], oid="T2"))
        d = b.check(order(2, "D", self.R))
        self.assertEqual(d.kind, "extend")
        b.commit(d)
        self.assertEqual((b.segment_start_i, b.release_i, b.at_final_release), (1, 3, True))
        b.mark_finished()
        d = b.check(order(1, "B", ["D", "C", "B"], oid="T2"))
        self.assertEqual(d.kind, "new")
        b.commit(d)
        with self.assertRaises(OrderRejected):          # 끝난 작업
            b.check(order(3, "D", self.R))

    def test_format(self):
        b = OrderBook("m@v1")
        bad = [
            dict(order(1, "B", self.R), extra=1),
            order(1, "B", self.R, ref="m@v2"),
            order(0, "B", self.R),
            order(1, "Z", self.R),
            order(1, "A", self.R),                      # 첫 노드를 해제(앞으로 안 감)
            dict(order(1, "B", self.R), action_at_end={}),
            order(2, "B", self.R),                      # 새 작업은 seq 1부터
            order(1, "B", ["A", "A", "B"]),
        ]
        for o in bad:
            with self.assertRaises(OrderRejected, msg=o):
                b.check(o)
        with self.assertRaises(OrderRejected):
            b.check(b"not json")
        # 같은 노드를 두 번 지나는 경로(유턴)는 받는다
        self.assertEqual(b.check(order(1, "A", ["A", "B", "A"])).kind, "new")


class TestGeometryAndSegments(unittest.TestCase):
    def test_along_and_reverse(self):
        g = MapGeometry({"nodes": [{"id": "A", "pos": [0, 0]}, {"id": "B", "pos": [100, 100]}],
                         "edges": [{"id": "E1", "from": "A", "to": "B", "shape": [[0, 0], [100, 0], [100, 100]]}]})
        x, y, th = g.along("E1", 0.25)
        self.assertEqual((round(x), round(y), round(th)), (50, 0, 0))
        x, y, th = g.along("E1", 0.75)
        self.assertEqual((round(x), round(y), round(th)), (100, 50, 90))
        x, y, th = g.along("E1~r", 0.25)
        self.assertEqual((round(x), round(y), round(th)), (100, 50, -90))
        # 예전 형식(m, points)
        g2 = MapGeometry({"nodes": [{"id": "A", "x": 0, "y": 0}, {"id": "B", "x": 0.2, "y": 0}],
                          "edges": [{"id": "E1", "from": "A", "to": "B", "points": [{"x": 0.1, "y": 0}]}]})
        self.assertAlmostEqual(g2.along("E1", 0.5)[0], 100.0)

    def test_cleared_resources(self):
        raw = unify(load(MAT_MAP))
        rules = SegmentRules(raw, "zones")
        path = ["D_P_orange", "P_orange", "S1", "M3", "P_red", "D_P_red"]
        # 대기 노드는 스퍼 끝뿐 -> 한 구간이 끝까지, 마지막이면 선 노드만 남김
        got = rules.cleared(path, 0, len(path) - 1)
        self.assertNotIn(["node", "D_P_red"], got)
        self.assertIn(["edge", '["M3","S1"]'], got)
        self.assertIn(["node", "D_P_orange"], got)
        # 모든 노드 대기(서버 시연 가정): 한 엣지씩, 다음 구간과 겹치는 노드는 남김
        r_all = SegmentRules(raw, "all")
        self.assertEqual(r_all.cleared(path, 1, 2), [["edge", '["P_orange","S1"]'], ["node", "P_orange"]])

    @unittest.skipIf(plan_segment is None, "AGV_SERVER_REPO 없음")
    def test_matches_server_rules(self):
        raw = unify(load(MAT_MAP))
        MapGraph(raw)
        g = MapGraph(raw)
        rules = SegmentRules(raw, "zones")
        ends = [n["id"] for n in raw["nodes"] if n["id"].startswith("D_")]
        for a in ends:
            for b in ends:
                if a == b:
                    continue
                path = list(g.shortest_path(a, b).nodes)
                seg = plan_segment(raw, path)
                cur = set(seg.resources)
                i = path.index(seg.released_until)
                nxt = plan_segment(raw, path[i:]) if i < len(path) - 1 else None
                keep = set(nxt.resources) if nxt else {("node", path[i])}
                want = [list(r) for r in sorted(cur - keep)]
                self.assertEqual(rules.cleared(path, 0, i), want, (a, b))


class TestUnifyMap(unittest.TestCase):
    def test_parallel_edges_get_via_nodes(self):
        raw = load(SIM_MAP)
        u = unify(raw)
        vias = sorted(n["id"] for n in u["nodes"] if n.get("kind") == "via")
        self.assertEqual(vias, ["V_E15", "V_E16"])
        pairs = [(e["from"], e["to"]) for e in u["edges"] if e.get("type") != "destination"]
        self.assertEqual(len(pairs), len(set(pairs)))
        nodes = dict((n["id"], n) for n in u["nodes"])
        self.assertEqual(nodes["M2"]["inputs"]["right"], "E15b")
        self.assertIn(["E16b", "E17"], nodes["P_yellow"]["through"])
        for e in u["edges"]:                      # 모양 양 끝 = 노드 pos(서버가 정확히 같기를 요구)
            self.assertEqual(e["shape"][0], nodes[e["from"]]["pos"])
            self.assertEqual(e["shape"][-1], nodes[e["to"]]["pos"])
        self.assertEqual(unify(u), u)             # 다시 돌려도 같음
        m = NavMap(u)
        self.assertEqual(m.kind("V_E15"), "via")
        self.assertEqual(m.route_edges(["S2", "V_E15", "M2"]), {"E15", "E15b"})

    def test_committed_mat_map_is_unified(self):
        u = load(MAT_MAP)
        self.assertEqual(unify(u), u)
        self.assertEqual(u["unit"], "mm")

    @unittest.skipIf(MapGraph is None, "AGV_SERVER_REPO 없음")
    def test_server_accepts_mat_map(self):
        g = MapGraph(load(MAT_MAP))
        self.assertIn("V_E15", g.shortest_path("P_purple", "P_green").nodes)


class TestNavigatorVia(unittest.TestCase):
    def test_release_at_via_node(self):
        from agv.tests.test_nav import UNIFIED, Feed
        nav = Navigator(NavMap(UNIFIED), start="P_orange")
        nav.set_route(["P_orange", "S1", "V_E3", "M1", "P_green"], release="S1")
        f = Feed(nav)
        f.patch("orange", 0)
        self.assertIsNone(nav.choose_split(380))            # 갈림길이 해제 노드: 앞에서 대기
        stop = nav.pop_stop()
        self.assertEqual((stop["node"], stop["hold"]), ("S1", True))
        nav.extend("V_E3")
        self.assertIsNone(nav.pop_stop())                    # 아직 via로 가는 엣지가 아님
        nav.resumed()
        self.assertEqual(nav.choose_split(400), "right")
        nav.on_split("right", 400)
        stop = nav.pop_stop()
        L = math.hypot(250, 120)
        self.assertEqual((stop["node"], stop["hold"]), ("V_E3", True))
        self.assertAlmostEqual(stop["pos"], 400 + L, places=3)
        nav.extend("P_green")
        nav.resumed()
        nav.on_merge(400 + L + 300)
        self.assertEqual(nav.where()["last_node"], "M1")
        stop = f.patch("green", 1400)
        self.assertEqual((stop["node"], stop["reason"]), ("P_green", "nav_arrived"))


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestSimServe(unittest.TestCase):
    """가상 매트에서 FakeLink로 주문을 넣어 움직인다(실물과 같은 runner·Navigator 코드)."""

    @classmethod
    def setUpClass(cls):
        cls.raw = load(MAT_MAP)        # 실물이 쓰는 맵(매트 도안 = 가상 매트 ssafy_v2)
        cls.ref = "%s@v%s" % (cls.raw["map_id"], cls.raw["version"])
        mat = Mat(MAT_V2)
        cls.centers = dict((p["color"], p["center"]) for p in mat.d["patches"])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, script, seed=2, max_time=90, start="P_orange"):
        nav = Navigator(NavMap(self.raw), start=start, stop_offset_mm=80, hold_at_end=True)
        link = FakeLink("sim")
        notes = []
        ctl = LinkController(link, nav, self.raw, start_node=start, log=lambda m, **k: notes.append((m, k)))
        st = {"runner": None}

        def on_step(runner, t):
            if st["runner"] is None:
                st["runner"] = runner
                ctl.attach(runner)
            s = ctl.step(now=t)
            script(link, ctl, runner, t, s)
        ev, gt = run_sim.run(MAT_V2, self.tmp, seed=seed, max_time_s=max_time, policy=nav, on_step=on_step)
        return link, ctl, notes, gt

    def _o(self, seq, rel, nodes, oid="T1"):
        return order(seq, rel, nodes, oid=oid, ref=self.ref)

    def test_segments_through_via_then_new_order_into_spur(self):
        r1 = ["P_orange", "S1", "M1", "P_purple", "S2", "V_E15", "M2", "P_green"]
        # 서버 최단 경로(server.routing): 초록에서 유턴해 본선을 거꾸로, 주황 스퍼 끝에 주차
        r2 = ["P_green", "M2", "S2", "P_purple", "M1", "S1", "P_orange", "D_P_orange"]
        plan = {0: self._o(1, "P_purple", r1), 1: self._o(2, "V_E15", r1), 2: self._o(3, "P_green", r1),
                3: self._o(1, "D_P_orange", r2, oid="T2")}
        sent = set()
        rec = {"poses": []}

        def script(link, ctl, runner, t, s):
            n = len(link.events)
            if n in plan and n not in sent and (n == 0 or s["status"] in ("WAITING", "IDLE")):
                if n == 3:      # 틀린 주문 몇 개를 먼저: 다른 맵, 다른 시작 노드, 같은 것 두 번
                    link.send(dict(plan[3], map_id="other@v9"))
                    link.send(self._o(1, "P_red", ["P_orange", "S1", "M3", "P_red"], oid="T9"))
                    link.send(plan[2])
                sent.add(n)
                link.send(plan[n])
            if s["status"] == "WAITING" and s["pose"] is not None:
                rec["poses"].append((s["last_node"], s["pose"]))
            if n == 4 and s["status"] == "IDLE":
                rec["final_state"] = s
                runner.stop("test_done")
        link, ctl, notes, gt = self._run(script, max_time=120)
        self.assertEqual(gt["finish_reason"], "test_done", [n for n in notes if n[0] != "link_order"])
        self.assertEqual([(e["order_id"], e["seq"], e["stopped_at"]) for e in link.events],
                         [("T1", 1, "P_purple"), ("T1", 2, "V_E15"), ("T1", 3, "P_green"), ("T2", 1, "D_P_orange")])
        for e in link.events:
            self.assertEqual(set(e), {"type", "event_id", "robot_session", "order_id", "seq", "map_id",
                                      "stopped_at", "cleared_resources"})
        rej = [k["reason"] for m, k in notes if m == "link_order_rejected"]
        self.assertEqual(len(rej), 2, rej)
        self.assertTrue(any("map mismatch" in r for r in rej))
        self.assertTrue(any("order starts at P_orange" in r for r in rej))
        self.assertIn("link_order_duplicate", [m for m, k in notes])
        fs = rec["final_state"]
        self.assertEqual((fs["last_node"], fs["order_id"], fs["pose"]["src"]), ("D_P_orange", "T2", "node"))
        self.assertIsNone(fs["rejected"])
        # 상태 메시지: 움직이는 동안 자세가 있고, 상태 값은 정한 넷 중 하나
        moving = [s for s in link.states if s["status"] == "MOVING"]
        self.assertGreater(len(moving), 20)
        self.assertGreater(sum(1 for s in moving if s["pose"] is not None) / float(len(moving)), 0.9)
        self.assertTrue(set(s["status"] for s in link.states) <= {"IDLE", "MOVING", "WAITING", "ERROR"})

    def test_state_follows_the_robot_out_of_a_spur(self):
        """스퍼에 주차한 뒤 새 작업: 상태의 노드 순서가 실제 주행과 같아야 한다(트윈 표시).
        예전에는 후진 중 last가 부모 패치·next가 스퍼 끝으로 거꾸로 나오고, 본선을 달리는 동안
        이전 작업의 정지 순번 때문에 스퍼 끝에 서 있는 것처럼 보고했다."""
        r1 = ["P_orange", "S1", "M3", "P_red", "D_P_red"]
        r2 = ["D_P_red", "P_red", "M3", "S1", "P_orange", "D_P_orange"]
        plan = {0: self._o(1, "D_P_red", r1, oid="A"), 1: self._o(1, "D_P_orange", r2, oid="B")}
        sent = set()

        def script(link, ctl, runner, t, s):
            n = len(link.events)
            if n in plan and n not in sent and s["status"] in ("IDLE", "WAITING"):
                sent.add(n)
                link.send(plan[n])
            if n == 2 and s["status"] == "IDLE":
                runner.stop("test_done")
        link, ctl, notes, gt = self._run(script, max_time=90)
        self.assertEqual(gt["finish_reason"], "test_done")
        seq = []
        for st in link.states:
            if st["order_id"] == "B" and st["status"] == "MOVING" and (not seq or seq[-1] != st["last_node"]):
                seq.append(st["last_node"])
        self.assertEqual(seq[0], "D_P_red", seq)
        self.assertEqual(seq[-1], "D_P_orange", seq)
        visited = [n for n in seq if n in r2]
        self.assertEqual(visited, sorted(visited, key=r2.index), seq)   # 경로 순서를 거슬러 가지 않음
        self.assertIn("S1", seq)
        backing = [st for st in link.states if st["order_id"] == "B" and st["last_node"] == "D_P_red"
                   and st["status"] == "MOVING"]
        self.assertTrue(backing and all(st["next_node"] == "P_red" for st in backing))

    def test_link_loss_stops_robot(self):
        route = ["P_orange", "S1", "M1", "P_purple", "S2", "M2", "P_green"]
        rec = {}

        def script(link, ctl, runner, t, s):
            if t < 0.06:
                link.send(self._o(1, "P_green", route))
            if 3.0 <= t < 3.06:
                link.connected, link.disconnected_at = False, t
            if "held" not in rec and runner.state == "hold" and runner.hold_reason == "link_lost":
                rec["held"], rec["odo"] = t, runner.odo
            if t > 6.0 and "restored" not in rec:
                link.connected, link.disconnected_at = True, None
                rec["restored"] = t
            if t > 8.0:
                rec["state"] = s
                rec["odo_end"] = runner.odo
                runner.stop("test_done")
        link, ctl, notes, gt = self._run(script, max_time=20)
        self.assertLess(rec["held"] - 3.0, 1.3)                 # link_loss_s(1초) 뒤 바로 섬
        self.assertAlmostEqual(rec["odo_end"], rec["odo"], delta=1.0)   # 다시 붙어도 그대로 서 있음
        self.assertEqual((rec["state"]["status"], rec["state"]["error"]), ("ERROR", "link_lost"))


if __name__ == "__main__":
    unittest.main()
