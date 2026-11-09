# -*- coding: utf-8 -*-
"""테스트용: 알려진 렌즈(어안/핀홀)로 체커보드 사진을 만들어 낸다."""
import math
import os

import cv2
import numpy as np


def _rot(ax, ay, az):
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz.dot(Ry).dot(Rx)


def render(model, K, D, size, R, t, board=(9, 6), sq=25.0, rnd=None):
    w, h = size
    cols, rows = board
    xs, ys = np.meshgrid(np.arange(w), np.arange(h))
    pts = np.stack([xs, ys], -1).reshape(-1, 1, 2).astype(np.float64)
    if model == "fisheye":
        n = cv2.fisheye.undistortPoints(pts, K, D).reshape(-1, 2)
    else:
        n = cv2.undistortPoints(pts, K, D).reshape(-1, 2)
    rays = np.c_[n, np.ones(len(n))]
    nc = R[:, 2]
    denom = rays.dot(nc)
    s = t.dot(nc) / np.where(np.abs(denom) < 1e-9, 1e-9, denom)
    P = rays * s[:, None]
    Xb = (P - t).dot(R)
    u, v = Xb[:, 0] / sq, Xb[:, 1] / sq
    on = (u > -1) & (u < cols) & (v > -1) & (v < rows) & (s > 0)
    paper = (u > -2) & (u < cols + 1) & (v > -2) & (v < rows + 1) & (s > 0)
    chk = ((np.floor(u) + np.floor(v)) % 2 == 0)
    img = np.full(len(n), 120.0)
    img[paper] = 235.0
    img[on & chk] = 25.0
    img = img.reshape(h, w).astype(np.float32)
    img = cv2.GaussianBlur(img, (0, 0), 0.8)
    if rnd is not None:
        img += rnd.normal(0, 2.0, img.shape)
    return cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


def make_set(folder, model, K, D, size, n=20, board=(9, 6), sq=25.0, seed=0):
    """화면 곳곳(귀퉁이 포함)에 기울인 보드 사진 n장을 folder/full_XXX.png로 만든다."""
    rnd = np.random.RandomState(seed)
    os.makedirs(folder, exist_ok=True)
    cols, rows = board
    obj = np.zeros((rows * cols, 3))
    obj[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * sq
    center_b = np.array([(cols - 1) / 2.0 * sq, (rows - 1) / 2.0 * sq, 0])
    w, h = size
    k = 0
    tries = 0
    while k < n and tries < 2000:
        tries += 1
        # 목표 화면 위치(귀퉁이까지 고르게) -> 그 방향 광선 위 거리 d에 보드 가운데
        tx, ty = rnd.uniform(0.12, 0.88) * w, rnd.uniform(0.15, 0.85) * h
        if model == "fisheye":
            ray = cv2.fisheye.undistortPoints(np.array([[[tx, ty]]], np.float64), K, D).reshape(2)
        else:
            ray = cv2.undistortPoints(np.array([[[tx, ty]]], np.float64), K, D).reshape(2)
        d = rnd.uniform(260, 420)
        c = np.array([ray[0], ray[1], 1.0])
        c = c / np.linalg.norm(c) * d
        R = _rot(rnd.uniform(-0.6, 0.6), rnd.uniform(-0.6, 0.6), rnd.uniform(-0.4, 0.4))
        t = c - R.dot(center_b)
        Pc = obj.dot(R.T) + t
        if (Pc[:, 2] < 50).any():
            continue
        if model == "fisheye":
            pr = cv2.fisheye.projectPoints(obj.reshape(-1, 1, 3), cv2.Rodrigues(R)[0], t, K, D)[0].reshape(-1, 2)
        else:
            pr = cv2.projectPoints(obj, cv2.Rodrigues(R)[0], t, K, D)[0].reshape(-1, 2)
        m = 12
        if (pr[:, 0] < m).any() or (pr[:, 0] > w - m).any() or (pr[:, 1] < m).any() or (pr[:, 1] > h - m).any():
            continue
        cv2.imwrite(os.path.join(folder, "full_%03d.png" % k), render(model, K, D, size, R, t, board, sq, rnd))
        k += 1
    return k
