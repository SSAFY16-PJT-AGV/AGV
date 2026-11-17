# -*- coding: utf-8 -*-
"""agv-map-v1 맵(탐색기·매트 도안 출력)에 통일 형식(docs/mapping/map-schema.md) 필드를 채운다.

    python3 mapping/unify_map.py maps/mat_ssafy_v2.json            # 그 자리에 고쳐 씀
    python3 mapping/unify_map.py maps/a.json -o maps/a_unified.json

하는 일 (원래 필드 x, y, points, length_m 등은 그대로 둔다 - 트윈이 읽는 값)
  - 맵 단위 `unit: "mm"`, 노드 `pos`(mm), 엣지 `shape`(mm, 양 끝이 노드 pos와 정확히 같음)·`length_mm`
  - 같은 두 노드를 같은 방향으로 잇는 엣지(렌즈)가 둘 이상이면, 두 번째부터 가운데에 중간 노드
    `V_<엣지>`(kind via)를 넣어 엣지 둘로 나눈다(`E15` -> `E15` + `E15b`). 서버 주문은 노드 목록만
    보내서, 이게 없으면 어느 가지인지 알 수 없다(서버 MapGraph도 거부함).
    합류 노드의 `inputs`, 패치 교차의 `through`에 있던 원래 엣지 ID는 뒤쪽 반(`E15b`)으로 바꾼다.
  - `waiting` 구역에 노드가 하나면 `node`도 적는다(서버 구간 계획이 읽는 이름).

이미 통일 필드가 있는 맵에 다시 돌려도 같은 결과가 나온다. 표준 라이브러리만 쓴다. Python 3.6 호환.
나중에 design_map.py·build_map.py가 이 형식을 바로 내보내면 이 스크립트는 필요 없다.
"""
import argparse
import copy
import io
import json
import math


def _r(v):
    return round(float(v), 1)


def _node_pos(n):
    if n.get("pos") is not None:
        return [_r(n["pos"][0]), _r(n["pos"][1])]
    return [_r(n["x"] * 1000.0), _r(n["y"] * 1000.0)]


def _edge_shape(e, pos):
    """양 끝을 노드 pos로 고정한 mm 폴리라인."""
    a, b = pos[e["from"]], pos[e["to"]]
    mid = []
    if e.get("shape") and len(e["shape"]) >= 2:
        mid = [[_r(p[0]), _r(p[1])] for p in e["shape"][1:-1]]
    else:
        for p in e.get("points") or []:
            if isinstance(p, dict):
                mid.append([_r(p["x"] * 1000.0), _r(p["y"] * 1000.0)])
            else:
                mid.append([_r(p[0] * 1000.0), _r(p[1] * 1000.0)])
    out = [list(a)]
    for p in mid + [list(b)]:
        if p != out[-1]:
            out.append(p)
    if len(out) < 2:
        out.append(list(b))
    return out


def _poly_len(sh):
    return sum(math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(sh[:-1], sh[1:]))


def _split_shape(sh):
    """폴리라인을 길이 절반 지점에서 둘로 나눈다. (앞, 뒤, 가운데 점, 앞쪽 비율)."""
    total = _poly_len(sh)
    half = total / 2.0
    acc = 0.0
    for i, (p, q) in enumerate(zip(sh[:-1], sh[1:])):
        L = math.hypot(q[0] - p[0], q[1] - p[1])
        if acc + L >= half and L > 0:
            t = (half - acc) / L
            m = [_r(p[0] + (q[0] - p[0]) * t), _r(p[1] + (q[1] - p[1]) * t)]
            first = sh[:i + 1] + [m]
            second = [m] + sh[i + 1:]
            first = [pt for k, pt in enumerate(first) if k == 0 or pt != first[k - 1]]
            second = [pt for k, pt in enumerate(second) if k == 0 or pt != second[k - 1]]
            return first, second, m, 0.5
        acc += L
    return sh, sh, sh[-1], 0.5


def unify(d):
    d = copy.deepcopy(d)
    d["unit"] = "mm"
    pos = {}
    for n in d["nodes"]:
        n["pos"] = _node_pos(n)
        pos[n["id"]] = n["pos"]
    for e in d["edges"]:
        e["shape"] = _edge_shape(e, pos)
        if e.get("length_mm") is None:
            e["length_mm"] = _r(e["length_m"] * 1000.0 if e.get("length_m") is not None
                                else _poly_len(e["shape"]))
        e.setdefault("lanes", 1)

    # 같은 방향 평행 엣지 나누기(스퍼 엣지는 빼고)
    seen = {}
    new_edges = []
    renamed = {}            # 원래 ID -> 뒤쪽 반 ID (to 노드에서 이 엣지를 가리키던 참조용)
    by_id = dict((n["id"], n) for n in d["nodes"])
    for e in d["edges"]:
        key = (e["from"], e["to"])
        if e.get("type") == "destination":
            new_edges.append(e)
            continue
        if key not in seen and (e["to"], e["from"]) not in seen:
            seen[key] = e["id"]
            new_edges.append(e)
            continue
        vid = "V_" + e["id"]
        first, second, mid, frac = _split_shape(e["shape"])
        vnode = {"id": vid, "pos": mid, "x": round(mid[0] / 1000.0, 4), "y": round(mid[1] / 1000.0, 4),
                 "type": "junction", "kind": "via", "via_of": e["id"]}
        d["nodes"].append(vnode)
        by_id[vid] = vnode
        pos[vid] = mid
        L = e.get("length_mm") or _poly_len(e["shape"])
        a = dict(e)
        a.update({"to": vid, "shape": first, "length_mm": _r(L * frac), "points": _pts_m(first)})
        b = dict((k, v) for k, v in e.items() if k not in ("branch",))
        b.update({"id": e["id"] + "b", "from": vid, "shape": second, "length_mm": _r(L * (1 - frac)),
                  "points": _pts_m(second), "branch": None})
        for k in ("length_m",):
            if e.get(k) is not None:
                a[k] = round(e[k] * frac, 4)
                b[k] = round(e[k] * (1 - frac), 4)
        new_edges += [a, b]
        renamed[e["id"]] = (b["id"], e["to"])
    d["edges"] = new_edges
    for old, (new, to) in renamed.items():
        n = by_id[to]
        if n.get("inputs"):
            n["inputs"] = dict((s, new if v == old else v) for s, v in n["inputs"].items())
        if n.get("through"):
            n["through"] = [[new if t[0] == old else t[0], t[1]] for t in n["through"]]

    for z in d.get("zones") or []:
        if z.get("type") == "waiting" and z.get("node") is None and len(z.get("nodes") or []) == 1:
            z["node"] = z["nodes"][0]
    return d


def _pts_m(shape):
    """트윈이 읽는 points(m, 양 끝 노드 제외)."""
    return [{"x": round(p[0] / 1000.0, 4), "y": round(p[1] / 1000.0, 4)} for p in shape[1:-1]]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("map")
    ap.add_argument("-o", "--out", default=None, help="출력 파일(기본: 입력 파일에 덮어씀)")
    a = ap.parse_args(argv)
    with io.open(a.map, encoding="utf-8") as f:
        d = json.load(f)
    u = unify(d)
    vias = [n["id"] for n in u["nodes"] if n.get("kind") == "via"]
    with io.open(a.out or a.map, "w", encoding="utf-8") as f:
        f.write(json.dumps(u, ensure_ascii=False, indent=1))
    print(u"%s: 노드 %d, 엣지 %d, 중간 노드 %s" % (a.out or a.map, len(u["nodes"]), len(u["edges"]),
                                             ", ".join(vias) or u"없음"))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
