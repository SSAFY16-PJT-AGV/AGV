# -*- coding: utf-8 -*-
"""현장 선반 시험 도구: 마커 인쇄 파일, shelfcheck(사진으로), 배차기 끝 동작 옵션."""
import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from agv.perception import aruco, scene  # noqa: E402
from agv.perception.shelf import ShelfLayout, camera_from_config, load_config  # noqa: E402


class TestFieldTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_marker_sheets(self):
        from agv.tools import markers
        self.assertEqual(markers.main(["--out", self.tmp]), 0)
        found = []
        for f in sorted(glob.glob(os.path.join(self.tmp, "*.png"))):
            img = cv2.imread(f, 0)
            small = cv2.resize(img, None, fx=0.35, fy=0.35, interpolation=cv2.INTER_AREA)
            found += [m.id for m in aruco.detect(small)]
        self.assertEqual(sorted(set(found)), list(range(1, 25)) + [30, 31, 32, 33])
        # 마커 한 변이 300dpi에서 25mm(약 295px)
        img = cv2.imread(os.path.join(self.tmp, "shelves_1.png"), 0)
        ms = aruco.detect(img)
        side = np.mean([np.linalg.norm(m.corners[0] - m.corners[1]) for m in ms])
        self.assertAlmostEqual(side, 25.0 / 25.4 * 300, delta=4)

    def test_shelfcheck_on_photos(self):
        from agv.tools import shelfcheck
        cfg = load_config()
        L = ShelfLayout(cfg)
        cam = camera_from_config(cfg)
        rng = np.random.RandomState(1)
        for i in range(3):
            img, _ = scene.render_case(cfg, cam, L, (172, -6, 3), {1: {"marker_id": 30}, 3: {"marker_id": 33}},
                                       cfg["observe_pose"]["servo_angles"], rng, shelf_id="N02")
            cv2.imwrite(os.path.join(self.tmp, "s_%d.png" % i), cv2.resize(img, (320, 240), interpolation=cv2.INTER_AREA))
        out = os.path.join(self.tmp, "out")
        rc = shelfcheck.main(["--shelf", "N02", "--camera", "image:" + os.path.join(self.tmp, "s_*.png"),
                              "--no-arm", "--frames", "3", "--out", out])
        self.assertEqual(rc, 0)
        with open(os.path.join(out, "report.json")) as f:
            rep = json.load(f)
        m = rep["tilts"][0]["merged"]
        self.assertTrue(m["seen"])
        self.assertEqual(m["slots"], {"1": "milk", "2": "empty", "3": "juice", "4": "empty"})
        self.assertLess(abs(m["pose"]["x_mm"] - 172), 5)
        # 다른 선반이라고 하면 못 찾음
        rc = shelfcheck.main(["--shelf", "N05", "--camera", "image:" + os.path.join(self.tmp, "s_*.png"),
                              "--no-arm", "--frames", "2", "--out", os.path.join(self.tmp, "out2")])
        self.assertEqual(rc, 1)

    def test_dispatcher_jobs(self):
        sys.path.insert(0, os.path.join(ROOT, "sim"))
        sys.path.insert(0, os.path.join(ROOT, "sim", "mat"))
        import link_demo
        a = argparse.Namespace(dispatch="N01,D01", releases=None, action='{"type":"observe","shelf":"N01"}',
                               then="D01,N01,J02;J02,J07,N03,N02,N07,N01,D01",
                               then_action='{"type":"load","shelf":"N01","slot":1}')
        jobs = link_demo._jobs(a)
        self.assertEqual([j[0][-1] for j in jobs], ["D01", "J02", "D01"])
        self.assertEqual([j[2] and j[2]["type"] for j in jobs], ["observe", None, "load"])
        with self.assertRaises(ValueError):
            link_demo._action('["load"]')


if __name__ == "__main__":
    unittest.main()
