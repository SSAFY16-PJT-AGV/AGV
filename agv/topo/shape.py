# -*- coding: utf-8 -*-
"""주행 기록(trace.jsonl)으로 맵의 실제 모양(노드 좌표·엣지 곡선)을 계산한다.

엣지 길이만으로 그린 배치(export.estimate_layout)는 연결 관계는 맞지만 모양은 추정이다. 탐색 중에
runner가 프레임마다 남긴 이동거리(odo)와 좌우 모터 명령 차이 적분(yaw)을 쓰면 로봇이 실제로 그린
궤적을 되살릴 수 있다(추측 항법).

    회전각(rad) = yaw_mm / 실효 궤도 폭(mm)       (왼쪽이 빠르면 +, 위에서 볼 때 시계 방향)

궤도 차체는 돌 때 미끄러져서 실효 궤도 폭이 실제 폭보다 크고(보통 1.2~2배) 바닥·배터리에 따라
달라진다. 그래서 폭을 미리 재지 않고 주행 기록에서 맞춘다. 같은 패치(노드)를 다시 지나면 그 사이
궤적은 닫힌 고리여야 한다(같은 자리, 같은 방향으로 들어왔으면 360도 x 바퀴 수). 이 조건을 가장 잘
맞추는 폭을 고른다.

계산 순서
    1. 맵의 각 노드 방문(debug.events)이 trace의 어느 프레임인지 찾는다(그 순간 odo == 이벤트 odo_mm).
    2. 실효 폭 후보마다 궤적을 적분하고, 방문 자세(x, y, 각도)를 변수로 하는 pose graph를 푼다.
       - 연속한 두 방문 사이: 적분한 상대 자세(거리에 비례해 오차를 허용)
       - 같은 노드의 방문끼리: 같은 위치(같은 엣지로 들어왔으면 같은 방향)
       - 같은 자리 교차(same_spot) 노드끼리: 같은 위치
    3. 고리 오차가 가장 작은 폭(제자리 회전용 폭은 따로)을 고르고, 그때의 해로
       노드 좌표 = 방문 위치 평균, 엣지 곡선 = 지나간 궤적들을 끝점에 맞게 편 뒤 평균.

좌표계는 수학 좌표(mm, y가 진행 방향 왼쪽)이고, 원점 노드를 처음 지날 때의 위치·진행 방향이
(0, 0)·+x다. agv-map-v1로 내보낼 때 y를 뒤집는다(export.to_agv_map_v1).

numpy가 필요하다(Jetson 기본 설치에 있음). Python 3.6 호환.
"""
from __future__ import division

import io
import json
import math
import os

TRACE_NAME = "trace.jsonl"
DEFAULT_OPTS = {
    "track_range_mm": (70.0, 600.0),   # 실효 궤도 폭 탐색 범위
    "grid": 48,                         # 처음 훑는 후보 수(로그 간격)
    "sigma_odo_xy": (3.0, 0.03),        # 방문 사이 위치 오차: a + b x 이동거리(mm)
    "sigma_odo_th": (0.01, 0.06),       # 방문 사이 방향 오차: a + b x |돈 각도|(rad)
    "sigma_same_xy": 15.0,              # 같은 노드 방문끼리 위치 차이(검출 위치 흔들림)
    "sigma_same_th": 0.10,              # 같은 엣지로 들어온 방문끼리 방향 차이(rad)
    "huber_k": 2.5,                     # 같은 노드 조건의 강건 가중(시그마 배수)
    "patch_offset_mm": (75.0, 5.0),     # 패치 이벤트 지점(앞 가장자리)에서 패치 가운데까지(기본값, 시그마).
                                        # trace에 패치 길이가 있으면 기본값은 그 절반
    "iters": 12,
    "min_loops": 1,                     # 같은 노드를 다시 지난 횟수가 이보다 적으면 계산 안 함
    "resample_mm": 15.0,                # 엣지 곡선 점 간격(평균 낼 때)
    "simplify_mm": 2.0,                 # 내보낼 때 곡선 단순화 허용 오차
}


# ---------------------------------------------------------------- trace

def trace_path_for(events_path):
    p = os.path.join(os.path.dirname(os.path.abspath(events_path)), TRACE_NAME)
    return p if os.path.exists(p) else None


def read_trace(path):
    """trace.jsonl -> {"ts", "o", "y", "p"} 리스트 dict. 깨진 줄은 건너뛴다."""
    out = {"ts": [], "o": [], "y": [], "p": [], "s": [], "meta": {}}
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "trace" in r:
                out["meta"] = r
                continue
            try:
                out["ts"].append(float(r["ts"]))
                out["o"].append(float(r["o"]))
                out["y"].append(float(r["y"]))
                out["p"].append(float(r.get("p", 0.0)))
                out["s"].append(r.get("s", "f"))
            except (KeyError, TypeError, ValueError):
                continue
    return out


# ---------------------------------------------------------------- 도우미

def _np():
    import numpy as np
    return np


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


class _Run(object):
    """한 주행(로그 파일)의 trace 배열과 추측 항법 적분."""

    def __init__(self, tr):
        np = _np()
        self.ts = np.asarray(tr["ts"], float)
        self.o = np.asarray(tr["o"], float)
        y = np.asarray(tr["y"], float)
        p = np.asarray(tr["p"], float)
        d = lambda a: np.concatenate([[0.0], np.diff(a)]) if len(a) else a  # noqa: E731 (numpy 1.13 호환)
        self.dO = d(self.o)
        dy = d(y)
        self.dP = d(p)
        self.dF = dy - self.dP
        self.pivot_total = float(np.abs(self.dP).sum())
        self.st = tr["s"]

    def integrate(self, b, bp):
        """궤적(N,3): x, y, th(수학 좌표, 반시계 +). 프레임 i의 이동은 그 프레임 명령으로 한 것."""
        np = _np()
        dth = -(self.dF / b + self.dP / bp)
        th = np.cumsum(dth)
        mid = th - dth / 2.0
        x = np.cumsum(self.dO * np.cos(mid))
        y = np.cumsum(self.dO * np.sin(mid))
        return np.stack([x, y, th], axis=1)

    def crossings(self, d):
        """odo가 d를 위로 지나는 (실수) 인덱스들."""
        np = _np()
        o = self.o
        if len(o) < 2:
            return []
        a, b = o[:-1] - d, o[1:] - d
        idx = np.nonzero((a < 0) & (b >= 0))[0]
        out = []
        for i in idx:
            f = -a[i] / (b[i] - a[i]) if b[i] != a[i] else 1.0
            out.append(i + f)
        if o[0] >= d and (len(o) < 2 or o[1] >= d) and abs(o[0] - d) < 5.0:
            out.insert(0, 0.0)
        return out

    def ts_at(self, fi):
        i = int(math.floor(fi))
        if i >= len(self.ts) - 1:
            return float(self.ts[-1])
        f = fi - i
        return float(self.ts[i] * (1 - f) + self.ts[i + 1] * f)


def _interp(traj, fi):
    i = int(math.floor(fi))
    n = len(traj)
    if i >= n - 1:
        return traj[-1].copy()
    if i < 0:
        return traj[0].copy()
    f = fi - i
    return traj[i] * (1 - f) + traj[i + 1] * f


def _rel(pa, pb):
    """pa 기준 pb의 상대 자세(dx, dy, dth). 방향 차이는 감지 않음(바퀴 수 보존)."""
    c, s = math.cos(pa[2]), math.sin(pa[2])
    dx, dy = pb[0] - pa[0], pb[1] - pa[1]
    return (c * dx + s * dy, -s * dx + c * dy, pb[2] - pa[2])


def _compose(pa, rel):
    c, s = math.cos(pa[2]), math.sin(pa[2])
    return (pa[0] + c * rel[0] - s * rel[1], pa[1] + s * rel[0] + c * rel[1], pa[2] + rel[2])


# ---------------------------------------------------------------- 방문 찾기

def _visits(m, runs):
    """debug.events에서 맵 노드 방문을 trace 프레임에 맞춘다. 반환: 방문 리스트(시간순)."""
    node_ids = set(n["id"] for n in m["nodes"])
    evs = (m.get("debug") or {}).get("events") or []
    out = []
    miss = 0
    for order, e in enumerate(evs):
        r = runs.get(e.get("run"))
        if r is None or e.get("node") not in node_ids or e.get("d_mm") is None:
            continue
        cands = r.crossings(float(e["d_mm"]))
        if not cands:
            miss += 1
            continue
        ts = e.get("ts")
        if ts is None:
            fi = cands[0]
        else:
            fi = min(cands, key=lambda c: abs(r.ts_at(c) - ts))
            if abs(r.ts_at(fi) - ts) > 5.0:
                miss += 1
                continue
        out.append({"run": e["run"], "seg": e.get("seg", 0), "node": e["node"], "type": e["type"],
                    "branch": e.get("detail") if e["type"] == "split" else None,
                    "d": float(e["d_mm"]), "fi": fi, "cands": cands, "order": order})
    # 빌더가 정한 순서(가까운 이벤트는 종류 순으로 정리됨)를 따라야 엣지와 맞는다
    out.sort(key=lambda v: (v["run"], v["seg"], v["order"]))
    # 같은 주행 구간 안에서 앞뒤 방문, 들어온 엣지, 마지막으로 떠난 프레임(스퍼에 들어갔다 나온 경우)
    for k, v in enumerate(out):
        prev = out[k - 1] if k > 0 and (out[k - 1]["run"], out[k - 1]["seg"]) == (v["run"], v["seg"]) else None
        nxt = out[k + 1] if k + 1 < len(out) and (out[k + 1]["run"], out[k + 1]["seg"]) == (v["run"], v["seg"]) \
            else None
        v["prev"] = k - 1 if prev is not None else None
        v["next"] = k + 1 if nxt is not None else None
        v["in_key"] = (prev["node"], prev["branch"]) if prev is not None else None
        end = nxt["fi"] if nxt is not None else float("inf")
        later = [c for c in v["cands"] if v["fi"] <= c < end]
        v["dep"] = later[-1] if later else v["fi"]
    return out, miss


# ---------------------------------------------------------------- pose graph

class _Graph(object):
    def __init__(self, visits, m, opts):
        self.v = visits
        self.o = opts
        same = {}
        for k, v in enumerate(visits):
            same.setdefault(v["node"], []).append(k)
        self.same = same
        spots = []
        for nd in m["nodes"]:
            for other in nd.get("same_spot") or []:
                if nd["id"] < other and nd["id"] in same and other in same:
                    spots.append((same[nd["id"]][0], same[other][0]))
        self.spots = spots
        self.loops = sum(len(ks) - 1 for ks in same.values())

    def factors(self, poses_raw):
        """현재 궤적 적분값으로 측정값을 만든다. poses_raw[k] = 방문 k의 적분 자세."""
        o = self.o
        F = []
        for k, v in enumerate(self.v):
            if v["next"] is None:
                continue
            j = v["next"]
            z = _rel(poses_raw[k], poses_raw[j])
            L = v["_dist_next"]
            sxy = o["sigma_odo_xy"][0] + o["sigma_odo_xy"][1] * L
            sth = o["sigma_odo_th"][0] + o["sigma_odo_th"][1] * abs(z[2])
            F.append(("odo", k, j, z, sxy, sth))
        for nid, ks in self.same.items():
            a = ks[0]
            patch = self.v[a]["type"] == "patch"
            for b in ks[1:]:
                F.append(("pos", a, b, patch, self.o["sigma_same_xy"], None))
                if self.v[a]["in_key"] is not None and self.v[a]["in_key"] == self.v[b]["in_key"]:
                    turns = round((poses_raw[b][2] - poses_raw[a][2]) / (2 * math.pi))
                    F.append(("th", a, b, turns * 2 * math.pi, None, self.o["sigma_same_th"]))
        for a, b in self.spots:
            F.append(("pos", a, b, False, self.o["sigma_same_xy"], None))
        return F

    def solve(self, init, F, anchor):
        """가우스-뉴턴 + 같은 노드 조건에 Huber 가중. 반환: (자세 (N,3), 패치 가운데 거리, 가중 제곱합).

        패치는 들어오는 방향마다 이벤트 지점(앞 가장자리)이 달라서, 방문 자세에서 진행 방향으로
        c만큼 간 점(패치 가운데)이 같아야 한다고 본다. c도 같이 푼다."""
        np = _np()
        X = np.array(init, float)
        n = len(X)
        c0, sc = self.o["patch_offset_mm"]
        cp = c0
        k_h = self.o["huber_k"]
        cost = 0.0
        N = 3 * n + 1
        for it in range(self.o["iters"]):
            H = np.zeros((N, N))
            g = np.zeros(N)
            H[N - 1, N - 1] = 1.0 / sc ** 2
            g[N - 1] = (cp - c0) / sc ** 2
            cost = ((cp - c0) / sc) ** 2
            for f in F:
                kind, a, b, z, sxy, sth = f
                pa, pb = X[a], X[b]
                if kind == "odo":
                    c, s = math.cos(pa[2]), math.sin(pa[2])
                    dx, dy = pb[0] - pa[0], pb[1] - pa[1]
                    r = np.array([c * dx + s * dy - z[0], -s * dx + c * dy - z[1], pb[2] - pa[2] - z[2]])
                    J = np.zeros((3, 6))
                    J[0, 0:2] = [-c, -s]
                    J[1, 0:2] = [s, -c]
                    J[0, 2] = -s * dx + c * dy
                    J[1, 2] = -c * dx - s * dy
                    J[0, 3:5] = [c, s]
                    J[1, 3:5] = [-s, c]
                    J[2, 2], J[2, 5] = -1.0, 1.0
                    w = np.array([1 / sxy ** 2, 1 / sxy ** 2, 1 / sth ** 2])
                elif kind == "pos":
                    r = np.array([pb[0] - pa[0], pb[1] - pa[1]])
                    J = np.zeros((2, 7))
                    J[0, 0], J[1, 1], J[0, 3], J[1, 4] = -1, -1, 1, 1
                    if z:      # 패치: 가운데 점끼리
                        ca, sa, cb, sb = math.cos(pa[2]), math.sin(pa[2]), math.cos(pb[2]), math.sin(pb[2])
                        r = r + cp * np.array([cb - ca, sb - sa])
                        J[0, 2], J[1, 2] = cp * sa, -cp * ca
                        J[0, 5], J[1, 5] = -cp * sb, cp * cb
                        J[0, 6], J[1, 6] = cb - ca, sb - sa
                    e = math.sqrt(r[0] ** 2 + r[1] ** 2) / sxy
                    hw = 1.0 if e <= k_h else k_h / e
                    w = np.array([hw / sxy ** 2] * 2)
                else:
                    r = np.array([pb[2] - pa[2] - z])
                    J = np.zeros((1, 6))
                    J[0, 2], J[0, 5] = -1, 1
                    e = abs(r[0]) / sth
                    hw = 1.0 if e <= k_h else k_h / e
                    w = np.array([hw / sth ** 2])
                cost += float(np.sum(w * r * r))
                ids = [3 * a, 3 * a + 1, 3 * a + 2, 3 * b, 3 * b + 1, 3 * b + 2]
                if J.shape[1] == 7:
                    ids.append(N - 1)
                JW = J.T * w
                H[np.ix_(ids, ids)] += JW.dot(J)
                g[ids] += JW.dot(r)
            # 원점 고정
            for q in range(3):
                H[3 * anchor + q, 3 * anchor + q] += 1e9
            H += np.eye(N) * 1e-9
            try:
                dx = -np.linalg.solve(H, g)
            except np.linalg.LinAlgError:
                break
            X += dx[:-1].reshape(-1, 3)
            cp += dx[-1]
            if np.max(np.abs(dx)) < 1e-3:
                break
        return X, cp, cost

    def loop_residuals(self, X, cp):
        out = []
        for nid, ks in self.same.items():
            c = cp if self.v[ks[0]]["type"] == "patch" else 0.0
            pts = [(X[k][0] + c * math.cos(X[k][2]), X[k][1] + c * math.sin(X[k][2])) for k in ks]
            for q in pts[1:]:
                out.append(math.hypot(q[0] - pts[0][0], q[1] - pts[0][1]))
        return out


def _evaluate(graph, runs, visits, b, bp, anchor):
    np = _np()
    trajs = dict((name, r.integrate(b, bp)) for name, r in runs.items())
    raw = [_interp(trajs[v["run"]], v["fi"]) for v in visits]
    F = graph.factors(raw)
    # 초기값: 구간별 적분 자세를 그대로(구간이 여럿이면 앞 구간 끝에 이어 붙임)
    X = np.array(raw, float)
    X0 = X[anchor].copy()
    c, s = math.cos(-X0[2]), math.sin(-X0[2])
    R = np.array([[c, -s], [s, c]])
    X[:, :2] = (X[:, :2] - X0[:2]).dot(R.T)
    X[:, 2] -= X0[2]
    X, cp, cost = graph.solve(X, F, anchor)
    return {"b": b, "bp": bp, "X": X, "cp": cp, "cost": cost, "trajs": trajs, "raw": raw}


def _search(fn, lo, hi, grid):
    """로그 간격으로 훑은 뒤 가장 낮은 곳 주변을 황금분할로 좁힌다. 반환: (x, 값, 훑은 값들)."""
    ls = [math.log(lo) + (math.log(hi) - math.log(lo)) * i / (grid - 1) for i in range(grid)]
    vals = [fn(math.exp(x)) for x in ls]
    i = min(range(grid), key=lambda k: vals[k])
    a = ls[max(0, i - 1)]
    c = ls[min(grid - 1, i + 1)]
    gr = (math.sqrt(5) - 1) / 2
    x1, x2 = c - gr * (c - a), a + gr * (c - a)
    f1, f2 = fn(math.exp(x1)), fn(math.exp(x2))
    for _ in range(18):
        if f1 < f2:
            c, x2, f2 = x2, x1, f1
            x1 = c - gr * (c - a)
            f1 = fn(math.exp(x1))
        else:
            a, x1, f1 = x1, x2, f2
            x2 = a + gr * (c - a)
            f2 = fn(math.exp(x2))
    x = (a + c) / 2
    return math.exp(x), fn(math.exp(x)), list(zip([math.exp(v) for v in ls], vals))


# ---------------------------------------------------------------- 곡선

def _resample(pts, n):
    np = _np()
    pts = np.asarray(pts, float)
    seg = np.hypot(*np.diff(pts, axis=0).T) if len(pts) > 1 else np.zeros(0)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    if s[-1] <= 1e-9:
        return np.repeat(pts[:1], n, axis=0)
    t = np.linspace(0, s[-1], n)
    return np.stack([np.interp(t, s, pts[:, 0]), np.interp(t, s, pts[:, 1])], axis=1)


def _simplify(pts, tol):
    """Douglas-Peucker."""
    if len(pts) < 3:
        return list(pts)
    a, b = pts[0], pts[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    best, bi = -1.0, 0
    for i in range(1, len(pts) - 1):
        p = pts[i]
        d = (abs(dy * (p[0] - a[0]) - dx * (p[1] - a[1])) / L) if L > 1e-9 else math.hypot(p[0] - a[0], p[1] - a[1])
        if d > best:
            best, bi = d, i
    if best <= tol:
        return [a, b]
    left = _simplify(pts[:bi + 1], tol)
    return left[:-1] + _simplify(pts[bi:], tol)


def _traversal_path(sol, v, w, pa_opt, pb_opt):
    """방문 v(떠난 프레임) -> w(도착 프레임) 궤적을 최적화된 양 끝 자세에 맞게 옮기고 편다."""
    np = _np()
    traj = sol["trajs"][v["run"]]
    i0, i1 = v["dep"], w["fi"]
    if i1 <= i0:
        return np.array([pa_opt[:2], pb_opt[:2]], float)
    a, b = int(math.ceil(i0)), int(math.floor(i1))
    pts = [_interp(traj, i0)] + [traj[i] for i in range(a, b + 1)] + [_interp(traj, i1)]
    pts = np.array(pts, float)
    # 도착 프레임 자세(최적화값) 기준의 강체 변환: 적분 궤적의 v 도착 자세 -> 최적화된 v 자세
    rv = _interp(traj, v["fi"])
    dth = pa_opt[2] - rv[2]
    c, s = math.cos(dth), math.sin(dth)
    R = np.array([[c, -s], [s, c]])
    P = (pts[:, :2] - rv[:2]).dot(R.T) + pa_opt[:2]
    # 끝점 오차를 거리 비율로 나눠 편다
    seg = np.hypot(*np.diff(P, axis=0).T)
    sfrac = np.concatenate([[0.0], np.cumsum(seg)])
    sfrac = sfrac / sfrac[-1] if sfrac[-1] > 1e-9 else np.linspace(0, 1, len(P))
    start_err = pa_opt[:2] - P[0]
    end_err = pb_opt[:2] - P[-1]
    P = P + np.outer(1 - sfrac, start_err) + np.outer(sfrac, end_err)
    return P


# ---------------------------------------------------------------- 본체

def estimate_shape(m, traces, opts=None):
    """build_map 결과 m과 {주행 이름: read_trace 결과}로 맵 모양을 계산한다.

    반환 dict(실패하면 None과 이유):
        pos: {노드: (x, y)} mm, heading: {노드: (ux, uy)} 노드를 지나는 진행 방향 단위 벡터,
        shapes: {엣지 ID: [(x, y), ...]} 양 끝 노드 포함, spur_end: {노드: (x, y)} 들어가 본 스퍼 끝,
        fit: 실효 궤도 폭, 고리 오차 등 요약.
    """
    try:
        np = _np()
    except ImportError:
        return None, u"numpy가 없어서 모양 계산을 건너뜀"
    o = dict(DEFAULT_OPTS)
    o.update(opts or {})
    runs = {}
    for name, tr in (traces or {}).items():
        if tr and len(tr.get("o", [])) > 10:
            runs[name] = _Run(tr)
    if not runs:
        return None, u"주행 기록(trace.jsonl)이 없음"
    visits, miss = _visits(m, runs)
    if len(visits) < 3:
        return None, u"주행 기록에서 노드 방문을 찾지 못함"
    for v in visits:
        if v["next"] is not None:
            w = visits[v["next"]]
            r = runs[v["run"]]
            a, b = int(math.floor(v["fi"])), int(math.ceil(w["fi"]))
            v["_dist_next"] = float(np.abs(r.dO[a + 1:b + 1]).sum())
    graph = _Graph(visits, m, o)
    if graph.loops < o["min_loops"]:
        return None, u"같은 노드를 다시 지난 적이 없어 고리로 회전 폭을 맞출 수 없음"
    lens = [tr.get("meta", {}).get("patch_length_mm") for tr in traces.values() if tr]
    lens = [float(x) for x in lens if x]
    if lens and "patch_offset_mm" not in (opts or {}):
        o["patch_offset_mm"] = (sum(lens) / len(lens) / 2.0, o["patch_offset_mm"][1])
    origin = m.get("origin_node")
    anchor = next((k for k, v in enumerate(visits) if v["node"] == origin), 0)

    cache = {}

    def f(b, bp):
        key = (round(b, 3), round(bp, 3))
        if key not in cache:
            cache[key] = _evaluate(graph, runs, visits, b, bp, anchor)
        return cache[key]["cost"]

    lo, hi = o["track_range_mm"]
    has_pivot = sum(r.pivot_total for r in runs.values()) > 0.5 * lo * math.pi
    b, _, scan = _search(lambda x: f(x, x), lo, hi, o["grid"])
    bp = b
    if has_pivot:
        bp, _, _ = _search(lambda x: f(b, x), lo, hi, max(12, o["grid"] // 2))
        b, _, scan = _search(lambda x: f(x, bp), lo, hi, o["grid"])
    sol = _evaluate(graph, runs, visits, b, bp, anchor)
    X0, cp = sol["X"], sol["cp"]
    res = graph.loop_residuals(X0, cp)
    raw_res = graph.loop_residuals(np.array(sol["raw"]), cp)

    # 패치 방문은 패치 가운데(이벤트 지점 + cp)를 지나는 순간으로 옮긴다. 노드 좌표·엣지 곡선이 가운데 기준
    orig = visits
    visits = [dict(v) for v in visits]
    X = X0.copy()
    for k, v in enumerate(visits):
        if v["type"] != "patch":
            continue
        r = runs[v["run"]]
        cands = r.crossings(v["d"] + cp)
        end = orig[v["next"]]["fi"] if v["next"] is not None else float("inf")
        fi = next((c for c in cands if c >= v["fi"]), None)
        if fi is None or fi >= end:
            continue
        # 패치 위에서 스퍼로 잘못 들어갔다 나온 경우까지: 다음 방문 전 마지막으로 가운데를 지난 프레임
        dep = [c for c in cands if fi <= c < end][-1]
        traj = sol["trajs"][v["run"]]
        X[k] = _compose(X0[k], _rel(_interp(traj, v["fi"]), _interp(traj, fi)))
        v["fi"], v["dep"] = fi, dep

    # 노드 좌표 = 방문 위치 평균(튀는 방문은 빼고)
    pos, heading = {}, {}
    for nid, ks in graph.same.items():
        P = X[ks, :2]
        med = np.median(P, axis=0)
        d = np.hypot(*(P - med).T)
        keep = d <= max(3 * o["sigma_same_xy"], 3 * float(np.median(d)) + 1e-9)
        pos[nid] = tuple(float(v) for v in P[keep].mean(axis=0))
        ux = float(np.mean(np.cos(X[ks, 2])))
        uy = float(np.mean(np.sin(X[ks, 2])))
        L = math.hypot(ux, uy) or 1.0
        heading[nid] = (ux / L, uy / L)

    # 설계 문서 좌표계: 원점 노드가 (0, 0), 원점을 지나는 진행 방향이 +x
    if origin in pos:
        ox, oy = pos[origin]
        hx, hy = heading[origin]
        ang = math.atan2(hy, hx)
        c, s = math.cos(-ang), math.sin(-ang)
        R = np.array([[c, -s], [s, c]])
        X = X.copy()
        X[:, :2] = (X[:, :2] - [ox, oy]).dot(R.T)
        X[:, 2] -= ang
        tf = lambda p: (c * (p[0] - ox) - s * (p[1] - oy), s * (p[0] - ox) + c * (p[1] - oy))  # noqa: E731
        pos = dict((k, tf(p)) for k, p in pos.items())
        heading = dict((k, (c * h[0] - s * h[1], s * h[0] + c * h[1])) for k, h in heading.items())

    # 엣지 곡선
    eid = dict(((e["from"], e["to"], e.get("branch")), e["id"]) for e in m["edges"])
    paths = {}
    for k, v in enumerate(visits):
        if v["next"] is None:
            continue
        w = visits[v["next"]]
        e = eid.get((v["node"], w["node"], v["branch"]))
        if e is None:
            continue
        paths.setdefault(e, []).append(_traversal_path(sol, v, w, X[k], X[v["next"]]))
    shapes = {}
    for e in m["edges"]:
        ps = paths.get(e["id"])
        a, b2 = pos.get(e["from"]), pos.get(e["to"])
        if not ps or a is None or b2 is None:
            continue
        L = max(float(np.hypot(*np.diff(p, axis=0).T).sum()) for p in ps)
        n = max(2, int(L / o["resample_mm"]) + 1)
        avg = np.mean([_resample(p, n) for p in ps], axis=0)
        sfrac = np.linspace(0, 1, n)
        avg = avg + np.outer(1 - sfrac, np.array(a) - avg[0]) + np.outer(sfrac, np.array(b2) - avg[-1])
        pts = [tuple(float(round(c, 1)) for c in p) for p in avg]
        shapes[e["id"]] = _simplify(pts, o["simplify_mm"])

    # 들어가 본 스퍼(tee): 갈림길에서 가장 멀리 간 지점 방향
    spur_end = {}
    for k, v in enumerate(visits):
        if v["type"] != "tee" or v["dep"] <= v["fi"] + 1:
            continue
        r = runs[v["run"]]
        traj = sol["trajs"][v["run"]]
        a, b2 = int(math.ceil(v["fi"])), int(math.floor(v["dep"]))
        if b2 <= a:
            continue
        i_far = a + int(np.argmax(r.o[a:b2 + 1]))
        rv = _interp(traj, v["fi"])
        rel = _rel(rv, traj[i_far])
        far = _compose(X[k], rel)
        nd = next((n for n in m["nodes"] if n["id"] == v["node"]), {})
        length = nd.get("length_mm")
        p0 = pos[v["node"]]
        dx, dy = far[0] - p0[0], far[1] - p0[1]
        L = math.hypot(dx, dy)
        if L < 20:
            continue
        if length:
            dx, dy = dx / L * length, dy / L * length
        spur_end.setdefault(v["node"], []).append((p0[0] + dx, p0[1] + dy))
    spur_end = dict((n, (sum(p[0] for p in ps) / len(ps), sum(p[1] for p in ps) / len(ps)))
                    for n, ps in spur_end.items())

    # 폭이 잘 정해졌는지: 최적 폭에서 ±25% 바꾸면 고리 오차가 얼마나 커지는지
    c0 = sol["cost"]
    c_lo, c_hi = f(b / 1.25, bp), f(b * 1.25, bp)
    sharp = min(c_lo, c_hi) / max(c0, 1e-9)
    fit = {
        "track_eff_mm": round(b, 1),
        "patch_center_mm": round(float(cp), 1),
        "pivot_track_eff_mm": round(bp, 1) if has_pivot else None,
        "visits": len(visits),
        "visits_unmatched": miss,
        "loops": graph.loops,
        "loop_rms_mm": round(math.sqrt(sum(x * x for x in res) / len(res)), 1) if res else None,
        "loop_max_mm": round(max(res), 1) if res else None,
        "loop_rms_before_mm": round(math.sqrt(sum(x * x for x in raw_res) / len(raw_res)), 1) if raw_res else None,
        "sharpness": round(float(sharp), 2),
        "edges_with_shape": len(shapes),
    }
    warns = []
    if sharp < 1.5:
        warns.append(u"실효 궤도 폭이 잘 정해지지 않음(고리가 적거나 짧음). 모양을 그대로 믿지 말 것")
    if fit["loop_max_mm"] is not None and fit["loop_max_mm"] > 80:
        warns.append(u"같은 노드 방문 위치가 최대 %.0fmm 어긋남(노드 판정 오류나 미끄러짐 의심)" % fit["loop_max_mm"])
    fit["warnings"] = warns
    return {"pos": pos, "heading": heading, "shapes": shapes, "spur_end": spur_end, "fit": fit,
            "scan": [(round(x, 1), round(v, 1)) for x, v in scan]}, None


def load_traces_for(event_paths, names):
    """events.jsonl 옆의 trace.jsonl을 읽는다. 반환: {주행 이름: trace}."""
    out = {}
    for p, name in zip(event_paths, names):
        tp = trace_path_for(p)
        if tp:
            out[name] = read_trace(tp)
    return out
