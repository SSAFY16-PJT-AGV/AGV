# -*- coding: utf-8 -*-
"""노드 이벤트 로그 -> 위상 맵 JSON (topo-v0).

노드 식별 방법
- 색 패치: 색이 곧 ID (P_yellow 등). 한 맵에서 같은 색 패치는 하나라고 가정한다.
  패치에서 absorb_mm 안의 X자 교차·스퍼(T자)는 패치의 속성(crossing, spur)으로 흡수한다.
- 분기(split)·합류(merge)·교차(cross)·스퍼(tee): 이름표가 없으므로 "주변 맥락"으로 같은 곳인지 판단한다.
    뒤쪽 서명 bwd = (직전 패치, 그 뒤로 지나온 토큰들)
    앞쪽 서명 fwd = (내 선택, 다음 패치까지 지날 토큰들, 다음 패치)
  같은 종류의 관측끼리 bwd가 같거나 fwd가 같으면 같은 노드 후보다(union-find).
  분기는 들어오는 길이 하나라 bwd로, 합류는 나가는 길이 하나라 fwd로 잘 묶인다.
  후보라도 "확정 경로"가 다르면 묶지 않는다. 한 지점에서 뒤로는 합류를 만나기 전까지,
  앞으로는 분기를 만나기 전까지 경로가 하나뿐이므로 같은 노드라면 그 구간 토큰이 같아야 한다.
  (예: 노랑 교차 뒤 합류가 두 개 - 하나는 빨강으로, 하나는 보라로 이어짐 - 를 구분)
  여기에 직전/다음 패치까지 거리가 비슷한지도 확인해서 검출 누락으로 인한 오결합을 막는다.
- 막다른 길(dead_end): 로봇이 갈림길에서 고른 쪽이 막혀 되돌아 나왔으면, dead_end가 가리키는
  갈림길(ref_seq)을 그쪽 스퍼(tee, 길이 spur_mm)로 바꾼다. 교차에서 막혔으면 그 자리에 스퍼를 더한다.
- 교차(X)를 지나는 길은 직진만 된다. 실제로 지나간 (들어온 엣지, 나간 엣지) 쌍을 노드의
  through에 남겨, 경로 계획이 교차에서 꺾지 않게 한다.

엣지 길이는 odo_mm 차이(없으면 시간 x 속도)의 평균이다. 실제 2D 형상은 아직 없다(shape: null).

Python 3.6 호환, 표준 라이브러리만 사용.
"""
import datetime
import math

from .events import split_runs

SCHEMA = "topo-v0"

# 거의 같은 자리(reorder_mm 안)에서 검출된 이벤트는 종류 순서로 고정한다: 합류 < 패치 < 교차 < 스퍼 < 분기.
# 합류는 두 선이 닿는 순간(실제 접점보다 앞)에, 분기는 떨어지는 순간(뒤)에 잡히므로
# 패치 위 합류·분기의 실제 순서와 같고, 바퀴마다 순서가 뒤바뀌는 것도 막는다.
DEFAULT_REORDER_MM = 60.0
# 패치 진입(앞 가장자리) 기준 [-ABSORB_BEFORE, +ABSORB] 안의 교차·스퍼는 패치 속성으로 흡수한다.
# 스퍼·교차는 보통 패치 가운데(앞 가장자리에서 패치 길이 절반, 약 70mm)에 있다.
DEFAULT_ABSORB_MM = 150.0
ABSORB_BEFORE_MM = 40.0
# 패치를 지나간 횟수 중 이 비율 이상에서 스퍼를 봤을 때만 패치에 스퍼가 있다고 본다.
# 비스듬히 들어오면 스퍼를 놓치는 일이 많아(시뮬레이션 기준 1/3 정도) 낮게 둔다.
SPUR_MIN_RATIO = 0.3
# 같은 노드로 묶을 때 허용하는 거리 차이: max(abs, rel * 거리)
DEFAULT_DIST_TOL_ABS = 200.0
DEFAULT_DIST_TOL_REL = 0.25
_TYPE_ORDER = {"merge": 0, "patch": 1, "cross": 2, "tee": 3, "split": 4}
_LIST_ORDER = {"patch": 0, "split": 1, "merge": 2, "cross": 3, "tee": 4}
_PREFIX = {"split": "S", "merge": "M", "cross": "X", "tee": "T"}


def _token(ev):
    t = ev["type"]
    if t == "split":
        return "S" + (ev.get("choice") or "?")[0].upper()
    if t == "merge":
        return "M"
    if t == "cross":
        return "X"
    if t == "tee":
        return "T" + (ev.get("side") or "?")[0].upper()
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


def _apply_dead_ends(events, warnings):
    """dead_end 이벤트로 갈림길을 스퍼로 바꾼다. 같은 로그 안에서 seq로 찾는다."""
    by_seq = dict((e.get("seq"), e) for e in events)
    out = []
    extra = []
    for e in events:
        if e.get("type") != "dead_end":
            continue
        ref = by_seq.get(e.get("ref_seq"))
        if ref is None:
            warnings.append(u"막다른 길(seq %s)이 가리키는 갈림길을 못 찾음" % e.get("seq"))
            continue
        if ref["type"] == "split":
            ref["type"] = "tee"
            ref["side"] = ref.get("choice") or e.get("side")
            ref["spur_mm"] = e.get("spur_mm")
        elif ref["type"] == "cross":
            t = dict(ref)
            t.update({"type": "tee", "side": e.get("side"), "spur_mm": e.get("spur_mm"),
                      "seq": None})
            extra.append(t)
    for e in events:
        if e.get("type") != "dead_end":
            out.append(e)
    # 교차에 붙은 스퍼는 교차 바로 뒤에 둔다
    for t in extra:
        i = next((k for k, e in enumerate(out) if e is not t and e.get("ts") == t.get("ts")
                  and e.get("type") == "cross"), len(out) - 1)
        out.insert(i + 1, t)
    return out


def _prepare_run(run_events, meta, reorder_mm, absorb_mm):
    """구간 이벤트에 거리 d(mm)를 붙이고, 패치 위 교차·스퍼를 흡수하고, 근접 이벤트 순서를 정리한다."""
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
    # 판정이 늦게 확정된 이벤트는 파일 순서와 위치 순서가 다를 수 있으므로 위치순 정렬
    obs.sort(key=lambda o: o["_d"])
    # 패치 위 교차·스퍼 흡수
    patches = [o for o in obs if o["type"] == "patch"]
    kept = []
    for o in obs:
        if o["type"] in ("cross", "tee") and patches:
            p = min(patches, key=lambda q: abs(q["_d"] - o["_d"]))
            if -ABSORB_BEFORE_MM <= o["_d"] - p["_d"] <= absorb_mm:
                if o["type"] == "cross":
                    p["_crossing"] = True
                else:
                    p.setdefault("_spur", []).append(o.get("side"))
                    if o.get("spur_mm") is not None:
                        p.setdefault("_spur_len", []).append(o["spur_mm"])
                continue
        kept.append(o)
    obs = kept
    # 근접 이벤트 정렬
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
    """구간 안 각 관측의 bwd/fwd 서명, 기준 패치까지 거리, 확정 경로 서명을 구한다."""
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
        # 확정 경로: 뒤로는 합류(포함)·패치까지, 앞으로는 분기(포함)·패치까지. 합류는 뒤가, 분기는 앞이 미정
        bdet = None
        if o["type"] != "merge":
            toks = []
            for j in range(i - 1, -1, -1):
                t = run[j]["type"]
                if t == "patch":
                    bdet = ("P_" + run[j]["color"], tuple(reversed(toks)))
                    break
                if t == "merge":
                    bdet = ("M", tuple(reversed(toks)))
                    break
                toks.append(_token(run[j]))
        fdet = None
        if o["type"] != "split":
            toks = []
            for j in range(i + 1, n):
                t = run[j]["type"]
                if t == "patch":
                    fdet = (tuple(toks), "P_" + run[j]["color"])
                    break
                if t == "split":
                    fdet = (tuple(toks), "S")
                    break
                toks.append(_token(run[j]))
        sig.append((bwd, d_b, fwd, d_f, bdet, fdet))
    return sig


def _conflict(a, b):
    """확정 경로 서명이 둘 다 있고 다르면 다른 지점이다."""
    return (a["_bdet"] is not None and b["_bdet"] is not None and a["_bdet"] != b["_bdet"]) or \
        (a["_fdet"] is not None and b["_fdet"] is not None and a["_fdet"] != b["_fdet"])


def build_map(event_lists, map_id="map", version=0, origin_node=None,
              reorder_mm=DEFAULT_REORDER_MM, tol_abs=DEFAULT_DIST_TOL_ABS,
              tol_rel=DEFAULT_DIST_TOL_REL, run_names=None, absorb_mm=DEFAULT_ABSORB_MM):
    """여러 주행 로그(각각 이벤트 리스트)를 합쳐 위상 맵 dict를 만든다."""
    warnings = []
    obs = []          # 전체 관측(평탄화)
    runs_idx = []     # 구간별 obs 인덱스 목록
    length_src = set()
    for li, events in enumerate(event_lists):
        name = run_names[li] if run_names else "run%d" % li
        events = _apply_dead_ends([dict(e) for e in events], warnings)
        for ri, (rev, meta) in enumerate(split_runs(events)):
            prepared, src = _prepare_run(rev, meta, reorder_mm, absorb_mm)
            length_src.add(src)
            sigs = _signatures(prepared)
            idx = []
            for o, s in zip(prepared, sigs):
                o["_run"] = name
                o["_seg"] = ri
                o["_bwd"], o["_db"], o["_fwd"], o["_df"], o["_bdet"], o["_fdet"] = s
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
                if _conflict(obs[ref], obs[m]):
                    continue
                if abs(obs[m][dkey] - obs[ref][dkey]) <= tol(obs[ref][dkey]):
                    uf.union(ref, m)
                else:
                    warnings.append(
                        u"같은 맥락(%s)인데 거리가 달라 다른 노드로 둠: %.0fmm vs %.0fmm "
                        u"(검출 누락 의심, seq %s/%s)" % (
                            kind[1:], obs[ref][dkey], obs[m][dkey],
                            obs[ref].get("seq"), obs[m].get("seq")))

    # 클러스터 이름: 패치는 색, 나머지는 처음 발견 순서대로 S1, M1, X1, T1 ...
    root_name = {}
    counters = dict((k, 0) for k in _PREFIX)
    for i, o in enumerate(obs):
        r = uf.find(i)
        if r in root_name:
            continue
        if o["type"] == "patch":
            root_name[r] = "P_" + o["color"]
        else:
            counters[o["type"]] += 1
            root_name[r] = _PREFIX[o["type"]] + str(counters[o["type"]])
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
                n["spur"] = None
                n["crossing"] = False
                n["_spur_votes"] = []
                n["_spur_len"] = []
            if o["type"] == "split":
                n["branches"] = {}
            if o["type"] in ("tee", "cross"):
                n["side"] = o.get("side") or o.get("from_side")
            if o["type"] == "tee":
                n["_len"] = []
            nodes[nid] = n
        n["visits"] += 1
        if o["type"] == "tee" and o.get("spur_mm") is not None:
            n["_len"].append(o["spur_mm"])
        if o["type"] == "patch":
            n["_spur_len"].extend(o.get("_spur_len", []))
            n["_spur_votes"].extend(o.get("_spur", []))
            n["crossing"] = n["crossing"] or bool(o.get("_crossing"))
    for n in nodes.values():
        if n["type"] == "tee":
            ls = n.pop("_len", [])
            n["length_mm"] = round(sum(ls) / len(ls), 1) if ls else None
        if n["type"] == "patch":
            ls = n.pop("_spur_len", [])
            votes = [v for v in n.pop("_spur_votes") if v]
            if votes:
                side = max(set(votes), key=votes.count)
                seen = votes.count(side)
                if seen >= SPUR_MIN_RATIO * n["visits"] or ls:
                    n["spur"] = {"side": side, "seen": seen,
                                 "length_mm": round(sum(ls) / len(ls), 1) if ls else None}
                else:
                    warnings.append(u"%s: 스퍼를 %d/%d회만 봐서 스퍼 없음으로 둠(오검출 의심)"
                                    % (n["id"], seen, n["visits"]))

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
            edges[key].append(max(0.0, ob["_d"] - oa["_d"]))

    # 검출 누락으로 생긴 지름길 엣지 제거:
    # a->x->b 두 단계 경로가 a->b와 길이 10% 안으로 같고 더 자주 지나간 경로라면(x가 스퍼·교차면
    # 같은 횟수여도) 중간 노드를 놓친 것으로 본다.
    dropped = []
    node_type = dict((nid, n["type"]) for nid, n in nodes.items())

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
                via_type = node_type.get(k1[1])
                # 스퍼·교차는 비스듬히 지나가면 놓치기 쉬워서, 횟수가 같아도 놓친 것으로 본다
                enough = (min(len(edges[k1]), len(edges[k2])) >= n_ab if via_type in ("tee", "cross")
                          else min(len(edges[k1]), len(edges[k2])) > n_ab)
                if abs(total - _avg(key)) <= 0.10 * total and enough:
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

    # --- 노드를 지나간 (들어온 엣지, 나간 엣지) 쌍 ---
    eid = dict(((e["from"], e["to"], e["branch"]), e["id"]) for e in edge_list)
    through = {}
    for idx in runs_idx:
        for a, b, c in zip(idx[:-2], idx[1:-1], idx[2:]):
            oa, ob, oc = obs[a], obs[b], obs[c]
            e_in = eid.get((oa["_node"], ob["_node"], oa.get("choice") if oa["type"] == "split" else None))
            e_out = eid.get((ob["_node"], oc["_node"], ob.get("choice") if ob["type"] == "split" else None))
            if e_in and e_out:
                through.setdefault(ob["_node"], set()).add((e_in, e_out))
    in_deg = {}
    for e in edge_list:
        in_deg.setdefault(e["to"], set()).add(e["id"])

    # --- 구조 점검 ---
    out_deg = {}
    for e in edge_list:
        out_deg.setdefault(e["from"], set()).add((e["to"], e["branch"]))
    unexplored = []
    for nid, n in nodes.items():
        outs = out_deg.get(nid, set())
        ins = in_deg.get(nid, set())
        if len(ins) > 1 and len(outs) > 1:
            # 여러 길이 지나는 지점(교차): 실제로 지나간 쌍만 직진으로 허용
            n["through"] = sorted([list(p) for p in through.get(nid, set())])
        if n["type"] == "split":
            for b in ("left", "right"):
                if b not in n["branches"]:
                    unexplored.append({"node": nid, "branch": b})
        elif len(outs) > 1 and not (n.get("crossing") or n["type"] == "cross"):
            warnings.append(u"%s(%s)에서 나가는 길이 %d개 - 분기 검출 누락 의심: %s" % (
                nid, n["type"], len(outs), ", ".join(sorted(t for t, _ in outs))))

    # 같은 지점의 교차(선마다 노드가 하나씩 생김)를 짝짓기: 뒤쪽 서명이 같고 거리가 비슷하면 같은 자리
    xs = {}
    for o in obs:
        if o["type"] == "cross" and o["_bwd"] is not None:
            xs.setdefault(o["_node"], (o["_bwd"], o["_db"]))
    xids = sorted(xs)
    for i, a in enumerate(xids):
        for b in xids[i + 1:]:
            (ba, da), (bb, db) = xs[a], xs[b]
            if ba == bb and abs(da - db) <= tol(da):
                nodes[a].setdefault("same_spot", []).append(b)
                nodes[b].setdefault("same_spot", []).append(a)

    if origin_node is None:
        origin_node = next((o["_node"] for o in obs if o["type"] == "patch"), None)

    node_list = sorted(nodes.values(), key=lambda n: (_LIST_ORDER[n["type"]], n["id"]))
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
                        "detail": o.get("color") or o.get("choice") or o.get("from_side") or o.get("side"),
                        "node": o["_node"]} for o in obs],
        },
    }
