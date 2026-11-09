# -*- coding: utf-8 -*-
"""렌즈 보정 계산(agv.tools.calib) 테스트: 알려진 어안 렌즈로 만든 체커보드 사진에서 값을 되찾는다."""
import os
import shutil
import sys
import tempfile
import unittest

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from agv.camcalib import CameraCalib, scale_K  # noqa: E402
from agv.linecam import DEFAULT_CONFIG, merge_config  # noqa: E402
from agv.tests.synth_calib import make_set  # noqa: E402
from agv.tools import calib  # noqa: E402

SIZE = (640, 360)
K_TRUE = np.array([[260.0, 0, 322], [0, 262.0, 178], [0, 0, 1]])
D_TRUE = np.array([-0.02, 0.015, -0.006, 0.001])


class TestCameraCalib(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        make_set(cls.tmp, "fisheye", K_TRUE, D_TRUE, SIZE, n=18, seed=0)
        cls.cfg = merge_config(DEFAULT_CONFIG, {"camera": {"capture_width": SIZE[0], "capture_height": SIZE[1],
                                                           "width": 320, "height": 240}})
        cls.out, cls.info = calib.run(cls.tmp, cls.cfg, quiet=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_picks_fisheye_and_recovers_intrinsics(self):
        out = self.out
        self.assertEqual(out["model"], "fisheye", self.info["models"])
        self.assertEqual(tuple(self.info["board"]), (9, 6))
        self.assertLess(out["rms_px"], 0.5)
        K = np.array(out["K"])
        self.assertAlmostEqual(K[0, 0] / K_TRUE[0, 0], 1.0, delta=0.02)
        self.assertAlmostEqual(K[1, 1] / K_TRUE[1, 1], 1.0, delta=0.02)
        self.assertLess(abs(K[0, 2] - K_TRUE[0, 2]), 4)
        self.assertLess(abs(K[1, 2] - K_TRUE[1, 2]), 4)

    def test_projection_matches_truth_across_view(self):
        # 화면 곳곳의 점을 진짜 렌즈로 펴고, 찾은 렌즈로 다시 휘었을 때 평균 1px, 최대 2.5px 안
        # (640x360 기준. 주행 해상도 320x240에서는 그 절반 정도)
        # (맨 귀퉁이는 보드가 덜 닿아 조금 더 틀린다)
        xs, ys = np.meshgrid(np.linspace(40, SIZE[0] - 40, 12), np.linspace(30, SIZE[1] - 30, 7))
        pts = np.stack([xs, ys], -1).reshape(-1, 1, 2).astype(np.float64)
        n = cv2.fisheye.undistortPoints(pts, K_TRUE, D_TRUE)
        n3 = np.concatenate([n, np.ones((len(n), 1, 1))], axis=2).reshape(1, -1, 3)
        back = cv2.fisheye.projectPoints(n3, np.zeros(3), np.zeros(3), np.array(self.out["K"]),
                                         np.array(self.out["D"]))[0].reshape(-1, 2)
        err = np.linalg.norm(back - pts.reshape(-1, 2), axis=1)
        self.assertLess(err.mean(), 1.0, err.mean())
        self.assertLess(err.max(), 2.5, err.max())

    def test_runtime_scaling(self):
        # 주행 해상도(320x240)는 가로 1/2, 세로 2/3로 따로 줄인 것: 줄인 K로 편 점 = 원본 K로 편 점
        cal_full = CameraCalib(self.out, SIZE)
        cal_run = CameraCalib(self.out)          # runtime_size 320x240
        self.assertEqual(cal_run.size, (320, 240))
        p_full = np.array([[100.0, 80.0], [600.0, 300.0], [322.0, 178.0]])
        sx, sy = 320.0 / SIZE[0], 240.0 / SIZE[1]
        p_run = np.c_[(p_full[:, 0] + 0.5) * sx - 0.5, (p_full[:, 1] + 0.5) * sy - 0.5]
        np.testing.assert_allclose(cal_full.undistort_points(p_full), cal_run.undistort_points(p_run), atol=1e-6)
        K = scale_K(np.array(self.out["K"]), SIZE, (320, 240))
        np.testing.assert_allclose(K, np.array(self.out["K_runtime"]), atol=1e-3)

    def test_check_image(self):
        path = os.path.join(self.tmp, "calib_check.png")
        calib.check_image(self.out, self.info, path)
        self.assertTrue(os.path.getsize(path) > 1000)


if __name__ == "__main__":
    unittest.main()
