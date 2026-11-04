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


if __name__ == "__main__":
    unittest.main()
