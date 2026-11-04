# -*- coding: utf-8 -*-
"""노드 이벤트 로그 -> 위상 맵 JSON (topo-v0).

노드 식별 방법
- 색 패치: 색이 곧 ID (P_yellow 등). 한 맵에서 같은 색 패치는 하나라고 가정한다.
- 분기(split)·합류(merge): 이름표가 없으므로 "주변 맥락"으로 같은 곳인지 판단한다.
    뒤쪽 서명 bwd = (직전 패치, 그 뒤로 지나온 분기·합류 토큰들)
    앞쪽 서명 fwd = (내 선택, 다음 패치까지 지날 토큰들, 다음 패치)
  같은 종류의 관측끼리 bwd가 같거나 fwd가 같으면 같은 노드로 묶는다(union-find).
  분기는 들어오는 길이 하나라 bwd로, 합류는 나가는 길이 하나라 fwd로 잘 묶인다.
  여기에 직전/다음 패치까지 거리가 비슷한지도 확인해서 검출 누락으로 인한 오결합을 막는다.

엣지 길이는 odo_mm 차이(없으면 시간 x 속도)의 평균이다. 실제 2D 형상은 아직 없다(shape: null).

Python 3.6 호환, 표준 라이브러리만 사용.
"""
import datetime
import math

from .events import split_runs

SCHEMA = "topo-v0"

# 분기·합류와 패치가 거의 같은 자리에서 검출되면 패치를 먼저 둔다.
# (노랑·보라 패치처럼 갈림길이 패치 위에 있을 때 들어오는 쪽에 따라 순서가 뒤바뀌는 것 방지)
DEFAULT_REORDER_MM = 40.0
# 같은 노드로 묶을 때 허용하는 거리 차이: max(abs, rel * 거리)
DEFAULT_DIST_TOL_ABS = 200.0
DEFAULT_DIST_TOL_REL = 0.25
_TYPE_ORDER = {"patch": 0, "merge": 1, "split": 2}


def _token(ev):
    if ev["type"] == "split":
        return "S" + (ev.get("choice") or "?")[0].upper()
    if ev["type"] == "merge":
        return "M"
    return "P"


class _UF(object):
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _mean_std(xs):
    if not xs:
        return None, None
    m = sum(xs) / float(len(xs))
    if len(xs) < 2:
        return m, 0.0
    v = sum((x - m) ** 2 for x in xs) / float(len(xs) - 1)
    return m, math.sqrt(v)


def _prepare_run(run_events, meta, reorder_mm):
    """구간 이벤트에 거리 d(mm)를 붙이고 근접 이벤트 순서를 정리한다."""
    speed = meta.get("speed_mm_s") or 0.0
    t0 = run_events[0].get("ts", 0.0)
    obs = []
    src = "odo"
    for ev in run_events:
        if ev.get("odo_mm") is not None:
            d = float(ev["odo_mm"])
        else:
            src = "time"
            d = (ev.get("ts", t0) - t0) * float(speed or 1.0)
        o = dict(ev)
        o["_d"] = d
        obs.append(o)
    # split은 처음 갈라진 지점 거리로 기록되어 파일 순서와 다를 수 있으므로 거리순 정렬
    if src == "odo":
        obs.sort(key=lambda o: o["_d"])
    # 근접 이벤트 정렬: 거리 reorder_mm 안에 모인 묶음 안에서 패치 > 합류 > 분기 순
    out = []
    i = 0
    while i < len(obs):
        j = i + 1
        while j < len(obs) and obs[j]["_d"] - obs[j - 1]["_d"] <= reorder_mm:
            j += 1
        group = obs[i:j]
        group.sort(key=lambda o: (_TYPE_ORDER.get(o["type"], 9), o["_d"]))
        out.extend(group)
        i = j
    return out, src


def _signatures(run):
    """구간 안 각 관측의 bwd/fwd 서명과 기준 패치까지 거리를 구한다."""
    n = len(run)
    sig = []
    for i, o in enumerate(run):
        # 뒤쪽
        bwd = None
        d_b = None
        toks = []
        for j in range(i - 1, -1, -1):
            if run[j]["type"] == "patch":
                bwd = (run[j]["color"], tuple(reversed(toks)))
                d_b = o["_d"] - run[j]["_d"]
                break
            toks.append(_token(run[j]))
        # 앞쪽
        fwd = None
        d_f = None
        toks = []
        for j in range(i + 1, n):
            if run[j]["type"] == "patch":
                own = o.get("choice") if o["type"] == "split" else None
                fwd = (own, tuple(toks), run[j]["color"])
                d_f = run[j]["_d"] - o["_d"]
                break
            toks.append(_token(run[j]))
        sig.append((bwd, d_b, fwd, d_f))
    return sig


def build_map(event_lists, map_id="map", version=0, origin_node=None,
              reorder_mm=DEFAULT_REORDER_MM, tol_abs=DEFAULT_DIST_TOL_ABS,
              tol_rel=DEFAULT_DIST_TOL_REL, run_names=None):
    """여러 주행 로그(각각 이벤트 리스트)를 합쳐 위상 맵 dict를 만든다."""
    warnings = []
    obs = []          # 전체 관측(평탄화)
    runs_idx = []     # 구간별 obs 인덱스 목록
    length_src = set()
    for li, events in enumerate(event_lists):
        name = run_names[li] if run_names else "run%d" % li
        for ri, (rev, meta) in enumerate(split_runs(events)):
            prepared, src = _prepare_run(rev, meta, reorder_mm)
            length_src.add(src)
            sigs = _signatures(prepared)
            idx = []
            for o, s in zip(prepared, sigs):
                o["_run"] = name
                o["_seg"] = ri
                o["_bwd"], o["_db"], o["_fwd"], o["_df"] = s
                idx.append(len(obs))
                obs.append(o)
            runs_idx.append(idx)

    def tol(d):
        return max(tol_abs, tol_rel * abs(d))

    # --- 노드 클러스터링 ---
    uf = _UF(len(obs))
    patch_first = {}
    for i, o in enumerate(obs):
        if o["type"] == "patch":
            c = o.get("color")
            if c in patch_first:
                uf.union(patch_first[c], i)
            else:
                patch_first[c] = i
    for kind, dkey in (("_bwd", "_db"), ("_fwd", "_df")):
        groups = {}
        for i, o in enumerate(obs):
            if o["type"] == "patch" or o[kind] is None:
                continue
            groups.setdefault((o["type"], o[kind]), []).append(i)
        for key, members in groups.items():
            ref = members[0]
            for m in members[1:]:
                if abs(obs[m][dkey] - obs[ref][dkey]) <= tol(obs[ref][dkey]):
                    uf.union(ref, m)
                else:
                    warnings.append(
                        u"같은 맥락(%s)인데 거리가 달라 다른 노드로 둠: %.0fmm vs %.0fmm "
                        u"(검출 누락 의심, seq %s/%s)" % (
                            kind[1:], obs[ref][dkey], obs[m][dkey],
                            obs[ref].get("seq"), obs[m].get("seq")))

    # 클러스터 이름: 패치는 색, 나머지는 처음 발견 순서대로 S1, M1 ...
    root_name = {}
    counters = {"split": 0, "merge": 0}
    for i, o in enumerate(obs):
        r = uf.find(i)
        if r in root_name:
            continue
        if o["type"] == "patch":
            root_name[r] = "P_" + o["color"]
        else:
            counters[o["type"]] += 1
            root_name[r] = ("S" if o["type"] == "split" else "M") + str(counters[o["type"]])
    for i, o in enumerate(obs):
        o["_node"] = root_name[uf.find(i)]

    # --- 노드 ---
    nodes = {}
    for o in obs:
        nid = o["_node"]
        n = nodes.get(nid)
        if n is None:
            n = {"id": nid, "type": o["type"], "pos": None, "visits": 0}
            if o["type"] == "patch":
                n["color"] = o["color"]
            if o["type"] == "split":
                n["branches"] = {}
            nodes[nid] = n
        n["visits"] += 1

    # --- 엣지 ---
    edges = {}
    order = []
    for idx in runs_idx:
        for a, b in zip(idx[:-1], idx[1:]):
            oa, ob = obs[a], obs[b]
            branch = oa.get("choice") if oa["type"] == "split" else None
            key = (oa["_node"], ob["_node"], branch)
            if key not in edges:
                edges[key] = []
                order.append(key)
            edges[key].append(ob["_d"] - oa["_d"])

    # 검출 누락으로 생긴 지름길 엣지 제거:
    # a->b 엣지가 드물게(<= 지름길 판정 횟수) 나왔고, a->x->b 두 단계 경로가 길이 10% 안으로
    # 존재하며 그 경로가 더 자주 지나간 경로라면 중간 노드를 놓친 것으로 본다.
    dropped = []
    def _avg(k):
        return sum(edges[k]) / float(len(edges[k]))
    for key in list(order):
        a, b, br = key
        n_ab = len(edges[key])
        best = None
        for k1 in order:
            if k1 == key or k1[0] != a or k1[1] == b:
                continue
            for k2 in order:
                if k2 == key or k2[0] != k1[1] or k2[1] != b:
                    continue
                total = _avg(k1) + _avg(k2)
                if abs(total - _avg(key)) <= 0.10 * total and min(len(edges[k1]), len(edges[k2])) > n_ab:
                    best = (k1, k2)
        if best:
            dropped.append({"from": a, "to": b, "branch": br, "length_mm": round(_avg(key), 1),
                            "traversals": n_ab, "via": best[0][1]})
            warnings.append(u"%s→%s 엣지(%d회)는 %s 검출 누락으로 보고 제외함" % (a, b, n_ab, best[0][1]))
            order.remove(key)

    edge_list = []
    for k, key in enumerate(order):
        frm, to, branch = key
        m, s = _mean_std(edges[key])
        e = {"id": "E%d" % (k + 1), "from": frm, "to": to, "branch": branch,
             "length_mm": round(m, 1), "length_std_mm": round(s, 1),
             "length_src": "odo" if length_src == {"odo"} else "time",
             "traversals": len(edges[key]), "shape": None,
             "bidirectional": False, "lanes": 1}
        edge_list.append(e)
        if branch and frm in nodes and nodes[frm]["type"] == "split":
            nodes[frm]["branches"][branch] = e["id"]
        if m and s > max(50.0, 0.15 * m) and len(edges[key]) > 1:
            warnings.append(u"%s(%s→%s) 길이 편차가 큼: %.0f±%.0fmm" % (e["id"], frm, to, m, s))

    # --- 구조 점검 ---
    out_deg = {}
    for e in edge_list:
        out_deg.setdefault(e["from"], set()).add((e["to"], e["branch"]))
    unexplored = []
    for nid, n in nodes.items():
        outs = out_deg.get(nid, set())
        if n["type"] == "split":
            for b in ("left", "right"):
                if b not in n["branches"]:
                    unexplored.append({"node": nid, "branch": b})
        elif len(outs) > 1:
            warnings.append(u"%s(%s)에서 나가는 길이 %d개 - 분기 검출 누락 의심: %s" % (
                nid, n["type"], len(outs), ", ".join(sorted(t for t, _ in outs))))

    if origin_node is None:
        origin_node = next((o["_node"] for o in obs if o["type"] == "patch"), None)

    node_list = sorted(nodes.values(), key=lambda n: (_TYPE_ORDER[n["type"]], n["id"]))
    return {
        "map_id": map_id,
        "version": version,
        "schema": SCHEMA,
        "unit": "mm",
        "created_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "origin_node": origin_node,
        "nodes": node_list,
        "edges": edge_list,
        "explore": {"complete": not unexplored, "unexplored": unexplored},
        "warnings": warnings,
        "debug": {
            "dropped_edges": dropped,
            "runs": sorted(set(o["_run"] for o in obs)),
            "events": [{"run": o["_run"], "seg": o["_seg"], "seq": o.get("seq"),
                        "ts": o.get("ts"), "type": o["type"], "d_mm": round(o["_d"], 1),
                        "detail": o.get("color") or o.get("choice") or o.get("from_side"),
                        "node": o["_node"]} for o in obs],
        },
    }
