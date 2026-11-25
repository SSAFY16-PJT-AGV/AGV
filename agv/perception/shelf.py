# -*- coding: utf-8 -*-
"""선반 칸 판정: 마커 검출 결과 -> 칸마다 상태(종류/empty/unknown)와 로봇 몸체 좌표.

docs/perception/shelf-perception.md의 "처리 파이프라인" 6~10과 "좌표계와 팔 담당과의 인터페이스".

- 칸 앞면 마커(선반 앞면, 칸마다 하나)로 선반 위치·각도와 칸 위치를 구한다. ID로 선반·칸을 안다.
- 박스 앞면 마커로 박스 종류·위치·방향을 구하고, 선반 좌표로 바꿔 칸에 넣는다.
- 칸 앞면 마커가 보이고 그 칸에 박스 마커가 없으면 empty, 앞면 마커도 안 보이면 unknown.
  카메라가 선반 윗면보다 낮아 윗면(칸 바닥)은 보이지 않는다.

선반 좌표계: 원점은 선반 앞 모서리 가운데(윗면 높이), x 선반 안쪽, y 왼쪽(로봇에서 볼 때), z 위.
칸 번호는 로봇에서 볼 때 왼쪽(+y)부터 1, 2, ...

Python 3.6 / OpenCV 4.1 호환.
"""
from __future__ import division

import io
import json
import math
import os

import numpy as np

from . import aruco
from .camera import CameraModel, camera_to_body

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_PATH = os.path.join(ROOT, "agv", "config", "perception.json")


def _merge(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None, robot=None):
    """기본값(agv/config/perception.json)에 로봇 폴더의 perception.json을 덮어 읽는다."""
    with io.open(path or CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if robot:
        rp = os.path.join(ROOT, "agv", "config", "robots", robot, "perception.json")
        if os.path.exists(rp):
            with io.open(rp, encoding="utf-8") as f:
                cfg = _merge(cfg, json.load(f))
    return cfg


def camera_from_config(cfg, robot=None):
    """렌즈 보정값(config/robots/<ID>/camera_calib.json)이 있으면 그것, 없으면 camera_model."""
    cm = cfg["camera_model"]
    if robot:
        cp = os.path.join(ROOT, "agv", "config", "robots", robot, "camera_calib.json")
        if os.path.exists(cp):
            with io.open(cp, encoding="utf-8") as f:
                return CameraModel.from_calib(json.load(f), cm["size"])
    return CameraModel.from_config(cm)


class ShelfLayout(object):
    """config의 shelf·objects·markers를 판정에 쓰기 좋게 정리."""

    def __init__(self, cfg):
        sh = cfg["shelf"]
        self.height = float(sh["height_mm"])
        self.n = int(sh["slot_count"])
        self.slot_w = float(sh["slot_width_mm"])
        self.depth = float(sh["slot_depth_mm"])
        self.front_x = float(sh.get("front_x_mm", 170.0))
        self.marker_below = float(sh.get("slot_marker_z_below_top_mm", 15.0))
        self.max_setback = float(sh.get("max_box_setback_mm", 15.0))
        self.place_setback = float(sh.get("place_setback_mm", 5.0))
        shelves = sh.get("shelf_ids") or {"SHELF": sh["slot_marker_ids"]}
        self.slot_marker = {}            # marker id -> (shelf_id, slot 1..n)
        for sid, ids in shelves.items():
            for k, mid in enumerate(ids):
                self.slot_marker[int(mid)] = (sid, k + 1)
        self.marker_size = float(cfg["markers"]["size_mm"])
        self.objects = {}                # marker id -> (type, size_mm, grip_z)
        for name, o in cfg["objects"].items():
            if name.startswith("_"):
                continue
            for mid in o["ids"]:
                self.objects[int(mid)] = (name, [float(v) for v in o["size_mm"]], float(o.get("grip_z_mm", 0)))
        # 빈 칸에 놓을 때 박스 가운데 자리(앞 모서리에서 place_setback + 박스 깊이 절반)
        depths = [v[1][0] for v in self.objects.values()] or [40.0]
        self.place_x = self.place_setback + max(depths) / 2.0

    def slot_y(self, k):
        """칸 k(1부터)의 가운데 y(선반 좌표)."""
        return ((self.n - 1) / 2.0 - (k - 1)) * self.slot_w


def _pick_pose(cands, R_bc, t_bc, cam_pos):
    """마커 자세 후보 중 몸체 좌표에서 세로 면(법선이 수평)에 가장 가깝고 카메라를 보는 것."""
    best = None
    for R, t, err in cands:
        Rb = R_bc.dot(R)
        pb = R_bc.dot(t) + t_bc
        nb = Rb[:, 2]
        if nb.dot(cam_pos - pb) <= 0:
            continue
        score = abs(nb[2]) + 0.02 * err
        if best is None or score < best[0]:
            best = (score, Rb, pb, nb, err)
    return best


def _fit_shelf(obs, layout):
    """칸 앞면 마커 관측 [(slot, p(3), n(3))] -> 선반 원점(x, y)과 각도(라디안, 선반 안쪽 방향)."""
    if not obs:
        return None
    # 각 마커 법선(로봇 쪽)의 반대 = 선반 안쪽 방향
    angs = [math.atan2(-n[1], -n[0]) for _, _, n in obs]
    th_n = math.atan2(np.mean(np.sin(angs)), np.mean(np.cos(angs)))
    if len(obs) >= 2:
        # 칸 위치(선반 좌표 (0, y_k))와 관측 위치로 2D 강체 맞춤
        A = np.array([[0.0, layout.slot_y(k)] for k, _, _ in obs])
        B = np.array([[p[0], p[1]] for _, p, _ in obs])
        ca, cb = A.mean(0), B.mean(0)
        H = (A - ca).T.dot(B - cb)
        th = math.atan2(H[0, 1] - H[1, 0], H[0, 0] + H[1, 1])
        span = np.ptp(A[:, 1])
        if span < layout.slot_w * 0.5:
            th = th_n
        c, s = math.cos(th), math.sin(th)
        Rm = np.array([[c, -s], [s, c]])
        org = cb - Rm.dot(ca)
    else:
        th = th_n
        k, p, _ = obs[0]
        c, s = math.cos(th), math.sin(th)
        Rm = np.array([[c, -s], [s, c]])
        org = np.array([p[0], p[1]]) - Rm.dot([0.0, layout.slot_y(k)])
    z = float(np.mean([p[2] for _, p, _ in obs])) + layout.marker_below
    return org, th, z


def perceive_shelf(img, cfg, cam, servo_angles, layout=None, extrinsic=None, expect_shelf=None, markers=None):
    """한 장으로 선반 칸 상태를 판정한다. 반환 dict는 설계 문서 5절 출력 형식.

    servo_angles: 찍을 때의 서보 각도 {"1": pan, "5": tilt, ...}. extrinsic: (R, t) 카메라 -> 몸체를
    직접 줄 때(보정값). 없으면 robot_geom과 1·5번 각도로 계산한다.
    """
    layout = layout or ShelfLayout(cfg)
    if extrinsic is None:
        R_bc, t_bc = camera_to_body(cfg["robot_geom"], servo_angles)
    else:
        R_bc, t_bc = extrinsic
    if markers is None:
        markers = aruco.detect(img, params={"max_bit_err": cfg["markers"].get("max_bit_err", 1)})

    slot_obs = {}       # shelf_id -> [(slot, p, n)]
    boxes = []          # (marker, type, size, grip_z, p, n)
    seen = []
    for m in markers:
        if m.id in layout.slot_marker:
            kind = "slot"
        elif m.id in layout.objects:
            kind = "box"
        else:
            continue
        best = _pick_pose(aruco.estimate_pose(m, layout.marker_size, cam), R_bc, t_bc, t_bc)
        if best is None:
            continue
        _, Rb, pb, nb, err = best
        nh = np.array([nb[0], nb[1], 0.0])
        nh = nh / max(np.linalg.norm(nh), 1e-9)
        seen.append({"id": m.id, "kind": kind, "pos_mm": [round(float(v), 1) for v in pb],
                     "tilt_deg": round(math.degrees(math.asin(max(-1.0, min(1.0, nb[2])))), 1),
                     "reproj_px": round(err, 2), "side_px": round(m.side_px, 1)})
        if kind == "slot":
            sid, k = layout.slot_marker[m.id]
            slot_obs.setdefault(sid, []).append((k, pb, nh))
        else:
            name, size, gz = layout.objects[m.id]
            boxes.append((m, name, size, gz, pb, nh))

    out = {"frame": "robot_base", "arm_pose": dict(servo_angles),
           "shelf": {"ok": False}, "slots": [], "markers": seen}
    if expect_shelf is not None:
        sid = expect_shelf if expect_shelf in slot_obs else None
    else:
        sid = max(slot_obs, key=lambda s: len(slot_obs[s])) if slot_obs else None
    fit = _fit_shelf(slot_obs.get(sid, []), layout) if sid is not None else None
    if fit is None:
        for k in range(1, layout.n + 1):
            out["slots"].append({"slot": k, "state": "unknown"})
        return out
    org, th, ztop = fit
    c, s = math.cos(th), math.sin(th)
    out["shelf"] = {"ok": True, "shelf_id": sid, "x_mm": round(float(org[0]), 1), "y_mm": round(float(org[1]), 1),
                    "z_mm": round(ztop, 1), "yaw_deg": round(math.degrees(th), 2),
                    "slot_markers": sorted(k for k, _, _ in slot_obs[sid])}

    def to_shelf(p):
        d = np.array([p[0] - org[0], p[1] - org[1]])
        return np.array([c * d[0] + s * d[1], -s * d[0] + c * d[1]])

    def to_body(lx, ly):
        return [float(org[0] + c * lx - s * ly), float(org[1] + s * lx + c * ly)]

    in_slot = dict((k, []) for k in range(1, layout.n + 1))
    for m, name, size, gz, pb, nh in boxes:
        ctr = pb - nh * (size[0] / 2.0)              # 앞면 가운데 -> 박스 가운데(안쪽으로 깊이 절반)
        loc = to_shelf(ctr)
        k = int(round((layout.n - 1) / 2.0 - loc[1] / layout.slot_w)) + 1
        z_ok = abs(pb[2] - (ztop + size[2] / 2.0)) <= 30.0
        x_ok = -15.0 <= loc[0] - size[0] / 2.0 <= layout.depth
        if not (1 <= k <= layout.n) or abs(loc[1] - layout.slot_y(k)) > layout.slot_w / 2.0 or not (z_ok and x_ok):
            continue
        yaw = math.degrees(math.atan2(-nh[1], -nh[0]))
        in_slot[k].append({"marker_id": m.id, "type": name,
                           "center_mm": [round(float(ctr[0]), 1), round(float(ctr[1]), 1), round(ztop + gz, 1)],
                           "yaw_deg": round(yaw, 2)})
    seen_slots = set(k for k, _, _ in slot_obs[sid])
    for k in range(1, layout.n + 1):
        b = in_slot[k]
        if len(b) == 1:
            e = {"slot": k, "state": b[0]["type"]}
            e.update(b[0])
            del e["type"]
        else:
            bx, by = to_body(layout.place_x, layout.slot_y(k))
            st = "unknown" if b else ("empty" if k in seen_slots else "unknown")
            e = {"slot": k, "state": st, "center_mm": [round(bx, 1), round(by, 1), round(ztop, 1)]}
        out["slots"].append(e)
    return out
