# -*- coding: utf-8 -*-
"""가상 매트에서 실물과 같은 인식·탐색 코드를 돌려 맵을 끝까지 만드는 테스트.

실제 카메라·모터는 쓰지 않는다. sim/mat/ 폴더가 필요하다(노트북에서 실행).
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sim", "mat"))

try:
    import run_sim  # noqa: E402
    from mat import Mat  # noqa: E402
except ImportError:  # Jetson에 sim/mat/이 없으면 건너뜀
    run_sim = None

from agv.linecam import DEFAULT_CONFIG, JunctionTracker, PatchTracker, analyze  # noqa: E402
from agv.topo import build_map, read_events  # noqa: E402

MAT = os.path.join(ROOT, "sim", "mat", "mats", "ssafy_mat.json")
MAT_V2 = os.path.join(ROOT, "sim", "mat", "mats", "ssafy_v2_fix.json")


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
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

    @staticmethod
    def _vee_along(mat, way, sub):
        """시뮬레이터 주행에서 뽑은 자세(x, y, 각도) 사이를 sub 프레임씩 이어 가며 vee 이벤트를 모은다."""
        import math
        jt = JunctionTracker(DEFAULT_CONFIG)
        out = []
        for (x0, y0, t0), (x1, y1, t1) in zip(way[:-1], way[1:]):
            for k in range(sub):
                f = float(k) / sub
                pose = (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, math.radians(t0 + (t1 - t0) * f))
                out += [e for e in jt.update(analyze(mat.camera_view(pose, run_sim.CAM), DEFAULT_CONFIG))
                        if e["type"] == "vee"]
        return out

    def test_vee_branch_on_curve(self):
        v2 = Mat(os.path.join(ROOT, "sim", "mat", "mats", "ssafy_v2.json"))
        # 오른쪽 고리 오른쪽 아래 모서리를 돌며: 안쪽(오른쪽)으로 급하게 꺾여 나가는 S자 가지
        corner = [(1827.9, 474.3, -90.9), (1827.5, 444.3, -90.8), (1826.8, 414.1, -91.6),
                  (1825.4, 384.9, -93.9), (1822.3, 356.5, -97.7), (1817.0, 329.5, -103.0),
                  (1810.0, 304.0, -108.5), (1801.0, 280.0, -114.0)]
        self.assertEqual([e["side"] for e in self._vee_along(v2, corner, 4)], ["right"])
        # 왼쪽 고리 오른쪽 변이 빨강 앞에서 아래 선에 붙음: 옆(바깥쪽)에서 붙는 T자 합류는 V자가 아님
        # (휨 조건이 없으면 여기서 왼쪽 V자로 잘못 잡힌다)
        tee = [(855.7, 433.2, -91.2), (855.3, 418.1, -91.5), (854.8, 403.0, -92.3), (854.0, 388.6, -93.4),
               (852.9, 374.2, -94.9), (851.4, 360.1, -96.9), (849.3, 346.2, -99.3), (846.4, 332.2, -102.4),
               (842.9, 319.1, -105.7), (838.6, 306.4, -109.7), (833.4, 294.3, -113.9), (827.0, 283.0, -118.0)]
        self.assertEqual(self._vee_along(v2, tee, 2), [])

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


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
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


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
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
        # 팀 공통 맵(agv-map-v1): 트윈 로더가 그대로 읽고, 스퍼 끝은 destination 노드
        from agv.topo import to_agv_map_v1, twin_problems
        d = to_agv_map_v1(m)
        self.assertEqual(twin_problems(d), [])
        dests = [n for n in d["nodes"] if n["type"] == "destination"]
        self.assertEqual(sorted(n["spur_of"] for n in dests), sorted(
            [n["id"] for n in m["nodes"] if n.get("spur")] + [t["id"]]))
        P = dict((n["id"], (n["x"], n["y"])) for n in d["nodes"])
        self.assertEqual(P[d["origin_node"]], (0.0, 0.0))
        # 시계 방향으로 도는 맵: 고리 안쪽은 원점에서 진행 방향 오른쪽(+y)이라 노드들의 무게중심이 +y 쪽
        ys = [n["y"] for n in d["nodes"] if n["type"] != "destination"]
        self.assertGreater(sum(ys) / len(ys), 0.2)


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestDeadEndRecovery(unittest.TestCase):
    """도안 그대로(ssafy_v2): 곡선에서 합류한 직후의 스퍼를 갈림길로 착각해 들어가도 되돌아 나온다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_back_out_of_red_spur(self):
        # 왼쪽 고리 오른쪽 변을 내려가다 빨강 스퍼로 들어감 -> 후진 -> 회전 -> 파랑으로 계속
        ev, gt = run_sim.run(os.path.join(ROOT, "sim", "mat", "mats", "ssafy_v2.json"), self.tmp, seed=1,
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
        ev, gt = run_sim.run(os.path.join(ROOT, "sim", "mat", "mats", "ssafy_v2.json"), self.tmp, seed=0,
                             max_time_s=300)
        self.assertEqual(gt["finish_reason"], "explore_explored")
        m = build_map([read_events(ev)])
        nodes = dict((n["id"], n) for n in m["nodes"])
        self.assertEqual(len([n for n in m["nodes"] if n["type"] == "patch"]), 6)
        self.assertIsNotNone(nodes["P_red"]["spur"]["length_mm"])
        self.assertTrue(m["explore"]["complete"])
        # 오른쪽 아래 모서리의 급한 S자 가지(V자)에 들어가 S자 선과 두 교차까지 탐색
        evs = read_events(ev)
        self.assertIn("vee_done", [e.get("msg") for e in evs])
        count = {}
        for n in m["nodes"]:
            count[n["type"]] = count.get(n["type"], 0) + 1
        self.assertEqual((count.get("split"), count.get("merge"), count.get("cross")), (3, 3, 2),
                         m["warnings"])


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestAutoColor(unittest.TestCase):
    """노란 조명(백열등) 아래: 기본 HSV로는 바탕이 주황으로 잡혀 탐색이 망가지지만,
    한 바퀴 영상에서 자동으로 잡은 색으로는 끝까지 탐색한다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_warm_light(self):
        import cv2
        import numpy as np
        from agv.tools import autocolor as ac
        gain = np.array((0.70, 0.92, 1.12), np.float32)   # B, G, R
        orig = Mat.camera_view

        def warm(mat, pose, cam, w=320, h=240):
            return np.clip(orig(mat, pose, cam, w, h).astype(np.float32) * gain, 0, 255).astype(np.uint8)
        Mat.camera_view = warm
        try:
            lap = os.path.join(self.tmp, "lap")
            run_sim.run(MAT_V2, lap, seed=3, max_time_s=75, raw_video=True)
            cap = cv2.VideoCapture(os.path.join(lap, "camera.mp4"))
            frames = []
            while True:
                ok, f = cap.read()
                if not ok:
                    break
                frames.append(f)
            frames = frames[::2]
            col = ac.Collector(DEFAULT_CONFIG, ac.base_stats(frames, DEFAULT_CONFIG))
            for f in frames:
                col.add(f)
            res, info = ac.solve(col, DEFAULT_CONFIG)
            self.assertEqual(sorted(c["name"] for c in info["clusters"]),
                             sorted(DEFAULT_CONFIG["patch"]["colors"]), info["warnings"])
            ev, gt = run_sim.run(MAT_V2, os.path.join(self.tmp, "run"), seed=3, max_time_s=300,
                                 cfg_override=res)
        finally:
            Mat.camera_view = orig
        self.assertEqual(gt["finish_reason"], "explore_explored")
        m = build_map([read_events(ev)])
        self.assertEqual(len([n for n in m["nodes"] if n["type"] == "patch"]), 6)
        self.assertTrue(m["explore"]["complete"])

    def test_names_follow_hue_order(self):
        from agv.tools.autocolor import _assign_names, _range_center
        nominal = dict((k, _range_center(v)) for k, v in DEFAULT_CONFIG["patch"]["colors"].items())
        # 조명 때문에 전부 8 정도 밀려도 순서대로 이름이 붙는다
        got = _assign_names([8, 20, 36, 73, 117, 155], nominal)
        self.assertEqual([got[h] for h in [8, 20, 36, 73, 117, 155]],
                         ["red", "orange", "yellow", "green", "blue", "purple"])
        # 하나가 빠져도(초록 없음) 나머지는 맞게
        got = _assign_names([2, 13, 28, 110, 147], nominal)
        self.assertEqual(sorted(got.values()), ["blue", "orange", "purple", "red", "yellow"])


@unittest.skipIf(run_sim is None, "sim/mat/ 없음")
class TestSpeedCal(unittest.TestCase):
    """실제 속도가 설정보다 20% 느리고 1분에 5%씩 더 느려져도(배터리), 패치로 속도를 다시 재서
    패치 사이 거리가 맞게 나온다. 보정이 없으면 25~30% 길게 나온다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_battery_drift(self):
        import numpy as np
        ev, gt = run_sim.run(MAT_V2, self.tmp, seed=3, max_time_s=300, speed_scale=0.8, speed_drift=0.05,
                             cfg_override={"patch": {"length_mm": 159.3}})   # 도안 패치 진행 방향 길이 평균
        self.assertEqual(gt["finish_reason"], "explore_explored")
        evs = read_events(ev)
        ot = np.array(gt["odo_true"])
        pts = [e for e in evs if e["type"] == "patch"]
        errs = []
        for a, b in zip(pts[1:-1], pts[2:]):      # 첫 패치를 지나 첫 측정이 들어간 뒤부터
            true = np.interp(b["ts"], ot[:, 0], ot[:, 1]) - np.interp(a["ts"], ot[:, 0], ot[:, 1])
            errs.append((b["odo_mm"] - a["odo_mm"]) / true - 1)
        self.assertLess(abs(np.median(errs)), 0.04, errs)
        self.assertLess(np.percentile(np.abs(errs), 90), 0.12, errs)
        cal = [e for e in evs if e.get("msg") == "speed_cal" and "reject" not in e]
        self.assertGreaterEqual(len(cal), 4)
        # 마지막 보정값이 그 시점 실제 속도와 6% 안
        t_last = cal[-1]["ts"] - evs[0]["ts"]
        true_k = 150.0 * 0.8 * (1 - 0.05 * t_last / 60.0)
        self.assertLess(abs(cal[-1]["k"] / true_k - 1), 0.06, (cal[-1], true_k))


class TestSpeedCalUnit(unittest.TestCase):
    def test_exact_on_synthetic_patch(self):
        import numpy as np
        from agv.speedcal import SpeedCal
        from agv.linecam import merge_config

        class An(object):
            pass
        cfg = merge_config(DEFAULT_CONFIG, {"patch": {"length_mm": 150.0}})
        cal = SpeedCal(cfg, k0=200.0)
        k_true, du = 120.0, 0.05                     # 기본 속도 1초 = 120mm, 프레임마다 0.05초어치
        rows = np.linspace(400, 120, 10)             # 줄마다 바닥 거리(mm), 위 줄이 멀다
        res = None
        for i in range(120):
            u = i * du
            front = 450 - k_true * u                 # 패치 앞 가장자리 위치(로봇 기준 mm)
            an = An()
            an.sat_rows = [float(np.clip((r - front) / 8.0 + 0.5, 0, 1) * np.clip((front + 150 - r) / 8.0 + 0.5, 0, 1)) * 0.6
                           for r in rows]
            res = cal.update(an, u, (0.3, 0.3)) or res
        self.assertIsNotNone(res)
        self.assertEqual(res["rows"], 10)
        self.assertAlmostEqual(cal.k, k_true, delta=1.5)


if __name__ == "__main__":
    unittest.main()
