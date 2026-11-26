# -*- coding: utf-8 -*-
"""새 매트(2026-11-25, 트윈 맵 warehouse_01) 테스트: 맵 파일 모양, 가상 매트에서 목표 가기, 서버 주문.

맵은 mapping/design_map.py로 sim/mat/mats/ssafy_v3.json(가상 매트)을 탐색해 만들고, 노드 이름·좌표를
트윈 맵(maps/twin/warehouse_01.json)에 맞춘 것이다(mapping/twin_ids.py). 더 많은 시나리오는
sim/mat/v3_scenarios.py.
"""
import math
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mapping"))

from agv.link import FakeLink, LinkController  # noqa: E402
from agv.nav import NavMap, Navigator  # noqa: E402
from agv.tests.test_link import load, order  # noqa: E402
from agv.topo import twin_problems  # noqa: E402
from unify_map import unify  # noqa: E402

try:
    sys.path.insert(0, os.path.join(ROOT, "sim", "mat"))
    import run_sim  # noqa: E402
    import v3_scenarios  # noqa: E402
except ImportError:
    run_sim = v3_scenarios = None

MAP = os.path.join(ROOT, "maps", "mat_ssafy_v3.json")
TWIN = os.path.join(ROOT, "maps", "twin", "warehouse_01.json")


class TestMapV3File(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load(MAP)
        cls.twin = load(TWIN)
        cls.nodes = dict((n["id"], n) for n in cls.m["nodes"])
        cls.tnodes = dict((n["id"], n) for n in cls.twin["nodes"])

    def test_twin_ids(self):
        ids = set(self.nodes)
        self.assertEqual(set(i for i in ids if i[0] == "N"), set("N0%d" % k for k in range(1, 9)))
        self.assertEqual(set(i for i in ids if i[0] == "D"), set("D0%d" % k for k in range(1, 9)))
        self.assertEqual(set(i for i in ids if i[0] == "J"), {"J02", "J03", "J06", "J07"})   # T자 갈림길만
        self.assertEqual(self.m["twin_ids"]["unmatched_junctions"], [])

    def test_patch_and_spur_positions_match_twin(self):
        for nid, n in self.nodes.items():
            if nid[0] in "ND":
                t = self.tnodes[nid]
                self.assertAlmostEqual(n["x"], t["x"], places=3, msg=nid)
                self.assertAlmostEqual(n["y"], t["y"], places=3, msg=nid)
        for nid in ("J02", "J03", "J06", "J07"):      # 로봇이 알아보는 자리: 트윈 그림과 몇 cm 안
            n, t = self.nodes[nid], self.tnodes[nid]
            self.assertLess(math.hypot(n["x"] - t["x"], n["y"] - t["y"]), 0.05, nid)

    def test_roles_and_charging(self):
        self.assertEqual(self.nodes["N02"]["role"], "shelf_milk")
        self.assertEqual(self.nodes["N07"]["role"], "charge_agv01")
        self.assertEqual(self.nodes["D07"].get("purpose"), "charging")
        self.assertEqual(self.nodes["N07"]["color"], "pink")
        self.assertEqual(self.nodes["N08"]["color"], "skyblue")

    def test_topology(self):
        nm = NavMap(self.m)
        self.assertEqual(nm.kind("J02"), "split")
        self.assertEqual(nm.kind("J03"), "merge")
        main = [(e["from"], e["to"]) for e in self.m["edges"] if e["type"] == "main"]
        self.assertIn(("J02", "J07"), main)       # 안쪽 왼쪽 세로선
        self.assertIn(("J06", "J03"), main)       # 안쪽 오른쪽 세로선
        self.assertEqual(len([e for e in self.m["edges"] if e["type"] == "destination"]), 8)

    def test_unified_and_twin_loader(self):
        self.assertEqual(unify(self.m), self.m)
        self.assertEqual(self.m["unit"], "mm")
        self.assertEqual(twin_problems(self.m), [])


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestSimGotoV3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = v3_scenarios.World()

    def check(self, r, n_goals, tol=35.0):
        self.assertEqual(r["finish"], "nav_arrived", r)
        self.assertEqual(r["goals_done"], n_goals, r)
        err = r["err"] if not isinstance(r["err"], tuple) else max(r["err"])
        self.assertLess(err, tol, r)
        self.assertEqual(r["relocalized"], 0, r)

    def test_inner_left_line(self):
        r = v3_scenarios.run_case(self.w, [["N01", "J02", "J07", "N03"]])
        self.check(r, 1)
        self.assertEqual(r["patches"], ["orange", "red"])

    def test_inner_right_line(self):
        r = v3_scenarios.run_case(self.w, ["N06", ["N06", "J06", "J03", "N04"]])
        self.check(r, 2)

    def test_charging_spur_and_back(self):
        r = v3_scenarios.run_case(self.w, ["D07", "N01"])
        self.check(r, 2)
        self.assertIn("spur_out_done", r["msgs"])

    def test_unknown_start_slow_robot(self):
        r = v3_scenarios.run_case(self.w, ["N05"], known_start=False, speed_scale=0.85, seed=7)
        self.check(r, 1)
        self.assertGreater(r["speed_cal"], 0)

    def test_spur_right_after_corner(self):
        """N01 스퍼는 왼쪽 위 모서리 끝에서 17cm: 모서리를 막 돌아 비스듬한 채로 봐도 들어간다
        (예전 스퍼 검출은 10번 중 6~8번 놓쳤다)."""
        for seed in (2, 3, 5):
            r = v3_scenarios.run_case(self.w, [["N07", "N01", "D01"]], start="N07", pose=[148, 380, 90],
                                      seed=seed, max_time_s=60)
            self.check(r, 1)

    def test_charging_patch_speed_cal(self):
        """충전 패치(14.9cm)를 지나도 속도 보정이 맞게(색별 길이)."""
        r = v3_scenarios.run_case(self.w, ["N08", "N03"], speed_scale=0.9, seed=4)
        self.check(r, 2)


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestLapAutocolorV3(unittest.TestCase):
    def test_lap_sees_eight_colors(self):
        """현장 1~2단계 그대로: lap 모드(갈림길마다 왼쪽)로 바깥 고리를 돌면 패치 8개를 다 지나고,
        autocolor가 8색(분홍·하늘 포함)을 다 잡는다."""
        import cv2
        from agv.linecam import DEFAULT_CONFIG
        from agv.tools import autocolor as ac
        tmp = tempfile.mkdtemp()
        try:
            _, gt = run_sim.run(v3_scenarios.MAT, tmp, seed=3, max_time_s=240, raw_video=True, mode="lap",
                                start_pose=v3_scenarios.TOP_START)
            self.assertEqual(gt["finish_reason"], "lap_closed")
            cap = cv2.VideoCapture(os.path.join(tmp, "camera.mp4"))
            frames = []
            while True:
                ok, f = cap.read()
                if not ok:
                    break
                frames.append(f)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        frames = frames[::2]
        col = ac.Collector(DEFAULT_CONFIG, ac.base_stats(frames, DEFAULT_CONFIG))
        for f in frames:
            col.add(f)
        res, info = ac.solve(col, DEFAULT_CONFIG)
        self.assertEqual(sorted(c["name"] for c in info["clusters"]), sorted(DEFAULT_CONFIG["patch"]["colors"]))


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestSimServeV3(unittest.TestCase):
    """서버 주문(FakeLink): N01 -> 안쪽 왼쪽 세로선 -> 휴지 선반 스퍼(D03) 주차 -> 새 작업으로 N01 스퍼(D01)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_order_into_shelf_spur_and_back(self):
        raw = load(MAP)
        ref = "%s@v%s" % (raw["map_id"], raw["version"])
        r1 = ["N01", "J02", "J07", "N03", "D03"]
        r2 = ["D03", "N03", "N02", "N07", "N01", "D01"]
        plan = {0: order(1, "D03", r1, oid="A", ref=ref), 1: order(1, "D01", r2, oid="B", ref=ref)}
        nav = Navigator(NavMap(raw), start="N01", stop_offset_mm=80, hold_at_end=True)
        link = FakeLink("sim")
        notes = []
        ctl = LinkController(link, nav, raw, start_node="N01", log=lambda m, **k: notes.append((m, k)))
        st = {"runner": None, "sent": set()}

        def on_step(runner, t):
            if st["runner"] is None:
                st["runner"] = runner
                ctl.attach(runner)
            s = ctl.step(now=t)
            n = len(link.events)
            if n in plan and n not in st["sent"] and s["status"] in ("IDLE", "WAITING"):
                st["sent"].add(n)
                link.send(plan[n])
            if n == 2 and s["status"] == "IDLE":
                runner.stop("test_done")
        ev, gt = run_sim.run(v3_scenarios.MAT, self.tmp, seed=2, max_time_s=120, policy=nav, on_step=on_step,
                             start_pose=v3_scenarios.TOP_START, cfg_override=v3_scenarios.CFG)
        self.assertEqual(gt["finish_reason"], "test_done", [n for n in notes if n[0] != "link_order"])
        self.assertEqual([(e["order_id"], e["stopped_at"]) for e in link.events], [("A", "D03"), ("B", "D01")])
        self.assertFalse([k for m, k in notes if m == "link_order_rejected"])
        w = v3_scenarios.World()
        along, side = w.target_err("D01", gt["final_pose"])
        self.assertLess(max(abs(along), side), 40.0)

    def test_release_at_merge_on_patch(self):
        """J07(합류점)은 N03 패치 위에 겹쳐 있다. 해제 노드가 J07이면 N03 패치 앞(스퍼 갈림 전)에 서서
        stopped_at J07을 보내고, 다음 해제(D03)에 스퍼로 들어간다. 예전에는 합류 판정이 패치에 묻혀
        nav_off_route로 섰고, 패치에 선 뒤 그 스퍼로 이어 가지도 못했다."""
        raw = load(MAP)
        ref = "%s@v%s" % (raw["map_id"], raw["version"])
        r1 = ["N01", "J02", "J07", "N03", "D03"]
        r2 = ["D03", "N03", "N02", "N07", "N01", "D01"]
        plan = {0: order(1, "J07", r1, oid="A", ref=ref), 1: order(2, "D03", r1, oid="A", ref=ref),
                2: order(1, "D01", r2, oid="B", ref=ref)}
        nav = Navigator(NavMap(raw), start="N01", stop_offset_mm=80, hold_at_end=True)
        link = FakeLink("sim")
        notes = []
        ctl = LinkController(link, nav, raw, start_node="N01", log=lambda m, **k: notes.append((m, k)))
        st = {"runner": None, "sent": set()}

        def on_step(runner, t):
            if st["runner"] is None:
                st["runner"] = runner
                ctl.attach(runner)
            s = ctl.step(now=t)
            n = len(link.events)
            if n in plan and n not in st["sent"] and s["status"] in ("IDLE", "WAITING"):
                st["sent"].add(n)
                link.send(plan[n])
            if n == 3 and s["status"] == "IDLE":
                runner.stop("test_done")
        ev, gt = run_sim.run(v3_scenarios.MAT, self.tmp, seed=2, max_time_s=150, policy=nav, on_step=on_step,
                             start_pose=v3_scenarios.TOP_START, cfg_override=v3_scenarios.CFG)
        self.assertEqual(gt["finish_reason"], "test_done", [n for n in notes if n[0] != "link_order"])
        self.assertEqual([(e["order_id"], e["seq"], e["stopped_at"]) for e in link.events],
                         [("A", 1, "J07"), ("A", 2, "D03"), ("B", 1, "D01")])
        self.assertFalse([k for m, k in notes if m in ("link_order_rejected", "link_error")])


if __name__ == "__main__":
    unittest.main()
