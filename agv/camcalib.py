# -*- coding: utf-8 -*-
"""카메라 렌즈 보정(내부 파라미터·왜곡) 계산과 불러오기.

체커보드 사진(원본 해상도, `camtool snap --count`)으로 일반(핀홀) 모델과 어안 모델을 둘 다 계산해
재투영 오차가 작은 쪽을 쓴다. JETANK 카메라처럼 화각이 넓으면 보통 어안 모델이 맞는다.

- 보정값은 찍은 해상도(capture_width x capture_height) 기준으로 계산하고, 주행 해상도(width x height)로
  줄인 값도 같이 저장한다. 주행 화면은 같은 센서 모드에서 가로·세로를 따로 줄인 것이라
  fx·cx는 가로 비율, fy·cy는 세로 비율로 바꾸면 된다. 왜곡 계수는 그대로다.
- 보정값은 그 센서 모드(찍은 해상도)에서만 맞는다. 카메라 설정의 capture 해상도를 바꾸거나
  jetbot 카메라(다른 센서 모드)를 쓰면 다시 찍어야 한다. 초점 링을 돌려도 다시 찍는다.
- 체커보드 칸 크기는 렌즈 보정값에 영향이 없다(사진마다 보드 위치를 같이 풀기 때문).

설계 문서 5.2절은 ChArUco 보드를 쓰지만, Jetson 기본 OpenCV(4.1.1)에는 aruco 모듈이 없을 수 있어
일반 체커보드를 쓴다. 렌즈 보정값을 구하는 데는 결과가 같다.

Python 3.6 / OpenCV 4.1 호환.
"""
from __future__ import print_function

import io
import json

import cv2
import numpy as np

BOARD_CANDIDATES = [(9, 6), (8, 6), (7, 6), (8, 5), (7, 5), (6, 5), (6, 4), (10, 7), (11, 8)]


def board_points(board, square=1.0):
    cols, rows = board
    obj = np.zeros((rows * cols, 3), np.float32)
    obj[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square
    return obj


def find_corners(gray, board, fast=False):
    """체커보드 안쪽 모서리를 찾는다. 반환: (N,1,2) float32 또는 None."""
    sb = getattr(cv2, "findChessboardCornersSB", None)
    if sb is not None and not fast:
        try:
            ok, c = sb(gray, board, cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_EXHAUSTIVE
                       | cv2.CALIB_CB_ACCURACY)
            if ok:
                return c.reshape(-1, 1, 2).astype(np.float32)
        except cv2.error:
            pass
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    if fast:
        flags |= cv2.CALIB_CB_FAST_CHECK
    ok, c = cv2.findChessboardCorners(gray, board, flags)
    if not ok:
        return None
    if not fast:
        c = cv2.cornerSubPix(gray, c, (5, 5), (-1, -1),
                             (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 0.01))
    return c.astype(np.float32)


def guess_board(grays, tries=6):
    """흔한 체커보드 크기 중 가장 많은 사진에서 찾히는 것."""
    best, best_n = None, 0
    for b in BOARD_CANDIDATES:
        n = 0
        for g in grays[:tries]:
            small = cv2.resize(g, (g.shape[1] // 2, g.shape[0] // 2))
            if find_corners(small, b, fast=True) is not None:
                n += 1
        if n > best_n:
            best, best_n = b, n
    return best


def coverage(corners, size, grid=(6, 4)):
    """모서리들이 화면을 얼마나 덮는지(격자 칸 중 점이 하나라도 있는 비율)와 비어 있는 귀퉁이."""
    w, h = size
    gx, gy = grid
    hit = np.zeros((gy, gx), bool)
    for c in corners:
        p = c.reshape(-1, 2)
        ix = np.clip((p[:, 0] / w * gx).astype(int), 0, gx - 1)
        iy = np.clip((p[:, 1] / h * gy).astype(int), 0, gy - 1)
        hit[iy, ix] = True
    names = {(0, 0): u"왼쪽 위", (0, gx - 1): u"오른쪽 위", (gy - 1, 0): u"왼쪽 아래", (gy - 1, gx - 1): u"오른쪽 아래"}
    empty = [n for (r, c), n in names.items() if not hit[r, c]]
    return float(hit.mean()), empty


def _per_view_err(obj, imgs, project):
    errs = []
    for o, im, pr in zip(obj, imgs, project):
        errs.append(float(np.sqrt(np.mean(np.sum((pr.reshape(-1, 2) - im.reshape(-1, 2)) ** 2, axis=1)))))
    return errs


def calib_pinhole(obj, imgs, size):
    rms, K, D, rv, tv = cv2.calibrateCamera(obj, imgs, size, None, None)
    proj = [cv2.projectPoints(o, r, t, K, D)[0] for o, r, t in zip(obj, rv, tv)]
    return {"model": "pinhole", "rms": float(rms), "K": K, "D": D.reshape(-1),
            "per_view": _per_view_err(obj, imgs, proj)}


def calib_fisheye(obj, imgs, size):
    """cv2.fisheye.calibrate. 조건이 나쁜 사진이 있으면 오류가 나므로 하나씩 빼며 다시 시도."""
    # OpenCV 4.x는 cv2.fisheye.CALIB_*, 5.x는 이름이 바뀜. 값은 같다(2, 4, 8)
    fe = lambda name, val: getattr(cv2.fisheye, name, getattr(cv2, name, val))
    flags = fe("CALIB_RECOMPUTE_EXTRINSIC", 2) | fe("CALIB_CHECK_COND", 4) | fe("CALIB_FIX_SKEW", 8)
    # fisheye.calibrate는 (1, N, 3) / (1, N, 2) 모양을 요구한다(다른 모양이면 OpenCV 버전에 따라 오류)
    obj_f = [o.reshape(1, -1, 3).astype(np.float64) for o in obj]
    img_f = [i.reshape(1, -1, 2).astype(np.float64) for i in imgs]
    keep = list(range(len(obj_f)))
    crit = (cv2.TERM_CRITERIA_COUNT + cv2.TERM_CRITERIA_EPS, 100, 1e-7)
    while len(keep) >= 6:
        try:
            K = np.eye(3)
            D = np.zeros((4, 1))
            rms, K, D, rv, tv = cv2.fisheye.calibrate([obj_f[i] for i in keep], [img_f[i] for i in keep],
                                                      size, K, D, flags=flags, criteria=crit)
            break
        except cv2.error as e:
            # 오류 메시지의 "input array N"으로 문제 사진을 찾을 수 있으면 그걸, 아니면 마지막 사진을 뺀다
            msg = str(e)
            bad = None
            if "CALIB_CHECK_COND" in msg or "input array" in msg:
                import re
                m = re.search(r"input array (\d+)", msg)
                if m:
                    bad = int(m.group(1))
            keep.pop(bad if bad is not None and bad < len(keep) else -1)
    else:
        return None
    proj = [cv2.fisheye.projectPoints(obj_f[i], r, t, K, D)[0] for i, r, t in zip(keep, rv, tv)]
    per = _per_view_err([obj_f[i] for i in keep], [img_f[i] for i in keep], proj)
    return {"model": "fisheye", "rms": float(rms), "K": K, "D": D.reshape(-1), "per_view": per,
            "used": keep}


def scale_K(K, from_size, to_size):
    sx = float(to_size[0]) / from_size[0]
    sy = float(to_size[1]) / from_size[1]
    K2 = np.array(K, float).copy()
    K2[0, 0] *= sx
    K2[0, 2] = (K2[0, 2] + 0.5) * sx - 0.5
    K2[1, 1] *= sy
    K2[1, 2] = (K2[1, 2] + 0.5) * sy - 0.5
    return K2


def fov_deg(model, K, D, size):
    """가로·세로 화각(도). 화면 가장자리 가운데 점을 펴서 각도를 잰다."""
    w, h = size
    pts = np.array([[[0, h / 2.0]], [[w - 1, h / 2.0]], [[w / 2.0, 0]], [[w / 2.0, h - 1]]], np.float64)
    if model == "fisheye":
        n = cv2.fisheye.undistortPoints(pts, K, D).reshape(-1, 2)
    else:
        n = cv2.undistortPoints(pts, K, D).reshape(-1, 2)
    ang = np.degrees(np.arctan(np.abs(n)))
    return float(ang[0, 0] + ang[1, 0]), float(ang[2, 1] + ang[3, 1])


class CameraCalib(object):
    """저장된 보정값. 주행 해상도 기준 K·D와 점/영상 펴기."""

    def __init__(self, d, size=None):
        self.d = d
        self.model = d["model"]
        self.capture_size = tuple(d["capture_size"])
        self.size = tuple(size or d["runtime_size"])
        self.K = scale_K(np.array(d["K"], float), self.capture_size, self.size)
        self.D = np.array(d["D"], float)

    @classmethod
    def load(cls, path, size=None):
        with io.open(path, encoding="utf-8") as f:
            return cls(json.load(f), size)

    def undistort_points(self, pts, pixel=False):
        """화면 점(N,2) -> 왜곡을 편 정규 좌표(N,2). pixel=True면 같은 K의 핀홀 픽셀 좌표."""
        p = np.asarray(pts, np.float64).reshape(-1, 1, 2)
        P = self.K if pixel else None
        if self.model == "fisheye":
            out = cv2.fisheye.undistortPoints(p, self.K, self.D, P=P)
        else:
            out = cv2.undistortPoints(p, self.K, self.D, P=P)
        return out.reshape(-1, 2)

    def undistort_image(self, img, balance=0.0):
        """보기용으로 편 영상. balance 0이면 검은 테두리 없이 자름, 1이면 전부 보임(어안)."""
        h, w = img.shape[:2]
        K = scale_K(np.array(self.d["K"], float), self.capture_size, (w, h))
        if self.model == "fisheye":
            newK = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(K, self.D, (w, h), np.eye(3),
                                                                          balance=balance)
            m1, m2 = cv2.fisheye.initUndistortRectifyMap(K, self.D, np.eye(3), newK, (w, h), cv2.CV_16SC2)
        else:
            newK, _ = cv2.getOptimalNewCameraMatrix(K, self.D, (w, h), balance)
            m1, m2 = cv2.initUndistortRectifyMap(K, self.D, None, newK, (w, h), cv2.CV_16SC2)
        return cv2.remap(img, m1, m2, cv2.INTER_LINEAR)
