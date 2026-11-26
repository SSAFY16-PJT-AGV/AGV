# -*- coding: utf-8 -*-
"""선반 장면 시뮬레이터: 가상 카메라로 선반·박스·마커를 그려 perceive_shelf를 시험한다.

- 몸체 좌표(mm, x 앞, y 왼쪽, z 위)에서 광선 추적으로 그린다. 렌즈는 CameraModel(어안/핀홀) 그대로.
- 물체는 회전한 직육면체(선반 앞판, 박스)와 바닥·뒷벽. 마커는 면 위의 텍스처다.
- 정답(박스 가운데, 칸 상태, 선반 위치)을 같이 돌려줘서 판정 결과와 비교할 수 있다.

실제 조명·인쇄·렌즈와는 다르므로, 여기서 통과해도 실물 사진 검증은 따로 한다.
Python 3.6 / OpenCV 4.1 호환(numpy·cv2만 사용).
"""
from __future__ import division

import math

import cv2
import numpy as np

from . import aruco
from .camera import camera_to_body, rot_z
from .shelf import ShelfLayout


def marker_cells(mid, dictionary=aruco.DICT):
    """마커 6x6 칸(흰색=1): 테두리 검은색 + 안쪽 비트."""
    n = dictionary.n
    cells = np.zeros((n + 2, n + 2), np.uint8)
    cells[1:-1, 1:-1] = aruco._code_to_bits(dictionary.codes[mid], n)
    return cells


class Box(object):
    """회전한 직육면체. center(3), yaw(도), size(dx, dy, dz), gray(0~255).
    front_marks: 앞면(-x 쪽, 로봇을 보는 면)에 붙인 마커 [(id, u_center, v_center, size)].
    u는 앞면을 볼 때 오른쪽(-y 로컬), v는 위(+z 로컬), 면 가운데 기준 mm."""

    def __init__(self, center, yaw_deg, size, gray, front_marks=None, tag=None):
        self.c = np.asarray(center, np.float64)
        self.R = rot_z(yaw_deg)
        self.h = np.asarray(size, np.float64) / 2.0
        self.gray = float(gray)
        self.marks = front_marks or []
        self.tag = tag
        self._cells = dict((m[0], marker_cells(m[0])) for m in self.marks)


def _shade_face(axis, sign):
    # 면마다 약간 다른 밝기(빛이 위 앞쪽에서 온다고 봄)
    if axis == 2:
        return 1.08 if sign > 0 else 0.6
    if axis == 0:
        return 1.0 if sign < 0 else 0.75
    return 0.88


def render(cam, R_bc, t_bc, boxes, floor_gray=165.0, wall_x=None, wall_gray=150.0, rays=None):
    """회색조 영상(float, 0~255). rays: cam.pixel_rays() 미리 계산한 것(여러 장 그릴 때)."""
    if rays is None:
        rays = cam.pixel_rays()
    h, w = rays.shape[:2]
    d_cam = np.concatenate([rays, np.ones((h, w, 1))], axis=2).reshape(-1, 3)
    d = d_cam.dot(R_bc.T)
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    o = np.asarray(t_bc, np.float64)
    N = len(d)
    best_t = np.full(N, np.inf)
    img = np.full(N, 120.0)
    # 바닥
    with np.errstate(divide="ignore", invalid="ignore"):
        tf = np.where(d[:, 2] < -1e-9, -o[2] / d[:, 2], np.inf)
    hit = tf < best_t
    best_t[hit] = tf[hit]
    px = o[0] + d[hit, 0] * tf[hit]
    img[hit] = floor_gray * (0.95 + 0.05 * np.cos(px / 37.0))
    # 뒷벽
    if wall_x is not None:
        with np.errstate(divide="ignore", invalid="ignore"):
            tw = np.where(d[:, 0] > 1e-9, (wall_x - o[0]) / d[:, 0], np.inf)
        hit = tw < best_t
        best_t[hit] = tw[hit]
        img[hit] = wall_gray
    for b in boxes:
        ol = b.R.T.dot(o - b.c)
        dl = d.dot(b.R)
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / dl
            t1 = (-b.h - ol) * inv
            t2 = (b.h - ol) * inv
        tmin3 = np.minimum(t1, t2)
        tmax3 = np.maximum(t1, t2)
        tmin3 = np.where(np.isnan(tmin3), -np.inf, tmin3)      # numpy 1.17 전 호환(nan_to_num의 nan= 없음)
        tmax3 = np.where(np.isnan(tmax3), np.inf, tmax3)
        tn = tmin3.max(axis=1)
        tx = tmax3.min(axis=1)
        hit = (tx >= tn) & (tn > 0) & (tn < best_t)
        if not hit.any():
            continue
        idx = np.nonzero(hit)[0]
        ax = tmin3[idx].argmax(axis=1)
        sign = -np.sign(dl[idx, ax])
        best_t[idx] = tn[idx]
        P = ol + dl[idx] * tn[idx][:, None]
        val = np.full(len(idx), b.gray)
        for a in (0, 1, 2):
            for sg in (-1, 1):
                sel = (ax == a) & (sign == sg)
                val[sel] *= _shade_face(a, sg)
        # 앞면(-x) 마커
        front = (ax == 0) & (sign < 0)
        if b.marks and front.any():
            fi = np.nonzero(front)[0]
            u = -P[fi, 1]
            v = P[fi, 2]
            fv = val[fi]
            for mid, uc, vc, size in b.marks:
                cell = size / 6.0
                du, dv = u - uc, v - vc
                quiet = (np.abs(du) <= size / 2.0 + cell) & (np.abs(dv) <= size / 2.0 + cell)
                fv[quiet] = 235.0
                col = np.floor((du + size / 2.0) / cell).astype(int)
                row = np.floor((size / 2.0 - dv) / cell).astype(int)
                ins = (col >= 0) & (col < 6) & (row >= 0) & (row < 6)
                cells = b._cells[mid]
                fv[ins] = np.where(cells[row[ins], col[ins]] > 0, 235.0, 22.0)
            val[fi] = fv
        img[idx] = val
    return img.reshape(h, w)


def finish(img, rng, gain=1.0, noise=3.0, blur=0.7, gradient=0.0):
    """조명 세기·밝기 기울기·흐림·잡음을 넣어 uint8로."""
    h, w = img.shape
    out = img * gain
    if gradient:
        out = out * (1.0 + gradient * np.linspace(-1, 1, w)[None, :])
    if blur > 0:
        out = cv2.GaussianBlur(out, (0, 0), blur)
    if noise > 0:
        out = out + rng.normal(0, noise, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def build_shelf_scene(cfg, layout, shelf_pose, contents, shelf_id_ids=None, apron_mm=35.0, shelf_id=None):
    """선반 + 박스 장면.

    shelf_pose: (x, y, yaw_deg) 선반 원점(앞 모서리 가운데, 윗면 높이)의 몸체 좌표.
    contents: {slot: {"type", "marker_id", "setback", "dy", "yaw"}}  없는 칸은 빈 칸.
    shelf_id: 어느 선반의 칸 마커를 붙일지(없으면 첫 선반). shelf_id_ids로 마커 id를 직접 줄 수도 있다.
    반환: (boxes, truth). truth: {"slots": {k: {...}}, "shelf": {...}}
    """
    sx, sy, syaw = shelf_pose
    Rs = rot_z(syaw)
    org = np.array([sx, sy, layout.height])
    ids = shelf_id_ids or layout.shelves[shelf_id or sorted(layout.shelves)[0]]
    width = layout.n * layout.slot_w + 40.0
    # 선반 윗판 + 앞판(두께 apron_mm). 앞면에 칸 마커
    marks = []
    for k in range(1, layout.n + 1):
        u = -layout.slot_y(k)
        v = apron_mm / 2.0 - layout.marker_below
        marks.append((ids[k - 1], u, v, layout.marker_size))
    slab_c = org + Rs.dot([layout.depth / 2.0, 0, -apron_mm / 2.0])
    boxes = [Box(slab_c, syaw, (layout.depth, width, apron_mm), 225.0, marks, tag="shelf")]
    # 다리 4개(시야 참고용)
    for lx in (8.0, layout.depth - 8.0):
        for ly in (width / 2.0 - 8.0, -width / 2.0 + 8.0):
            lc = org + Rs.dot([lx, ly, -(layout.height - apron_mm) / 2.0 - apron_mm])
            boxes.append(Box(lc, syaw, (12.0, 12.0, layout.height - apron_mm), 90.0, tag="leg"))
    truth = {"shelf": {"x": sx, "y": sy, "yaw": syaw}, "slots": {}}
    for k in range(1, layout.n + 1):
        c = contents.get(k)
        if not c:
            truth["slots"][k] = {"state": "empty"}
            continue
        mid = c["marker_id"]
        name, size, gz = layout.objects[mid]
        loc = np.array([c.get("setback", 0.0) + size[0] / 2.0, layout.slot_y(k) + c.get("dy", 0.0),
                        size[2] / 2.0])
        ctr = org + Rs.dot(loc)
        yaw = syaw + c.get("yaw", 0.0)
        boxes.append(Box(ctr, yaw, size, 150.0, [(mid, 0.0, 0.0, layout.marker_size)], tag="box%d" % k))
        truth["slots"][k] = {"state": name, "marker_id": mid,
                             "center_mm": [float(ctr[0]), float(ctr[1]), float(layout.height + gz)],
                             "yaw_deg": float(yaw)}
    return boxes, truth


def random_case(rng, cfg, layout, stop_xy_mm=20.0, stop_yaw_deg=5.0, p_empty=0.35):
    """정차 오차와 칸 내용을 무작위로 뽑는다."""
    shelf_pose = (layout.front_x + rng.uniform(-stop_xy_mm, stop_xy_mm),
                  rng.uniform(-stop_xy_mm, stop_xy_mm),
                  rng.uniform(-stop_yaw_deg, stop_yaw_deg))
    types = {}
    for mid, (name, size, gz) in layout.objects.items():
        types.setdefault(name, []).append(mid)
    names = sorted(types)
    contents = {}
    for k in range(1, layout.n + 1):
        if rng.uniform() < p_empty:
            continue
        name = names[rng.randint(len(names))]
        mid = types[name][rng.randint(len(types[name]))]
        size = layout.objects[mid][1]
        slack = max(0.0, (layout.slot_w - size[1]) / 2.0 - 3.0)
        contents[k] = {"marker_id": mid, "setback": rng.uniform(0, layout.max_setback),
                       "dy": rng.uniform(-slack, slack) * 0.7, "yaw": rng.uniform(-8, 8)}
    return shelf_pose, contents


def render_case(cfg, cam, layout, shelf_pose, contents, servo_angles, rng, rays=None, light=None, shelf_id=None):
    """한 장면을 그려 (영상 uint8, truth)를 돌려준다. light: finish()에 넘길 값."""
    R_bc, t_bc = camera_to_body(cfg["robot_geom"], servo_angles)
    boxes, truth = build_shelf_scene(cfg, layout, shelf_pose, contents, shelf_id=shelf_id)
    truth["shelf"]["id"] = shelf_id or sorted(layout.shelves)[0]
    raw = render(cam, R_bc, t_bc, boxes, wall_x=layout.front_x + layout.depth + 250.0, rays=rays)
    light = light or {}
    img = finish(raw, rng, **light)
    return img, truth


def compare(result, truth):
    """판정 결과와 정답 비교. 반환: 칸마다 (정답 상태, 판정 상태, 위치 오차 mm, yaw 오차 도)."""
    rows = []
    got = dict((s["slot"], s) for s in result["slots"])
    for k, t in sorted(truth["slots"].items()):
        g = got.get(k, {"state": "unknown"})
        err = yerr = None
        if t["state"] != "empty" and g["state"] == t["state"] and "center_mm" in g:
            err = math.hypot(g["center_mm"][0] - t["center_mm"][0], g["center_mm"][1] - t["center_mm"][1])
            yerr = abs((g["yaw_deg"] - t["yaw_deg"] + 180.0) % 360.0 - 180.0)
        rows.append((k, t["state"], g["state"], err, yerr))
    return rows
