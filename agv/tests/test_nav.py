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
        self.assertEqual(m.shortest_path("E1", "P_red"), ["S1", "M3", "P_red"])
        self.assertEqual(m.shortest_path("E1", "P_purple"), ["S1", "M1", "P_purple"])
        # 초록에서 보라: 모서리 V자로 S자 선을 타고 노랑 교차를 지나 올라가는 길이 짧다
        self.assertEqual(m.shortest_path("E7", "P_purple"), ["S3", "P_yellow", "X2", "M1", "P_purple"])
        # 반대 방향: 합류가 갈림길로 보이고 가지는 inputs를 좌우 뒤집은 것, 스퍼는 오른쪽
        self.assertEqual(m.kind_seen("M3", "r"), "split")
        self.assertEqual(m.branches_seen("M3", "r"), {"left": "E14~r", "right": "E10~r"})
        self.assertEqual(m.kind_seen("S1", "r"), "merge")
        self.assertEqual(m.spur_side_at("P_red", "r"), "right")
        # 교차 패치(노랑)는 반대로도 들어온 선 그대로
        self.assertEqual(m.next_edges("E17~r"), ["E16~r"])
        # 보라에서 주황: 한 바퀴(약 5m)보다 보라 가운데에서 유턴해 되돌아가는 게 싸다
        self.assertEqual(m.shortest_path("E3", "P_orange"), ["P_purple", "M1", "S1", "P_orange"])
        self.assertTrue(m.best_next("E3", "P_orange")[1])
        self.assertFalse(m.uturn_ok("P_yellow"))                    # 교차 패치에서는 안 돎

    def test_one_way_maps_and_uturn_routes(self):
        """한쪽 방향 맵(--one-way, 예전 directed)이나 allow_reverse=False면 반대 방향을 계획하지 않는다.
        경로(주문)의 유턴은 패치 가운데·스퍼 끝에서만."""
        with io.open(SIM_MAP, encoding="utf-8") as f:
            d = json.load(f)
        one = NavMap(d, allow_reverse=False)
        self.assertNotIn("E3~r", one.edges)
        with self.assertRaises(ValueError):
            one.route_edges(["S3", "P_green"])
        two = NavMap(d)
        self.assertTrue(two.reversible)
        self.assertEqual(two.route_edges(["M1", "P_purple", "M1"]), {"E3", "E3~r"})   # 보라에서 유턴
        with self.assertRaises(ValueError):
            two.route_edges(["P_purple", "M1", "P_purple"])        # 합류(M1)에서는 유턴 못 함
        with self.assertRaises(ValueError):
            two.route_edges(["P_orange", "S1", "P_orange"])       # 갈림길에서 되돌아가기
        with self.assertRaises(ValueError):
            two.route_edges(["P_red", "P_yellow"])                 # 이어지지 않음

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

    def test_reverse_tracking(self):
        """반대 방향(반시계): 빨강에서 합류 M3이 갈림길로 보이고, 주황으로 가는 가지는 왼쪽."""
        nav = Navigator(self.m, goal="P_orange", start="P_red", start_dir="r")
        f = Feed(nav)
        f.patch("red", 0)
        nav.on_tee(70, side="right")                # 반대 방향이라 빨강 스퍼는 오른쪽
        self.assertEqual(f.split(30), ("left", None))
        self.assertEqual(nav.where()["edge"], "E14~r")
        nav.on_merge(600)                           # S1이 합류로 보임
        stop = f.patch("orange", 960)
        self.assertEqual((stop["node"], stop["reason"]), ("P_orange", "nav_arrived"))
        self.assertEqual(nav.relocalized, 0)

    def test_uturn_at_patch_while_moving(self):
        """보라 앞에서 주황으로: 보라 가운데에서 유턴(스퍼 반대쪽으로 돎). 돌고 나면 바로 앞 M1(반대로는
        갈림길)에서 S1 쪽(오른쪽) 가지를 고른 채로 따라가게 한다."""
        nav = Navigator(self.m, goal="P_orange", start="M1")
        f = Feed(nav)
        nav.on_merge(0)
        u = f.patch("purple", 40)
        self.assertEqual((u["type"], u["pos"], u["side"], u["prefer"]), ("uturn", 120, "right", "right"))
        nav.on_tee(110, side="left")                # 돌기 전에 본 보라 스퍼
        nav.uturned(120)
        self.assertEqual((nav.where()["edge"], nav.where()["dir"]), ("E3~r", "rev"))
        nav.on_split("right", 150)
        nav.on_merge(500)
        stop = f.patch("orange", 880)
        self.assertEqual(stop["reason"], "nav_arrived")

    def test_uturn_after_hold(self):
        nav = Navigator(self.m, goal="P_purple", start="P_orange", hold_at_end=True)
        f = Feed(nav)
        f.patch("orange", 0)
        f.split(370)
        nav.on_merge(820)
        self.assertEqual(f.patch("purple", 860)["reason"], "nav_hold")
        nav.set_goal("P_orange")                    # 보라 가운데에 서 있는 채로 새 목표
        u = nav.pop_stop()
        self.assertEqual((u["type"], u["pos"]), ("uturn", None))

    def test_spur_exit_backwards(self):
        """보라 스퍼에 주차 -> 주황: 스퍼 쪽으로 돌아 나와(되돌아감) 바로 앞 M1에서 오른쪽 가지."""
        nav = Navigator(self.m, goal="D_P_purple", start="P_orange", hold_at_end=True)
        f = Feed(nav)
        f.patch("orange", 0)
        f.split(370)
        nav.on_merge(820)
        f.patch("purple", 860)
        nav.on_tee(930, side="left")
        self.assertEqual(nav.pop_stop()["type"], "enter_spur")
        nav.spur_arrived("left")
        nav.set_goal("P_orange")
        nav.resumed()
        self.assertEqual(nav.spur_exit_side(), ("left", "right"))
        nav.spur_exited(940)
        self.assertEqual(nav.where()["edge"], "E3~r")

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
        # 방향은 아직 모름: 정방향(-> 주황)과 반대 방향(-> 빨강) 둘 다 후보
        self.assertEqual((w["last_node"], w["candidates"]), ("P_blue", 2))
        f.patch("orange", 6010)
        w = nav.where(6100)
        self.assertEqual((w["dir"], w["candidates"]), ("fwd", 1))
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
        nav.set_route(["P_orange", "S1", "M3", "P_red"])
        f = Feed(nav)
        f.patch("orange", 0)
        self.assertEqual(f.split(370)[0], "right")
        # 선을 잘못 타서 위 선(왼쪽)으로 감: 예약 안 된 길이니 바로 대기
        nav2 = Navigator(self.m, start="P_orange")
        nav2.set_route(["P_orange", "S1", "M3", "P_red"])
        Feed(nav2).patch("orange", 0)
        nav2.choose_split(370)
        nav2.on_split("left", 470)
        stop = nav2.pop_stop()
        self.assertEqual((stop["reason"], stop["hold"], stop["pos"]), ("nav_off_route", True, None))

    def test_hold_before_split_target(self):
        nav = Navigator(self.m, start="P_orange")
        nav.set_route(["P_orange", "S1", "M3", "P_red"], release="S1")
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

    def test_spur_goal_enters_on_tee(self):
        """스퍼 끝 목표: 부모 패치에서 무장(못 볼 때 대비 기한), 근처에서 스퍼(tee)를 보면 그 자리에서 진입."""
        nav = Navigator(self.m, goal="D_P_blue", start="P_red")
        f = Feed(nav)
        f.patch("red", 0)
        nav.on_tee(70, side="left")                 # 빨강 스퍼: 목표가 아니니 그냥 지나감
        self.assertIsNone(nav.pop_stop())
        dl = f.patch("blue", 340)
        self.assertEqual((dl["type"], dl["node"]), ("spur_deadline", "P_blue"))
        nav.on_tee(410, side="left")
        ent = nav.pop_stop()
        self.assertEqual((ent["type"], ent["via"], ent["pos"], ent["side"]), ("enter_spur", "tee", 410, "left"))
        nav.on_merge(450)                           # 스퍼 안에서 본 것은 무시
        res = nav.spur_arrived("left")
        self.assertEqual((res["node"], res["hold"], res["reason"]), ("D_P_blue", False, "nav_arrived"))
        self.assertEqual(nav.where()["last_node"], "P_blue")

    def test_spur_goal_enters_on_split(self):
        """스퍼가 갈림길로 보이면 스퍼 쪽을 고른다(아는 막다른 길로 피하지 않음)."""
        nav = Navigator(self.m, goal="D_P_red", start="P_orange")
        f = Feed(nav)
        f.patch("orange", 0)
        self.assertEqual(f.split(370)[0], "right")
        nav.pop_stop()
        f.patch("red", 1090)
        nav.pop_stop()
        self.assertEqual(nav.choose_split(1190), "left")
        self.assertIsNone(nav.known_dead())
        self.assertEqual(nav.pop_stop()["via"], "split")
        nav.on_split("left", 1200)                  # 스퍼로 들어간 갈림은 위치 추적에 안 씀
        self.assertEqual(nav.where()["last_node"], "P_red")
        self.assertIsNotNone(nav.spur_arrived("left"))

    def test_spur_missed_retry_then_stop(self):
        nav = Navigator(self.m, goal="D_P_blue", start="P_red")
        for lap in range(3):
            base = 4000 * lap
            nav.on_patch("red", base)
            nav.on_patch("blue", base + 340)
            self.assertEqual(nav.pop_stop()["type"], "spur_deadline")
            nav.spur_missed()
            stop = nav.pop_stop()
            if lap < 2:
                self.assertIsNone(stop)             # 한 바퀴 돌아 다시
                nav.on_patch("orange", base + 1350)
            else:
                self.assertEqual((stop["reason"], stop["hold"]), ("nav_spur_missed", True))
        # 정해진 경로면 바로 선다(예약 밖으로 돌 수 없음)
        nav = Navigator(self.m, start="P_red")
        nav.set_route(["P_red", "P_blue", "D_P_blue"])
        nav.on_patch("red", 0)
        nav.on_patch("blue", 340)
        nav.pop_stop()
        nav.spur_missed()
        self.assertEqual(nav.pop_stop()["reason"], "nav_spur_missed")

    def test_route_into_and_out_of_spur(self):
        """주문처럼: 스퍼 끝까지 경로 -> 주차 -> 스퍼 끝에서 시작하는 다음 경로. 나오면 부모 패치 뒤 위치로 이어 감."""
        nav = Navigator(self.m, start="P_orange", hold_at_end=True)
        nav.set_route(["P_orange", "S1", "M3", "P_red", "D_P_red"])
        f = Feed(nav)
        f.patch("orange", 0)
        self.assertEqual(f.split(370)[0], "right")
        nav.pop_stop()
        f.patch("red", 1090)
        nav.pop_stop()
        nav.on_tee(1160, side="left")
        self.assertEqual(nav.pop_stop()["type"], "enter_spur")
        res = nav.spur_arrived("left")
        self.assertEqual((res["hold"], res["node"]), (True, "D_P_red"))
        nav.set_route(["D_P_red", "P_red", "P_blue"])
        nav.resumed()
        nav.on_patch("red", 1170)                  # 나오는 동안 패치를 다시 봐도 무시
        self.assertEqual(nav.spur_exit_side(), ("right", None))   # 파랑은 들어온 방향 그대로: 스퍼 반대쪽
        nav.spur_exited(1170)
        self.assertEqual(nav.where()["edge"], "E12")
        stop = f.patch("blue", 1500)
        self.assertEqual((stop["node"], stop["hold"]), ("P_blue", True))

    def test_spur_of_tee_node(self):
        """스퍼 갈림(T) 노드의 스퍼 끝: T를 본 순간이 갈림 지점이라 바로 진입."""
        d = {"map_id": "t", "nodes": [
            {"id": "P_a", "kind": "patch", "color": "orange", "pos": [0, 0]},
            {"id": "T1", "kind": "tee", "side": "left", "pos": [400, 0]},
            {"id": "D_T1", "kind": "spur_end", "pos": [400, -150]},
            {"id": "P_b", "kind": "patch", "color": "green", "pos": [800, 0]}],
            "edges": [
                {"id": "E1", "from": "P_a", "to": "T1", "length_mm": 400},
                {"id": "E2", "from": "T1", "to": "P_b", "length_mm": 400},
                {"id": "E3", "from": "P_b", "to": "P_a", "length_mm": 1000},
                {"id": "ED", "from": "T1", "to": "D_T1", "type": "destination", "length_mm": 150}]}
        nav = Navigator(NavMap(d), goal="D_T1", start="P_a")
        nav.on_patch("orange", 0)
        nav.on_tee(395, side="left")
        ent = nav.pop_stop()
        self.assertEqual((ent["type"], ent["pos"]), ("enter_spur", 395))

    def test_gives_up_after_laps(self):
        nav = Navigator(NavMap.load(SIM_MAP, allow_reverse=False), goal="P_yellow", start="P_orange")
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


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestSimSpur(unittest.TestCase):
    """가상 매트에서 스퍼 끝에 주차하고, 다음 목표로 후진해 빠져나간다."""

    @classmethod
    def setUpClass(cls):
        cls.m = NavMap.load(SIM_MAP)
        mat = Mat(MAT_V2)
        cls.centers = dict((p["color"], p["center"]) for p in mat.d["patches"])
        cls.spurs = dict(("D_P_" + p["to"][4:], (p["points"][0], p["points"][-1]))
                         for p in mat.d["paths"] if p["to"].startswith("end_"))

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _in_spur(self, pose, node):
        """(스퍼 따라 들어간 거리, 스퍼 선에서 옆으로 벗어난 거리, 스퍼 길이) mm."""
        (jx, jy), (ex, ey) = self.spurs[node]
        L = math.hypot(ex - jx, ey - jy)
        ux, uy = (ex - jx) / L, (ey - jy) / L
        dx, dy = pose[0] - jx, pose[1] - jy
        return dx * ux + dy * uy, abs(-dx * uy + dy * ux), L

    def _park_then(self, goal, nxt, seed, start="P_orange"):
        nav = Navigator(self.m, goal=goal, start=start, hold_at_end=True)
        st = {"t": None, "pose": None, "sent": False}
        orig = Mat.camera_view

        def cam(mat, pose, c, w=320, h=240):
            cam.pose = pose
            return orig(mat, pose, c, w, h)

        def on_step(runner, t):
            if runner.state == "hold" and st["pose"] is None:
                st["t"], st["pose"] = t, cam.pose
            elif st["pose"] is not None and runner.state == "hold" and t - st["t"] > 1.0 and not st["sent"]:
                st["sent"] = True
                nav.hold_at_end = False
                nav.set_goal(nxt)
                self.assertTrue(runner.resume())
        Mat.camera_view = cam
        try:
            ev, gt = run_sim.run(MAT_V2, self.tmp, seed=seed, max_time_s=120, policy=nav, on_step=on_step)
        finally:
            Mat.camera_view = orig
        with io.open(ev, encoding="utf-8") as f:
            msgs = [json.loads(l).get("msg") for l in f]
        return st["pose"], gt, msgs

    def _check(self, goal, nxt, seed, min_depth):
        pose, gt, msgs = self._park_then(goal, nxt, seed)
        self.assertIsNotNone(pose, msgs)
        depth, side, L = self._in_spur(pose, goal)
        self.assertGreater(depth, min_depth, (depth, L))
        self.assertLess(depth, L + 5.0, (depth, L))            # 선 끝을 넘어가지 않음
        self.assertLess(side, 25.0)
        self.assertIn("spur_out_done", msgs)
        self.assertEqual(gt["finish_reason"], "nav_arrived")
        c = self.centers[nxt[2:]]
        self.assertLess(math.hypot(gt["final_pose"][0] - c[0], gt["final_pose"][1] - c[1]), 35.0)
        self.assertNotIn("nav_relocalized", msgs)
        return msgs

    def test_tee_spur_park_and_leave(self):
        msgs = self._check("D_P_orange", "P_purple", 1, 120.0)
        self.assertIn("spur_in_done", msgs)                     # J에서 제자리 회전해 들어감

    def test_spur_seen_as_split(self):
        """빨강 스퍼(곡선에서 합류한 직후)는 갈림길로 보여 그 가지를 타고 들어간다."""
        self._check("D_P_red", "P_blue", 1, 120.0)

    def test_missed_spur_retries_next_lap(self):
        """렌즈 오른쪽 가지로 와서 흔들리는 채로 초록을 지나면 스퍼를 놓친다: 한 바퀴 돌아 다시 들어간다."""
        msgs = self._check("D_P_green", "P_red", 3, 120.0)
        self.assertIn("nav_spur_retry", msgs)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestSimReverse(unittest.TestCase):
    """양방향 맵: 반시계 방향 주행, 패치 가운데 유턴, 스퍼에서 되돌아 나오기."""

    @classmethod
    def setUpClass(cls):
        cls.m = NavMap.load(SIM_MAP)
        cls.centers = dict((p["color"], p["center"]) for p in Mat(MAT_V2).d["patches"])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, goals, start="P_orange", start_dir="f", pose=None, seed=1):
        nav = Navigator(self.m, start=start, start_dir=start_dir, hold_at_end=len(goals) > 1)
        st = {"i": 0, "t": None}

        def go(g):
            nav.set_route(g) if isinstance(g, list) else nav.set_goal(g)
        go(goals[0])

        def on_step(runner, t):
            if runner.state != "hold":
                return
            if st["t"] is None:
                st["t"] = t
            elif t - st["t"] > 1.0 and st["i"] + 1 < len(goals):
                st["i"] += 1
                nav.hold_at_end = st["i"] + 1 < len(goals)
                go(goals[st["i"]])
                runner.resume()
                st["t"] = None
        ev, gt = run_sim.run(MAT_V2, self.tmp, seed=seed, max_time_s=90, policy=nav, start_pose=pose,
                             on_step=on_step)
        with io.open(ev, encoding="utf-8") as f:
            evs = [json.loads(l) for l in f]
        last = goals[-1][-1] if isinstance(goals[-1], list) else goals[-1]
        self.assertEqual(gt["finish_reason"], "nav_arrived", [e.get("msg") for e in evs])
        if last.startswith("P_"):
            c = self.centers[last[2:]]
            self.assertLess(math.hypot(gt["final_pose"][0] - c[0], gt["final_pose"][1] - c[1]), 35.0)
        msgs = [e.get("msg") for e in evs]
        self.assertNotIn("nav_relocalized", msgs)
        return gt, msgs

    def test_counterclockwise(self):
        gt, msgs = self._run(["P_blue"], start_dir="r", pose=[700, 842, 180])
        h = (gt["final_pose"][2] + 180.0) % 360.0 - 180.0
        self.assertLess(abs(h), 20.0)          # 아래 선을 오른쪽(+x)으로 달려 파랑에 섬 = 반시계

    def test_uturn_after_hold_and_while_moving(self):
        gt, msgs = self._run(["P_purple", "P_orange"])
        self.assertIn("uturn_done", msgs)
        self.assertAlmostEqual(abs(gt["final_pose"][2]), 180.0, delta=20)       # 돌아서 왔던 쪽을 봄
        gt, msgs = self._run(["P_orange"], start="M1", pose=[1050, 842, 0], seed=2)
        self.assertIn("uturn_done", msgs)

    def test_route_spur_round_trip(self):
        """주문처럼: 빨강 스퍼에 주차 -> 스퍼에서 되돌아 나와 바로 앞 M3에서 S1 쪽 가지 -> 주황."""
        gt, msgs = self._run([["P_orange", "S1", "M3", "P_red", "D_P_red"],
                              ["D_P_red", "P_red", "M3", "S1", "P_orange"]])
        self.assertIn("spur_out_done", msgs)
        self.assertNotIn("nav_off_route", msgs)

    def test_reverse_into_spur_then_forward(self):
        """반시계로 와서 빨강 스퍼(오른쪽)에 주차 -> 시계 방향으로 나가 보라."""
        gt, msgs = self._run(["D_P_red", "P_purple"], start_dir="r", pose=[700, 842, 180])
        self.assertIn("spur_out_done", msgs)


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestExploreCliGoto(unittest.TestCase):
    """explore.py goto 실행 루프 그대로(카메라·모터·시계만 가상 매트로 바꿔 끼움)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args):
        import agv.explore as ex
        mat = Mat(MAT_V2)
        clock = {"t": 1.7e9}
        pose = list(mat.d["start_pose"][:2]) + [math.radians(mat.d["start_pose"][2])]

        class Motors(object):
            l = r = 0.0

            def __init__(self, *a, **k):
                pass

            def set(self, l, r):
                Motors.l, Motors.r = l, r

            def stop(self):
                Motors.l = Motors.r = 0.0

        class Cam(object):
            fps = 20.0
            last = [None]

            def read(self):
                t = clock["t"]
                dt = 0.0 if Cam.last[0] is None else t - Cam.last[0]
                Cam.last[0] = t
                vl, vr = Motors.l * 500.0, Motors.r * 500.0
                pose[2] -= (vl - vr) / 140.0 * dt
                v = (vl + vr) / 2.0
                pose[0] += v * math.cos(pose[2]) * dt
                pose[1] += v * math.sin(pose[2]) * dt
                clock["t"] += 0.05
                return mat.camera_view(tuple(pose), run_sim.CAM)

            def close(self):
                pass

        saved = (ex.open_camera, ex.JetbotMotors, ex.time.time, ex.time.sleep)
        ex.open_camera = lambda *a, **k: Cam()
        ex.JetbotMotors = Motors
        ex.time.time = lambda: clock["t"]
        ex.time.sleep = lambda s: None
        try:
            rc = ex.main(args + ["--map", SIM_MAP, "--out", os.path.join(self.tmp, "run"), "--no-arm",
                                 "--debug-every", "0", "--max-time", "120", "--enable-motors"])
        finally:
            ex.open_camera, ex.JetbotMotors, ex.time.time, ex.time.sleep = saved
        with io.open(os.path.join(self.tmp, "run", "events.jsonl"), encoding="utf-8") as f:
            evs = [json.loads(l) for l in f]
        return rc, evs, pose

    def test_goto_spur_then_patch(self):
        rc, evs, pose = self._run(["--mode", "goto", "--start", "P_orange", "--to", "D_P_red",
                                   "--then", "P_blue", "--wait", "1"])
        msgs = [e.get("msg") for e in evs]
        self.assertEqual(rc, 0)
        self.assertEqual(evs[-1]["reason"], "nav_arrived")
        for m in ("spur_parked", "spur_out_done", "resume"):
            self.assertIn(m, msgs)
        blue = [p["center"] for p in Mat(MAT_V2).d["patches"] if p["color"] == "blue"][0]
        self.assertLess(math.hypot(pose[0] - blue[0], pose[1] - blue[1]), 40.0)

    def test_bad_goal_exit_code(self):
        import agv.explore as ex
        self.assertEqual(ex.main(["--mode", "goto", "--map", SIM_MAP, "--to", "P_pink", "--enable-motors"]), 2)
