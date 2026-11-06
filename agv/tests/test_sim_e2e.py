# -*- coding: utf-8 -*-
"""가상 매트에서 실물과 같은 인식·탐색 코드를 돌려 맵을 끝까지 만드는 테스트.

실제 카메라·모터는 쓰지 않는다. sim/ 폴더가 필요하다(노트북에서 실행).
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sim"))

try:
    import run_sim  # noqa: E402
    from mat import Mat  # noqa: E402
except ImportError:  # Jetson에 sim/이 없으면 건너뜀
    run_sim = None

from agv.linecam import DEFAULT_CONFIG, JunctionTracker, PatchTracker, analyze  # noqa: E402
from agv.topo import build_map, read_events  # noqa: E402

MAT = os.path.join(ROOT, "sim", "mats", "ssafy_mat.json")
MAT_V2 = os.path.join(ROOT, "sim", "mats", "ssafy_v2_fix.json")


@unittest.skipIf(run_sim is None, "sim/ 없음")
class TestPerception(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mat = Mat(MAT)

    def test_single_line_is_one_segment(self):
        an = analyze(self.mat.camera_view((600, 232, 0), run_sim.CAM), DEFAULT_CONFIG)
        counts = [len(s) for _, s in an.rows]
        self.assertTrue(all(c == 1 for c in counts), counts)

    def test_patch_detected(self):
        pt = PatchTracker(DEFAULT_CONFIG)
        got = None
        for x in range(700, 1000, 10):
            got = pt.update(analyze(self.mat.camera_view((x, 232, 0), run_sim.CAM), DEFAULT_CONFIG)) or got
        self.assertEqual(got, "orange")

    def test_fork_visible_before_split(self):
        import math
        jt = JunctionTracker(DEFAULT_CONFIG)
        seen = False
        # 오른쪽 분기(S2) 아래에서 위로 다가가며
        for k in range(12):
            y = 760 + 15 * k
            jt.update(analyze(self.mat.camera_view((1745 - 0.35 * (y - 760), y, math.radians(105)),
                                                   run_sim.CAM), DEFAULT_CONFIG))
            seen = seen or jt.fork_ahead
        self.assertTrue(seen)


@unittest.skipIf(run_sim is None, "sim/ 없음")
class TestSimExploration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _check(self, seed):
        ev, gt = run_sim.run(MAT, self.tmp, seed=seed, max_time_s=300)
        self.assertEqual(gt["finish_reason"], "explore_explored")
        m = build_map([read_events(ev)])
        types = sorted(n["type"] for n in m["nodes"])
        self.assertEqual(types, ["merge"] * 2 + ["patch"] * 4 + ["split"] * 2, m["warnings"])
        self.assertEqual(len(m["edges"]), 10, m["warnings"])
        self.assertTrue(m["explore"]["complete"])
        # 노랑 패치 뒤 분기(왼쪽 큰 분기)의 두 가지 길이가 정답(898/1007mm)과 15% 안
        sy = [e["to"] for e in m["edges"] if e["from"] == "P_yellow"][0]
        lens = sorted(e["length_mm"] for e in m["edges"] if e["from"] == sy)
        for got, want in zip(lens, sorted(p["length_mm"] for p in gt["paths"] if p["from"] == "S1")):
            self.assertLess(abs(got - want) / want, 0.15, (lens, want))

    def test_seed0(self):
        self._check(0)

    def test_seed7(self):
        self._check(7)


@unittest.skipIf(run_sim is None, "sim/ 없음")
class TestSimV2(unittest.TestCase):
    """2m x 1m 이은 맵(수정안): X자 교차 2곳, 스퍼, 렌즈, 갈림길 3곳."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_explore_v2_fix(self):
        ev, gt = run_sim.run(MAT_V2, self.tmp, seed=3, max_time_s=300)
        self.assertEqual(gt["finish_reason"], "explore_explored")
        m = build_map([read_events(ev)])
        count = {}
        for n in m["nodes"]:
            count[n["type"]] = count.get(n["type"], 0) + 1
        self.assertEqual(count, {"patch": 6, "split": 3, "merge": 3, "cross": 2, "tee": 1}, m["warnings"])
        self.assertEqual(len(m["edges"]), 19)
        self.assertTrue(m["explore"]["complete"])
        nodes = dict((n["id"], n) for n in m["nodes"])
        # 노랑 패치 위 교차: 두 선이 각각 직진으로만 지나감
        y = nodes["P_yellow"]
        self.assertTrue(y["crossing"])
        self.assertEqual(len(y["through"]), 2)
        ins = set(t[0] for t in y["through"])
        outs = set(t[1] for t in y["through"])
        self.assertEqual((len(ins), len(outs)), (2, 2))
        # 아래 선의 두 번째 교차는 선마다 노드가 하나씩 생기고 같은 자리로 짝지어진다
        xs = [n for n in m["nodes"] if n["type"] == "cross"]
        self.assertEqual(xs[0].get("same_spot"), [xs[1]["id"]])
        # 스퍼: 주황·파랑은 패치 위, 빨강은 패치 뒤 따로(T 노드)
        self.assertEqual(nodes["P_blue"]["spur"]["side"], "left")
        self.assertIsNone(nodes["P_purple"]["spur"])
        self.assertIsNone(nodes["P_yellow"]["spur"])
        t = [n for n in m["nodes"] if n["type"] == "tee"][0]
        self.assertTrue(any(e["from"] == "P_red" and e["to"] == t["id"] for e in m["edges"]))


@unittest.skipIf(run_sim is None, "sim/ 없음")
class TestDeadEndRecovery(unittest.TestCase):
    """도안 그대로(ssafy_v2): 곡선에서 합류한 직후의 스퍼를 갈림길로 착각해 들어가도 되돌아 나온다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_back_out_of_red_spur(self):
        # 왼쪽 고리 오른쪽 변을 내려가다 빨강 스퍼로 들어감 -> 후진 -> 회전 -> 파랑으로 계속
        ev, gt = run_sim.run(os.path.join(ROOT, "sim", "mats", "ssafy_v2.json"), self.tmp, seed=1,
                             max_time_s=12, start_pose=[853.4, 549, -90])
        evs = read_events(ev)
        types = [e["type"] for e in evs]
        self.assertIn("dead_end", types)
        de = [e for e in evs if e["type"] == "dead_end"][0]
        self.assertAlmostEqual(de["spur_mm"], 188, delta=50)    # 도안 스퍼 길이 약 188mm
        after = types[types.index("dead_end"):]
        self.assertIn("patch", after)
        self.assertEqual([e for e in evs if e["type"] == "patch"][-1]["color"], "blue")
        self.assertNotIn("line_lost", [e.get("reason") for e in evs])

    def test_explore_drawn_map(self):
        ev, gt = run_sim.run(os.path.join(ROOT, "sim", "mats", "ssafy_v2.json"), self.tmp, seed=0,
                             max_time_s=300)
        self.assertEqual(gt["finish_reason"], "explore_explored")
        m = build_map([read_events(ev)])
        nodes = dict((n["id"], n) for n in m["nodes"])
        self.assertEqual(len([n for n in m["nodes"] if n["type"] == "patch"]), 6)
        self.assertIsNotNone(nodes["P_red"]["spur"]["length_mm"])
        self.assertTrue(m["explore"]["complete"])


if __name__ == "__main__":
    unittest.main()
