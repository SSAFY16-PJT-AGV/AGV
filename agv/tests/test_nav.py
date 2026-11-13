# -*- coding: utf-8 -*-
"""완성된 맵 위 이동(agv/nav): 맵 읽기, 위치 추적, 갈림길 선택, 가상 매트에서 목표 패치 도착.

단위 테스트는 표준 라이브러리만 쓴다(Jetson에서도 돎). 시뮬레이션 테스트는 sim/mat/이 있어야 한다.
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

from agv.nav import NavMap, Navigator  # noqa: E402

try:
    import run_sim  # noqa: E402
    from mat import Mat  # noqa: E402
except ImportError:
    run_sim = None

SIM_MAP = os.path.join(ROOT, "maps", "sim_ssafy_v2.json")      # 도안 그대로(ssafy_v2)를 탐색해 만든 맵
MAT_V2 = os.path.join(ROOT, "sim", "mat", "mats", "ssafy_v2.json")

# docs/mapping/map-schema.md 5장 예시(통일 제안 형식: mm, pos, shape, bidirectional, 중간 노드)
UNIFIED = {
    "map_id": "mat_2x1", "version": 3, "schema": "agv-map-v1", "unit": "mm",
    "nodes": [
        {"id": "P_orange", "pos": [0, 0], "type": "access", "kind": "patch", "color": "orange",
         "spur": {"side": "left", "length_mm": 180, "node": "D_P_orange"}},
        {"id": "D_P_orange", "pos": [0, -180], "type": "destination", "kind": "spur_end"},
        {"id": "S1", "pos": [400, 0], "type": "junction", "kind": "split", "branches": {"left": "E2", "right": "E3"}},
        {"id": "V_E3", "pos": [650, 120], "type": "junction", "kind": "via"},
        {"id": "M1", "pos": [900, 0], "type": "junction", "kind": "merge"},
        {"id": "P_green", "pos": [1300, 0], "type": "access", "kind": "patch", "color": "green"},
    ],
    "edges": [
        {"id": "E1", "from": "P_orange", "to": "S1", "shape": [[0, 0], [400, 0]], "bidirectional": False,
         "type": "main", "length_mm": 402},
        {"id": "E2", "from": "S1", "to": "M1", "shape": [[400, 0], [900, 0]], "bidirectional": False,
         "type": "main", "branch": "left", "length_mm": 505},
        {"id": "E3", "from": "S1", "to": "V_E3", "shape": [[400, 0], [650, 120]], "bidirectional": False,
         "type": "main", "branch": "right"},
        {"id": "E3b", "from": "V_E3", "to": "M1", "shape": [[650, 120], [900, 0]], "bidirectional": False,
         "type": "main"},
        {"id": "E4", "from": "M1", "to": "P_green", "shape": [[900, 0], [1300, 0]], "bidirectional": False,
         "type": "main", "length_mm": 398},
        {"id": "E5", "from": "P_green", "to": "P_orange", "shape": [[1300, 0], [0, 0]], "bidirectional": False,
         "type": "main", "length_mm": 1500},
        {"id": "E_D_P_orange", "from": "P_orange", "to": "D_P_orange", "shape": [[0, 0], [0, -180]],
         "bidirectional": True, "type": "destination", "length_mm": 180},
    ],
}


class Feed(object):
    """runner 대신 이벤트를 넣어 준다(odo는 mm)."""

    def __init__(self, nav):
        self.nav = nav

    def patch(self, color, odo):
        self.nav.on_patch(color, odo)
        return self.nav.pop_stop()

    def split(self, odo, choice=None):
        side = self.nav.choose_split(odo)
        dead = self.nav.known_dead()
        if side is not None and not dead:
            self.nav.on_split(choice or side, odo + 100)
        elif dead:
            self.nav.on_tee(odo + 100)
        return side, dead


class TestNavMap(unittest.TestCase):
    def test_sim_map(self):
        m = NavMap.load(SIM_MAP)
        self.assertEqual(m.by_color["red"], "P_red")
        self.assertEqual(m.nodes["S1"]["branches"], {"left": "E2", "right": "E14"})
        self.assertEqual(m.spur_side_at("P_red"), "left")
        self.assertEqual(m.spur_side_at("T1"), "left")
        # 패치 위 X자 교차: 들어온 선 그대로만 지나감
        self.assertEqual(m.next_edges("E16"), ["E17"])
        self.assertEqual(m.next_edges("E8"), ["E9"])
        # 스퍼 엣지는 경로에서 뺌
        self.assertNotIn("E_D_P_red", m.out["P_red"])
        # 주황 뒤 첫 갈림길에서 빨강은 오른쪽(아래로)이 짧고, 보라는 왼쪽(위 선)
        self.assertEqual(m.shortest_path("E1", "P_red"), ["S1", "P_red"])
        self.assertEqual(m.shortest_path("E1", "P_purple"), ["S1", "M1", "P_purple"])
        # 초록에서 보라: 모서리 V자로 S자 선을 타고 노랑 교차를 지나 올라가는 길이 짧다
        self.assertEqual(m.shortest_path("E7", "P_purple"), ["S3", "P_yellow", "X2", "P_purple"])

    def test_unified_format(self):
        m = NavMap(UNIFIED)
        self.assertEqual(m.kind("V_E3"), "via")
        self.assertAlmostEqual(m.edges["E3"]["len"], math.hypot(250, 120), places=3)   # shape 길이
        self.assertEqual(m.edges["E1"]["len"], 402)                                    # length_mm 우선
        self.assertEqual(m.spur_side_at("P_orange"), "left")
        self.assertEqual(m.shortest_path("E5", "P_green"), ["P_orange", "S1", "M1", "P_green"])
        self.assertEqual(m.route_edges(["P_orange", "S1", "V_E3", "M1"]), {"E1", "E3", "E3b"})
        with self.assertRaises(ValueError):
            m.route_edges(["P_orange", "M1"])

    def test_route_through_via_node(self):
        """중간 노드(via)는 보이지 않으니 공짜로 건너뛴다: 오른쪽 가지 경로를 주면 오른쪽을 고른다."""
        nav = Navigator(NavMap(UNIFIED), start="P_orange")
        nav.set_route(["P_orange", "S1", "V_E3", "M1", "P_green"])
        f = Feed(nav)
        f.patch("orange", 0)
        side, dead = f.split(380)
        self.assertEqual((side, dead), ("right", None))
        nav.on_merge(900)
        self.assertEqual(nav.where()["last_node"], "M1")
        stop = f.patch("green", 1300)
        self.assertEqual((stop["node"], stop["hold"], stop["reason"]), ("P_green", False, "nav_arrived"))
        self.assertEqual(stop["pos"], 1300 + 80)


class TestNavigator(unittest.TestCase):
    def setUp(self):
        self.m = NavMap.load(SIM_MAP)

    def test_shortest_choice_and_patch_spur(self):
        nav = Navigator(self.m, goal="P_red", start="P_orange", stop_offset_mm=70)
        f = Feed(nav)
        self.assertIsNone(f.patch("orange", 125))
        nav.on_tee(170)                                    # 주황 패치 스퍼: 패치 속성이라 그냥 지나감
        self.assertEqual(f.split(370), ("right", None))   # S1 오른쪽 = 빨강으로 바로
        stop = f.patch("red", 1090)
        self.assertEqual((stop["node"], stop["pos"]), ("P_red", 1160))

    def test_known_spur_is_not_entered(self):
        """빨강 바로 뒤에서 갈라지는 모습이 보이면 빨강 스퍼: 묻지 않고 반대쪽으로 지나간다."""
        nav = Navigator(self.m, goal="P_blue", start="P_red")
        f = Feed(nav)
        f.patch("red", 0)
        self.assertEqual(f.split(80), ("right", "left"))
        self.assertEqual(f.patch("blue", 340)["node"], "P_blue")
        self.assertEqual(nav.relocalized, 0)

    def test_relocalize_from_unknown_start(self):
        nav = Navigator(self.m, goal="P_green")
        self.assertFalse(nav.where()["known"])
        f = Feed(nav)
        f.patch("blue", 5000)
        w = nav.where(5100)
        self.assertEqual((w["last_node"], w["next_node"], w["candidates"]), ("P_blue", "P_orange", 1))
        f.patch("orange", 6010)
        self.assertEqual(f.split(6380), ("left", None))   # 초록은 위 선

    def test_missed_merge_and_unexpected_patch(self):
        nav = Navigator(self.m, goal="P_green", start="P_orange")
        f = Feed(nav)
        f.patch("orange", 0)
        f.split(370)
        # M1 합류를 놓치고 바로 보라: 합류 건너뛰기(비용)로 설명
        f.patch("purple", 760)
        self.assertEqual(nav.where()["last_node"], "P_purple")
        self.assertEqual(nav.relocalized, 0)
        # 맵상 있을 수 없는 패치(보라 다음 빨강): 그 패치에서 다시 위치를 잡는다
        f.patch("red", 900)
        self.assertEqual((nav.where()["last_node"], nav.relocalized), ("P_red", 1))

    def test_route_hold_and_extend(self):
        nav = Navigator(self.m, start="P_orange")
        nav.set_route(["P_orange", "S1", "M1", "P_purple", "S2", "M2", "P_green"], release="P_purple")
        f = Feed(nav)
        f.patch("orange", 125)
        self.assertEqual(f.split(370)[0], "left")
        nav.on_merge(820)
        stop = f.patch("purple", 860)
        self.assertEqual((stop["node"], stop["hold"], stop["reason"]), ("P_purple", True, "nav_hold"))
        with self.assertRaises(ValueError):
            nav.extend("P_red")                     # 경로에 없는 노드
        nav.extend("P_green")
        nav.resumed()
        side, dead = f.split(950)                   # 렌즈(S2) 또는 보라 스퍼: 스퍼 반대쪽
        self.assertEqual(side, "right")
        nav.on_merge(1150)
        stop = f.patch("green", 1190)
        self.assertEqual((stop["node"], stop["hold"]), ("P_green", False))

    def test_route_forbids_other_branch_and_stops_off_route(self):
        nav = Navigator(self.m, start="P_orange")
        nav.set_route(["P_orange", "S1", "P_red"])
        f = Feed(nav)
        f.patch("orange", 0)
        self.assertEqual(f.split(370)[0], "right")
        # 선을 잘못 타서 위 선(왼쪽)으로 감: 예약 안 된 길이니 바로 대기
        nav2 = Navigator(self.m, start="P_orange")
        nav2.set_route(["P_orange", "S1", "P_red"])
        Feed(nav2).patch("orange", 0)
        nav2.choose_split(370)
        nav2.on_split("left", 470)
        stop = nav2.pop_stop()
        self.assertEqual((stop["reason"], stop["hold"], stop["pos"]), ("nav_off_route", True, None))

    def test_hold_before_split_target(self):
        nav = Navigator(self.m, start="P_orange")
        nav.set_route(["P_orange", "S1", "P_red"], release="S1")
        f = Feed(nav)
        f.patch("orange", 0)
        self.assertEqual(f.split(370), (None, None))
        stop = nav.pop_stop()
        self.assertEqual((stop["node"], stop["hold"], stop["pos"]), ("S1", True, None))
        nav.extend("P_red")
        nav.resumed()
        self.assertEqual(nav.choose_split(370), "right")

    def test_early_patch_stop(self):
        """runner가 교차 판정으로 패치 기록을 보류해도 목표 패치면 정지 위치를 먼저 받는다(한 번만)."""
        nav = Navigator(self.m, goal="P_purple", start="P_orange")
        f = Feed(nav)
        f.patch("orange", 0)
        f.split(370)
        nav.on_patch_early("purple", 760)
        self.assertEqual(nav.pop_stop()["pos"], 840)
        nav.on_merge(720)
        nav.on_patch("purple", 760)
        self.assertIsNone(nav.pop_stop())

    def test_spur_end_goal_rejected(self):
        with self.assertRaises(ValueError):
            Navigator(self.m, goal="D_P_red")

    def test_gives_up_after_laps(self):
        nav = Navigator(self.m, goal="P_yellow", start="P_orange")
        f = Feed(nav)
        stop = None
        loop = ["orange", "red", "blue"]               # 계속 아래 고리만 돎(노랑을 못 감)
        for i in range(30):
            nav.on_patch(loop[i % 3], 1000 * i)
            stop = nav.pop_stop() or stop
            if stop:
                break
        self.assertEqual(stop["reason"], "nav_gave_up")
        self.assertFalse(stop["hold"])

    def test_replays_exploration_events(self):
        """탐색 주행 로그(드라이브 모드, 갈림길 양쪽·스퍼·V자·교차 포함)를 넣으면 재위치 없이 끝까지 추적."""
        evs = [("patch", "orange", 125), ("tee", None, 168), ("split", "left", 467), ("merge", None, 817),
               ("patch", "purple", 855), ("tee", None, 927), ("split", "left", 1045), ("merge", None, 1145),
               ("patch", "green", 1183), ("tee", None, 1255), ("split", "left", 2028), ("patch", "yellow", 2267),
               ("cross", None, 2357), ("cross", None, 2614), ("merge", None, 2984), ("patch", "red", 3015),
               ("tee", None, 3087), ("patch", "blue", 3350), ("tee", None, 3423), ("patch", "orange", 4342),
               ("split", "right", 4710), ("patch", "red", 5322), ("split", "left", 5430),
               ("dead_end", "left", 5609), ("patch", "blue", 5699), ("tee", None, 5770),
               ("patch", "orange", 6693), ("split", "left", 7063), ("merge", None, 7407),
               ("patch", "purple", 7445), ("tee", None, 7518), ("split", "right", 7643), ("merge", None, 7734),
               ("patch", "green", 7776), ("split", "right", 8633), ("patch", "yellow", 8930),
               ("cross", None, 8968), ("cross", None, 9284), ("patch", "purple", 9958), ("split", "left", 10058),
               ("dead_end", "left", 10182), ("tee", None, 10208), ("merge", None, 10287),
               ("patch", "green", 10328)]
        nav = Navigator(self.m, start="P_orange")
        last = []
        for t, a, odo in evs:
            if t == "patch":
                nav.on_patch(a, odo)
                last.append(nav.where()["last_node"])
            elif t == "split":
                nav.on_split(a, odo)
            elif t == "dead_end":
                nav.on_dead_end(a, odo)
            else:
                getattr(nav, "on_" + t)(odo)
        self.assertEqual(nav.relocalized, 0)
        self.assertEqual(last, ["P_" + a for t, a, _ in evs if t == "patch"])
        # S자 선(V자 오른쪽)을 탄 뒤 노랑은 S자 선 쪽 교차(E17 -> X2)로 지나감
        self.assertEqual(nav.where()["last_node"], "P_green")


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestSimGoto(unittest.TestCase):
    """가상 매트(도안 그대로)에서 탐색으로 만든 맵(maps/sim_ssafy_v2.json)으로 목표 패치까지 간다."""

    @classmethod
    def setUpClass(cls):
        cls.m = NavMap.load(SIM_MAP)
        cls.centers = dict((p["color"], p["center"]) for p in Mat(MAT_V2).d["patches"])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _err(self, pose, color):
        c = self.centers[color]
        return math.hypot(pose[0] - c[0], pose[1] - c[1])

    def _events(self, ev):
        with io.open(ev, encoding="utf-8") as f:
            return [json.loads(l) for l in f]

    def test_goto_red_takes_short_branch(self):
        nav = Navigator(self.m, goal="P_red", start="P_orange")
        ev, gt = run_sim.run(MAT_V2, self.tmp, seed=1, max_time_s=60, policy=nav)
        self.assertEqual(gt["finish_reason"], "nav_arrived")
        self.assertLess(self._err(gt["final_pose"], "red"), 35.0)
        evs = self._events(ev)
        self.assertEqual([e["color"] for e in evs if e["type"] == "patch"], ["orange", "red"])
        self.assertEqual([e["choice"] for e in evs if e["type"] == "split"], ["right"])
        self.assertNotIn("dead_end", [e["type"] for e in evs])      # 빨강 스퍼에 안 들어감

    def test_goto_purple_through_vee_branch(self):
        """오른쪽 변에서 출발: 모서리의 급한 가지(V자)로 S자 선에 들어가 보라로 간다."""
        nav = Navigator(self.m, goal="P_purple", start="S3")
        ev, gt = run_sim.run(MAT_V2, self.tmp, seed=3, max_time_s=60, policy=nav,
                             start_pose=[1828, 700, -90])
        self.assertEqual(gt["finish_reason"], "nav_arrived")
        self.assertLess(self._err(gt["final_pose"], "purple"), 35.0)
        evs = self._events(ev)
        self.assertIn("vee_done", [e.get("msg") for e in evs])
        self.assertEqual([e["color"] for e in evs if e["type"] == "patch"], ["yellow", "purple"])

    def test_route_hold_resume(self):
        """서버 주문처럼: 경로 + 해제 노드(보라)에서 대기 -> 해제를 초록까지 늘리고 재개."""
        nav = Navigator(self.m, start="P_orange")
        nav.set_route(["P_orange", "S1", "M1", "P_purple", "S2", "M2", "P_green"], release="P_purple")
        st = {"t": None, "pose": None}

        def on_step(runner, t):
            if runner.state == "hold" and st["t"] is None:
                st["t"] = t
            if st["t"] is not None and runner.state == "hold" and t - st["t"] > 1.5:
                nav.extend("P_green")
                self.assertTrue(runner.resume())
        ev, gt = run_sim.run(MAT_V2, self.tmp, seed=2, max_time_s=60, policy=nav, on_step=on_step)
        self.assertIsNotNone(st["t"])
        self.assertEqual(gt["finish_reason"], "nav_arrived")
        self.assertLess(self._err(gt["final_pose"], "green"), 35.0)
        evs = self._events(ev)
        msgs = [e.get("msg") for e in evs]
        self.assertEqual((msgs.count("hold"), msgs.count("resume")), (1, 1))
        # 대기한 자리는 보라 패치 가운데 근처(궤적에서 대기 시각의 위치)
        k = int(st["t"] / 0.05 / 4)
        self.assertLess(self._err(gt["trajectory"][k], "purple"), 40.0)

    def test_unknown_start_with_speed_error(self):
        """시작 위치를 안 알려 주고, 실제 속도가 15% 느려도(패치로 속도 보정) 파랑에 선다."""
        nav = Navigator(self.m, goal="P_blue")
        ev, gt = run_sim.run(MAT_V2, self.tmp, seed=7, max_time_s=90, policy=nav, speed_scale=0.85,
                             cfg_override={"patch": {"length_mm": 159.3}})
        self.assertEqual(gt["finish_reason"], "nav_arrived")
        self.assertLess(self._err(gt["final_pose"], "blue"), 35.0)


if __name__ == "__main__":
    unittest.main()
