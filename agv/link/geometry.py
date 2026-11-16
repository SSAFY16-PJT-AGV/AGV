# -*- coding: utf-8 -*-
"""맵 좌표(mm)로 로봇 자세 구하기: "엣지 E 위, 시작 노드에서 s mm" -> (x, y, 방향).

맵 엣지 모양은 통일 형식의 shape(mm)를 쓰고, 없으면 x, y·points(m)로 만든다. 주행 엣지 이름
"E~r"(반대 방향)은 모양을 뒤집는다. 노드 사이 위치는 이동거리 비율로 모양 위에 놓는다
(엣지 길이 = 맵 length, 모양 길이와 다르면 비율로 맞춤). 방향 th는 맵 좌표계에서 x축으로부터
atan2(dy, dx)를 도로 잰 값이다(맵 coordinate_frame 그대로, 변환하지 않음).

표준 라이브러리만 쓴다. Python 3.6 호환.
"""
import math


def _node_mm(n):
    if n.get("pos") is not None:
        return float(n["pos"][0]), float(n["pos"][1])
    if n.get("x") is not None and n.get("y") is not None:
        return float(n["x"]) * 1000.0, float(n["y"]) * 1000.0
    return None


class MapGeometry(object):
    def __init__(self, raw_map):
        self.nodes = {}
        for n in raw_map.get("nodes", []):
            p = _node_mm(n)
            if p is not None:
                self.nodes[n["id"]] = p
        self.shapes = {}
        for e in raw_map.get("edges", []):
            a, b = self.nodes.get(e.get("from")), self.nodes.get(e.get("to"))
            if a is None or b is None:
                continue
            if e.get("shape") and len(e["shape"]) >= 2:
                pts = [(float(p[0]), float(p[1])) for p in e["shape"]]
            else:
                mid = []
                for p in e.get("points") or []:
                    if isinstance(p, dict):
                        mid.append((float(p["x"]) * 1000.0, float(p["y"]) * 1000.0))
                    else:
                        mid.append((float(p[0]) * 1000.0, float(p[1]) * 1000.0))
                pts = [a] + mid + [b]
            clean = [pts[0]]
            for p in pts[1:]:
                if p != clean[-1]:
                    clean.append(p)
            if len(clean) < 2:
                continue
            self.shapes[e.get("id")] = clean

    def shape(self, eid):
        """주행 엣지 ID(반대 방향 "~r" 포함)의 모양(mm 점 목록). 모르면 None."""
        rev = eid.endswith("~r")
        pts = self.shapes.get(eid[:-2] if rev else eid)
        if pts is None:
            return None
        return list(reversed(pts)) if rev else pts

    def node_pose(self, node, heading=None):
        p = self.nodes.get(node)
        if p is None:
            return None
        return (p[0], p[1], heading)

    def heading_at_end(self, eid):
        """주행 엣지 끝에서의 방향(도)."""
        pts = self.shape(eid)
        if not pts:
            return None
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
        return math.degrees(math.atan2(y1 - y0, x1 - x0))

    def heading_at_start(self, eid):
        pts = self.shape(eid)
        if not pts:
            return None
        (x0, y0), (x1, y1) = pts[0], pts[1]
        return math.degrees(math.atan2(y1 - y0, x1 - x0))

    def along(self, eid, frac):
        """주행 엣지 모양 위 비율 frac(0~1) 지점의 (x, y, 방향 도)."""
        pts = self.shape(eid)
        if not pts:
            return None
        frac = min(1.0, max(0.0, float(frac)))
        segs = [math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(pts[:-1], pts[1:])]
        total = sum(segs)
        if total <= 0:
            return (pts[0][0], pts[0][1], None)
        want = frac * total
        acc = 0.0
        for (p, q), L in zip(zip(pts[:-1], pts[1:]), segs):
            if L <= 0:
                continue
            if acc + L >= want - 1e-9:
                t = (want - acc) / L
                th = math.degrees(math.atan2(q[1] - p[1], q[0] - p[0]))
                return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, th)
            acc += L
        p, q = pts[-2], pts[-1]
        return (q[0], q[1], math.degrees(math.atan2(q[1] - p[1], q[0] - p[0])))
