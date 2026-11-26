# -*- coding: utf-8 -*-
"""선반 perception 테스트: 직접 구현한 ArUco 검출기, 마커 자세, 선반 칸 판정(가상 장면)."""
import math
import os
import sys
import unittest

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from agv.perception import aruco, scene  # noqa: E402
from agv.perception.camera import camera_to_body  # noqa: E402
from agv.perception.shelf import ShelfLayout, camera_from_config, load_config, perceive_shelf  # noqa: E402


def marker_image(mid, cell=20, margin=1):
    """마커 그림(흰 여백 margin칸 포함). cv2.aruco 없이 사전 코드로 만든다."""
    cells = scene.marker_cells(mid)
    img = np.kron(cells, np.ones((cell, cell))) * 255
    return np.pad(img, margin * cell, mode="constant", constant_values=255).astype(np.uint8), cell * margin


def warped_case(rng, size=(640, 480)):
    """무작위 원근으로 붙인 마커 한 장. 반환: (영상, id, 정답 꼭짓점(4,2))."""
    mid = rng.randint(50)
    m, off = marker_image(mid)
    S = m.shape[0]
    w, h = size
    img = np.full((h, w), rng.randint(60, 140), np.uint8)
    side = rng.uniform(30, 170)
    cx, cy = rng.uniform(side, w - side), rng.uniform(side, h - side)
    ang = rng.uniform(0, 2 * math.pi)
    base = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]]) * side / 2
    R = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]])
    dst = base.dot(R.T) + [cx, cy] + rng.uniform(-0.12, 0.12, (4, 2)) * side
    src = np.float32([[-0.5, -0.5], [S - 0.5, -0.5], [S - 0.5, S - 0.5], [-0.5, S - 0.5]])
    H = cv2.getPerspectiveTransform(src, np.float32(dst))
    img = cv2.warpPerspective(m, H, (w, h), dst=img, borderMode=cv2.BORDER_TRANSPARENT)
    img = cv2.GaussianBlur(img, (0, 0), 0.8)
    img = np.clip(img + rng.normal(0, 4, img.shape), 0, 255).astype(np.uint8)
    a, b = off - 0.5, S - off - 0.5
    gt = cv2.perspectiveTransform(np.float32([[[a, a], [b, a], [b, b], [a, b]]]), H).reshape(4, 2)
    return img, mid, gt


class TestDetector(unittest.TestCase):
    def test_random_warps(self):
        rng = np.random.RandomState(0)
        errs = []
        for _ in range(60):
            img, mid, gt = warped_case(rng)
            found = aruco.detect(img)
            self.assertEqual([m.id for m in found], [mid])
            errs.append(np.abs(found[0].corners - gt).max())
        self.assertLess(np.median(errs), 0.8)
        self.assertLess(max(errs), 2.5)

    def test_corner_order_follows_marker(self):
        """돌려 붙여도 corners[0]은 마커 기준 왼쪽 위(OpenCV aruco와 같은 순서)."""
        m, off = marker_image(7)
        S = m.shape[0]
        for k in range(4):
            r = np.ascontiguousarray(np.rot90(m, k))
            img = np.full((S + 80, S + 80), 100, np.uint8)
            img[40:40 + S, 40:40 + S] = r
            found = aruco.detect(img)
            self.assertEqual([f.id for f in found], [7])
            # 마커 왼쪽 위 꼭짓점이 돌린 뒤 어디로 갔는지(rot90은 반시계)
            a, b = 40 + off - 0.5, 40 + S - off - 0.5
            tl = [(a, a), (a, b), (b, b), (b, a)][k]
            self.assertLess(np.abs(found[0].corners[0] - tl).max(), 1.0)

    def test_no_false_positives(self):
        rng = np.random.RandomState(1)
        noise = rng.randint(0, 256, (360, 480)).astype(np.uint8)
        board = (np.indices((360, 480)).sum(0) // 40 % 2 * 200 + 30).astype(np.uint8)
        squares = np.full((360, 480), 200, np.uint8)
        cv2.rectangle(squares, (50, 50), (150, 150), 20, -1)          # 검은 사각형만
        cv2.rectangle(squares, (250, 60), (350, 160), 20, 12)         # 테두리만
        for img in (noise, board, squares):
            self.assertEqual(aruco.detect(img), [])

    @unittest.skipUnless(hasattr(cv2, "aruco") and hasattr(cv2.aruco, "ArucoDetector"),
                         "cv2.aruco(OpenCV 4.7+) 없음 - 로봇에서는 건너뜀")
    def test_same_as_cv2_aruco(self):
        d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        det = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters())
        rng = np.random.RandomState(2)
        for _ in range(40):
            img, mid, _ = warped_case(rng)
            _, ids, _ = det.detectMarkers(img)
            ref = sorted(int(i) for i in ids.flatten()) if ids is not None else []
            self.assertEqual(sorted(m.id for m in aruco.detect(img)), ref)
        # 사전 코드도 같은지
        for i in range(50):
            ref = cv2.aruco.generateImageMarker(d, i, 60)
            cells = (ref.reshape(6, 10, 6, 10).mean(axis=(1, 3)) > 127).astype(np.uint8)
            np.testing.assert_array_equal(cells, scene.marker_cells(i))


class TestShelf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls.layout = ShelfLayout(cls.cfg)
        cls.cam = camera_from_config(cls.cfg)
        cls.servo = cls.cfg["observe_pose"]["servo_angles"]
        cls.rays = cls.cam.pixel_rays()

    def run_case(self, pose, contents, seed=0, ids=None, shelf=None, expect=None):
        rng = np.random.RandomState(seed)
        R_bc, t_bc = camera_to_body(self.cfg["robot_geom"], self.servo)
        boxes, truth = scene.build_shelf_scene(self.cfg, self.layout, pose, contents, shelf_id_ids=ids,
                                               shelf_id=shelf)
        raw = scene.render(self.cam, R_bc, t_bc, boxes, wall_x=self.layout.front_x + 300, rays=self.rays)
        img = scene.finish(raw, rng, gain=0.9, noise=3.0, blur=0.7)
        return perceive_shelf(img, self.cfg, self.cam, self.servo, self.layout, expect_shelf=expect), truth

    def check(self, res, truth, pose, tol_mm=3.0):
        self.assertTrue(res["shelf"]["ok"])
        self.assertLess(math.hypot(res["shelf"]["x_mm"] - pose[0], res["shelf"]["y_mm"] - pose[1]), 2.0)
        self.assertLess(abs(res["shelf"]["yaw_deg"] - pose[2]), 0.6)
        for k, t, g, e, ye in scene.compare(res, truth):
            self.assertEqual(g, t, "칸 %d" % k)
            if e is not None:
                self.assertLess(e, tol_mm, "칸 %d 위치" % k)
                self.assertLess(ye, 2.0, "칸 %d 방향" % k)

    def test_fixed_case(self):
        pose = (self.layout.front_x + 12, -15.0, 4.0)
        contents = {1: {"marker_id": 30, "setback": 0, "dy": 5, "yaw": 6},
                    2: {"marker_id": 31, "setback": 14, "dy": -8, "yaw": -7},
                    4: {"marker_id": 32, "setback": 8, "dy": 0, "yaw": 0}}
        res, truth = self.run_case(pose, contents)
        self.check(res, truth, pose)
        self.assertEqual([s["state"] for s in res["slots"]], ["milk", "tissue", "empty", "egg"])
        self.assertEqual(res["slots"][0]["marker_id"], 30)

    def test_random_cases(self):
        rng = np.random.RandomState(7)
        for i in range(3):
            pose, contents = scene.random_case(rng, self.cfg, self.layout, p_empty=0.25)
            res, truth = self.run_case(pose, contents, seed=i)
            self.check(res, truth, pose)

    def test_without_ippe(self):
        """OpenCV 4.1처럼 solvePnPGeneric이 없을 때(ITERATIVE 한 해)도 맞게."""
        saved = getattr(cv2, "solvePnPGeneric", None)
        cv2.solvePnPGeneric = None
        try:
            pose = (self.layout.front_x - 10, 12.0, -3.0)
            contents = {2: {"marker_id": 33, "setback": 5, "dy": 3, "yaw": -4},
                        3: {"marker_id": 31, "setback": 10, "dy": -5, "yaw": 5}}
            res, truth = self.run_case(pose, contents)
            self.check(res, truth, pose)
        finally:
            cv2.solvePnPGeneric = saved

    def test_hidden_slot_marker_is_unknown(self):
        """빈 칸인데 칸 앞면 마커가 안 보이면 empty가 아니라 unknown."""
        pose = (self.layout.front_x, 0.0, 0.0)
        res, _ = self.run_case(pose, {1: {"marker_id": 30, "setback": 0}}, ids=[1, 2, 45, 4])   # 45: 안 쓰는 ID
        self.assertEqual([s["state"] for s in res["slots"]], ["milk", "empty", "unknown", "empty"])
        self.assertEqual(res["shelf"]["slot_markers"], [1, 2, 4])

    def test_six_shelves(self):
        """선반 6개(N01~N06)는 칸 마커가 서로 달라, 서버가 정한 선반 앞인지 확인된다."""
        self.assertEqual(sorted(self.layout.shelves), ["N0%d" % k for k in range(1, 7)])
        self.assertEqual(self.layout.shelves["N03"], [9, 10, 11, 12])
        pose = (self.layout.front_x + 5, 8.0, -2.0)
        contents = {2: {"marker_id": 31, "setback": 3}, 4: {"marker_id": 31, "setback": 9}}
        res, truth = self.run_case(pose, contents, shelf="N03", expect="N03")
        self.check(res, truth, pose)
        self.assertEqual(res["shelf"]["shelf_id"], "N03")
        self.assertEqual([s["state"] for s in res["slots"]], ["empty", "tissue", "empty", "tissue"])
        # 다른 선반 앞에 섰으면(서버는 N02라고 함) 칸을 판정하지 않음
        res, _ = self.run_case(pose, contents, shelf="N03", expect="N02")
        self.assertFalse(res["shelf"]["ok"])
        self.assertEqual(set(s["state"] for s in res["slots"]), {"unknown"})


if __name__ == "__main__":
    unittest.main()
