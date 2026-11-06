# -*- coding: utf-8 -*-
"""가상 매트: 매트 정의(JSON) -> 위에서 본 이미지 렌더, 로봇 카메라 시점 영상 생성."""
import json
import math

import cv2
import numpy as np

COLORS_BGR = {
    "orange": (40, 110, 215),
    "yellow": (35, 195, 235),
    "green": (115, 175, 55),
    "purple": (150, 35, 95),
    "red": (40, 40, 200),
    "blue": (190, 90, 30),
}
BG_BGR = (222, 232, 240)
LINE_BGR = (28, 28, 28)


def catmull_rom(points, step_mm=5.0):
    """점들을 지나는 부드러운 곡선(조밀한 점 목록)."""
    p = [np.array(q, float) for q in points]
    if len(p) < 3:
        a, b = p[0], p[-1]
        n = max(2, int(np.linalg.norm(b - a) / step_mm))
        return [a + (b - a) * t for t in np.linspace(0, 1, n)]
    p = [2 * p[0] - p[1]] + p + [2 * p[-1] - p[-2]]
    out = []
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        n = max(2, int(np.linalg.norm(p2 - p1) / step_mm))
        for t in np.linspace(0, 1, n, endpoint=(i == len(p) - 3)):
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    return out


def polyline_length(pts):
    return float(sum(np.linalg.norm(b - a) for a, b in zip(pts[:-1], pts[1:])))


class Mat(object):
    def __init__(self, path, px_per_mm=1.0):
        with open(path) as f:
            self.d = json.load(f)
        self.s = px_per_mm
        self.w_mm, self.h_mm = self.d["size_mm"]
        self.curves = []
        for p in self.d["paths"]:
            pts = catmull_rom(p["points"])
            self.curves.append({"from": p["from"], "to": p["to"], "pts": pts,
                                "length_mm": polyline_length(pts)})
        self.nodes = {}
        for p in self.d["paths"]:
            self.nodes[p["from"]] = p["points"][0]
            self.nodes[p["to"]] = p["points"][-1]
        self.image = self.render()

    def to_px(self, xy):
        return (xy[0] * self.s, (self.h_mm - xy[1]) * self.s)

    def render(self):
        W, H = int(self.w_mm * self.s), int(self.h_mm * self.s)
        img = np.full((H, W, 3), BG_BGR, np.uint8)
        for p in self.d["patches"]:
            cx, cy = p["center"]
            w, h = p["size"]
            a = self.to_px((cx - w / 2.0, cy + h / 2.0))
            b = self.to_px((cx + w / 2.0, cy - h / 2.0))
            cv2.rectangle(img, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])),
                          COLORS_BGR[p["color"]], -1)
        lw = int(round(self.d["line_width_mm"] * self.s))
        for c in self.curves:
            pts = np.array([self.to_px(q) for q in c["pts"]], np.float32)
            cv2.polylines(img, [np.round(pts * 16).astype(np.int32)], False, LINE_BGR, lw,
                          cv2.LINE_AA, shift=4)
        return img

    def camera_view(self, pose, cam, out_w=320, out_h=240):
        """로봇 자세(x, y, th[rad])에서 앞 바닥을 비스듬히 본 영상(원근 포함)."""
        x, y, th = pose
        u = np.array([math.cos(th), math.sin(th)])
        v = np.array([-math.sin(th), math.cos(th)])   # 왼쪽
        o = np.array([x, y])
        near, far = cam["near_mm"], cam["far_mm"]
        wn, wf = cam["near_width_mm"] / 2.0, cam["far_width_mm"] / 2.0
        ground = [o + far * u + wf * v, o + far * u - wf * v,
                  o + near * u - wn * v, o + near * u + wn * v]
        src = np.array([self.to_px(g) for g in ground], np.float32)
        dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], np.float32)
        M = cv2.getPerspectiveTransform(dst, src)
        return cv2.warpPerspective(self.image, M, (out_w, out_h),
                                   flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                   borderMode=cv2.BORDER_CONSTANT, borderValue=(90, 90, 90))
