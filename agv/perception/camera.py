# -*- coding: utf-8 -*-
"""카메라 모델(렌즈)과 카메라 -> 로봇 몸체 변환.

- 렌즈: OpenCV 어안(fisheye) 또는 핀홀 모델. 렌즈 보정값(config/robots/<ID>/camera_calib.json)이
  있으면 그것을, 없으면 agv/config/perception.json의 camera_model(사양으로 근사한 값)을 쓴다.
- 몸체 좌표: 원점 차체 회전 중심 바닥, x 앞, y 왼쪽, z 위(mm). 기존 AGV 코드와 같다.
- 카메라는 팔 아래 회전 받침에 달려 있어 1번(받침 좌우, pan)과 5번(카메라 상하, tilt) 서보만
  카메라 자세를 바꾼다. 5번 각도는 아래로 보는 각(도)과 같다고 본다(+ 아래, - 위,
  Waveshare 예제의 cameraDown=+25, cameraUp=-70과 같은 방향). 차이는 robot_geom.tilt_offset_deg로 맞춘다.

Python 3.6 / OpenCV 4.1 호환.
"""
from __future__ import division

import math

import cv2
import numpy as np


class CameraModel(object):
    def __init__(self, model, K, D, size):
        self.model = model
        self.K = np.asarray(K, np.float64).reshape(3, 3)
        self.D = np.asarray(D, np.float64).reshape(-1)
        self.size = (int(size[0]), int(size[1]))

    @classmethod
    def from_config(cls, cm):
        K = [[cm["fx"], 0, cm["cx"]], [0, cm["fy"], cm["cy"]], [0, 0, 1]]
        return cls(cm.get("model", "fisheye"), K, cm.get("D", [0, 0, 0, 0]), cm["size"])

    @classmethod
    def from_calib(cls, calib_dict, size):
        """camcalib가 저장한 보정값(camera_calib.json)을 원하는 해상도로 바꿔 만든다."""
        from ..camcalib import scale_K
        K = scale_K(np.array(calib_dict["K"], float), tuple(calib_dict["capture_size"]), tuple(size))
        return cls(calib_dict["model"], K, calib_dict["D"], size)

    def undistort(self, pts):
        """화면 점(N,2) -> 왜곡을 편 정규 좌표(N,2) (x/z, y/z)."""
        p = np.asarray(pts, np.float64).reshape(-1, 1, 2)
        if self.model == "fisheye":
            out = cv2.fisheye.undistortPoints(p, self.K, self.D.reshape(4, 1))
        else:
            out = cv2.undistortPoints(p, self.K, self.D)
        return out.reshape(-1, 2)

    def project(self, Xc):
        """카메라 좌표 점(N,3) -> 화면 점(N,2). 카메라 뒤쪽 점은 nan."""
        X = np.asarray(Xc, np.float64).reshape(-1, 3)
        z = X[:, 2]
        out = np.full((len(X), 2), np.nan)
        ok = z > 1e-6
        a = X[ok, 0] / z[ok]
        b = X[ok, 1] / z[ok]
        if self.model == "fisheye":
            r = np.sqrt(a * a + b * b)
            th = np.arctan(r)
            k = self.D
            thd = th * (1 + k[0] * th ** 2 + k[1] * th ** 4 + k[2] * th ** 6 + k[3] * th ** 8)
            s = np.where(r > 1e-12, thd / np.maximum(r, 1e-12), 1.0)
            xd, yd = a * s, b * s
        else:
            pts, _ = cv2.projectPoints(np.c_[a, b, np.ones_like(a)], np.zeros(3), np.zeros(3), np.eye(3), self.D)
            xd, yd = pts[:, 0, 0], pts[:, 0, 1]
        out[ok, 0] = self.K[0, 0] * xd + self.K[0, 2]
        out[ok, 1] = self.K[1, 1] * yd + self.K[1, 2]
        return out

    def pixel_rays(self):
        """화면 모든 픽셀의 정규 좌표 (h, w, 2). 장면 렌더용."""
        w, h = self.size
        xs, ys = np.meshgrid(np.arange(w, dtype=np.float64), np.arange(h, dtype=np.float64))
        n = self.undistort(np.stack([xs, ys], -1).reshape(-1, 2))
        return n.reshape(h, w, 2)


def rot_z(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], np.float64)


def camera_to_body(geom, servo_angles):
    """카메라 좌표 -> 몸체 좌표 변환 (R, t). X_body = R . X_cam + t.

    geom: perception.json의 robot_geom. servo_angles: {"1": pan, "5": tilt} (도).
    5번 + 는 아래로 봄. 1번 + 는 왼쪽(반시계)으로 돎.
    """
    pan = float(servo_angles.get("1", 0.0) or 0.0)
    tilt = float(servo_angles.get("5", 0.0) or 0.0) + float(geom.get("tilt_offset_deg", 0.0))
    p = math.radians(tilt)
    f = np.array([math.cos(p), 0.0, -math.sin(p)])      # 카메라 z(앞)
    r = np.array([0.0, -1.0, 0.0])                     # 카메라 x(오른쪽)
    d = np.cross(f, r)                                  # 카메라 y(아래)
    Rz = rot_z(pan)
    R = Rz.dot(np.column_stack([r, d, f]))
    s1 = np.array([geom["s1_axis_x_mm"], 0.0, 0.0])
    t = s1 + Rz.dot(np.array([geom["camera_x_from_s1_mm"], 0.0, 0.0]))
    t[2] = geom["camera_z_mm"]
    return R, t
