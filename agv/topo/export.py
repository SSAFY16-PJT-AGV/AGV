# -*- coding: utf-8 -*-
"""위상 맵(build_map 결과) -> 팀 공통 맵 JSON(agv-map-v1).

agv-map-v1은 디지털 트윈(feature/digital-twin)의 `warehouse_map.json` 형식을 뼈대로 한다.
트윈 로더(WarehouseMapLoader.cpp)가 읽는 필드는 그대로 두고, 탐색기가 아는 정보는 필드를 더해 싣는다.
트윈은 모르는 필드를 무시하므로 이 파일을 트윈의 `Content/Data/warehouse_map.json`에 그대로 넣으면 된다.

트윈이 반드시 읽는 필드(없으면 맵 전체를 안 읽음):
    nodes[]: id(문자열), x, y(숫자, 미터)
    edges[]: from, to(문자열), type(문자열), points(배열, 원소는 {x, y} 미터. 비어도 됨)

좌표의 출처는 `pos_source`에 적는다.
    "odometry"   주행 기록(trace.jsonl)으로 되살린 궤적에서 계산한 노드 좌표와 엣지 곡선(topo/shape.py).
                 축척은 패치 길이로 보정한 이동거리, 모양은 고리로 맞춘 회전량. 수 cm 오차가 있다.
    "estimated"  주행 기록이 없을 때. 엣지 길이만 맞춘 배치라 연결 관계만 믿을 수 있다.
좌표계: 원점 패치가 (0, 0), 원점 패치를 지나는 진행 방향이 +x(설계 문서 v0.3 4장).
+y는 +x의 오른쪽이다. 언리얼(왼손 좌표계, X 앞·Y 오른쪽·Z 위)과 같은 방향이라서, 트윈이 x, y를
그대로 X, Y로 써도 위에서 볼 때 매트와 같은 모양(거울상 아님)으로 보인다. 각도(yaw)도 언리얼처럼
위에서 볼 때 시계 방향이 +다. 거울상은 갈림길의 왼쪽/오른쪽 기록으로 바로잡는다.

표준 라이브러리만 쓴다(Jetson에서 바로 돌려도 됨). Python 3.6 호환.
"""
import math

SCHEMA_V1 = "agv-map-v1"
MIN_LAYOUT_MM = 60.0          # 아주 짧은 엣지도 노드가 겹치지 않게 배치할 최소 간격
SAME_SPOT_MM = 5.0            # 같은 자리 교차 노드끼리 배치 거리
DEFAULT_SPUR_MM = 150.0       # 들어가 보지 않아 길이를 모르는 스퍼의 그림용 길이
TWIN_NODE_TYPE = {"patch": "access", "split": "junction", "merge": "junction",
                  "cross": "junction", "tee": "junction"}


def _m(mm):
    return None if mm is None else round(mm / 1000.0, 4)


def estimate_layout(m, iters=600):
    """엣지 길이를 최대한 맞추는 2D 배치(응력 최소화). 반환: {노드 ID: (x_mm, y_mm)}."""
    ids = [n["id"] for n in m["nodes"]]
    n = len(ids)
    if n == 0:
        return {}
    idx = dict((k, i) for i, k in enumerate(ids))
    INF = 1e15
    D = [[0.0 if i == j else INF for j in range(n)] for i in range(n)]
    for e in m["edges"]:
        a, b = idx.get(e["from"]), idx.get(e["to"])
        if a is None or b is None or a == b:
            continue
        L = max(MIN_LAYOUT_MM, float(e.get("length_mm") or 300.0))
        if L < D[a][b]:
            D[a][b] = D[b][a] = L
    for nd in m["nodes"]:
        for o in nd.get("same_spot") or []:
            if o in idx:
                a, b = idx[nd["id"]], idx[o]
                D[a][b] = D[b][a] = SAME_SPOT_MM
    for k in range(n):
        Dk = D[k]
        for i in range(n):
            dik = D[i][k]
            if dik >= INF:
                continue
            Di = D[i]
            for j in range(n):
                v = dik + Dk[j]
                if v < Di[j]:
                    Di[j] = v
    # 색 패치는 곧은 라인 위에 놓는다는 가정: 패치의 앞 노드와 뒤 노드 사이를 거의 일직선 거리로 둔다.
    # 길이만으로는 고리 모양이 정해지지 않아(접히거나 꺾임) 이 가정으로 모양을 잡아 준다.
    for nd in m["nodes"]:
        if nd["type"] != "patch" or nd.get("crossing"):
            continue
        ins = [e for e in m["edges"] if e["to"] == nd["id"] and e["from"] in idx and e["from"] != nd["id"]]
        outs = [e for e in m["edges"] if e["from"] == nd["id"] and e["to"] in idx and e["to"] != nd["id"]]
        if len(ins) != 1 or len(outs) != 1 or ins[0]["from"] == outs[0]["to"]:
            continue
        a, b = idx[ins[0]["from"]], idx[outs[0]["to"]]
        L = 0.97 * (max(MIN_LAYOUT_MM, float(ins[0].get("length_mm") or 300.0))
                    + max(MIN_LAYOUT_MM, float(outs[0].get("length_mm") or 300.0)))
        D[a][b] = D[b][a] = L
    finite = [D[i][j] for i in range(n) for j in range(n) if D[i][j] < INF]
    maxd = max(finite) if finite else 1000.0
    for i in range(n):
        for j in range(n):
            if D[i][j] >= INF:
                D[i][j] = maxd * 1.2 + 500.0
    # 초기값: 원점부터 진행 순서대로 원 위에
    origin = m.get("origin_node") if m.get("origin_node") in idx else ids[0]
    order, seen, queue = [], set(), [origin]
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        order.append(cur)
        queue.extend(e["to"] for e in m["edges"] if e["from"] == cur and e["to"] not in seen)
    order += [k for k in ids if k not in seen]
    total = sum(max(MIN_LAYOUT_MM, float(e.get("length_mm") or 300.0)) for e in m["edges"])
    R = max(300.0, total / (2 * math.pi) * 0.7)
    X, Y = [0.0] * n, [0.0] * n
    for t, k in enumerate(order):
        a = -math.pi / 2 + 2 * math.pi * t / n
        X[idx[k]], Y[idx[k]] = R * math.cos(a), R * math.sin(a)
    W = [[0.0 if i == j else 1.0 / (D[i][j] * D[i][j]) for j in range(n)] for i in range(n)]
    for _ in range(iters):
        for i in range(n):
            nx = ny = ws = 0.0
            Wi, Di = W[i], D[i]
            xi, yi = X[i], Y[i]
            for j in range(n):
                if i == j:
                    continue
                dx, dy = xi - X[j], yi - Y[j]
                dist = math.sqrt(dx * dx + dy * dy) or 1e-6
                w = Wi[j]
                nx += w * (X[j] + Di[j] * dx / dist)
                ny += w * (Y[j] + Di[j] * dy / dist)
                ws += w
            X[i], Y[i] = nx / ws, ny / ws
    pos = dict((k, (X[idx[k]], Y[idx[k]])) for k in ids)
    return _orient(m, pos, origin)


def _handedness(m, pos):
    """갈림길마다 '왼쪽 가지가 진행 방향 왼쪽에 있는가'를 세어 (맞음 - 틀림)을 돌려준다."""
    score = 0
    edges = dict((e["id"], e) for e in m["edges"])
    for nd in m["nodes"]:
        br = nd.get("branches") or {}
        if nd["type"] != "split" or "left" not in br or "right" not in br:
            continue
        inc = [e for e in m["edges"] if e["to"] == nd["id"]]
        el, er = edges.get(br["left"]), edges.get(br["right"])
        if not inc or el is None or er is None or el["to"] == er["to"]:
            continue
        s = pos[nd["id"]]
        p = pos[inc[0]["from"]]
        dx, dy = s[0] - p[0], s[1] - p[1]
        lx, ly = pos[el["to"]][0] - s[0], pos[el["to"]][1] - s[1]
        rx, ry = pos[er["to"]][0] - s[0], pos[er["to"]][1] - s[1]
        # y가 위인 좌표에서 진행 방향 d의 왼쪽은 외적 d x v > 0
        cl = dx * ly - dy * lx
        cr = dx * ry - dy * rx
        score += 1 if cl > cr else -1
    return score


def _orient(m, pos, origin):
    """설계 문서 v0.3 4장 좌표계: 원점 패치가 (0, 0), 원점 패치를 지나는 진행 방향이 +x."""
    ox, oy = pos[origin]
    outs = [e["to"] for e in m["edges"] if e["from"] == origin and e["to"] in pos and e["to"] != origin]
    if outs:   # 원점에서 나가는 방향(다음 노드 쪽)
        dx, dy = pos[outs[0]][0] - ox, pos[outs[0]][1] - oy
    else:
        dx, dy = _travel_dir(m, origin, pos)
    ang = math.atan2(dy, dx)
    c, s = math.cos(-ang), math.sin(-ang)
    out = dict((k, ((p[0] - ox) * c - (p[1] - oy) * s, (p[0] - ox) * s + (p[1] - oy) * c)) for k, p in pos.items())
    # 거울상 바로잡기: 갈림길의 왼쪽 가지가 진행 방향 왼쪽에 오게(+x 방향은 그대로 두고 y를 뒤집음)
    if _handedness(m, out) < 0:
        out = dict((k, (p[0], -p[1])) for k, p in out.items())
    return out


def _travel_dir(m, nid, pos):
    """노드에서의 진행 방향(들어온 엣지와 나간 엣지 방향의 평균, 단위 벡터)."""
    p = pos[nid]
    vs = []
    for e in m["edges"]:
        if e["to"] == nid and e["from"] in pos and e["from"] != nid:
            q = pos[e["from"]]
            vs.append((p[0] - q[0], p[1] - q[1]))
        if e["from"] == nid and e["to"] in pos and e["to"] != nid:
            q = pos[e["to"]]
            vs.append((q[0] - p[0], q[1] - p[1]))
    sx = sy = 0.0
    for vx, vy in vs:
        L = math.hypot(vx, vy) or 1.0
        sx += vx / L
        sy += vy / L
    L = math.hypot(sx, sy)
    return (1.0, 0.0) if L < 1e-9 else (sx / L, sy / L)


def to_agv_map_v1(m, pos_mm=None, shape=None, one_way=False):
    """build_map 결과를 agv-map-v1 dict로.

    엣지는 기본으로 양방향(`bidirectional: true`)이다. 테이프 선은 어느 쪽으로든 따라갈 수 있고 차체는
    제자리에서 돌 수 있기 때문이다. `from` -> `to`는 탐색할 때 지나간 방향이고 `branch`(왼쪽/오른쪽)도
    그 방향 기준이다. one_way=True면 탐색한 방향으로만 다니는 맵으로 내보낸다.

    반대 방향 주행·운영용으로 더 내보내는 것:
      - 합류 노드 `inputs`: {"left": 엣지, "right": 엣지} 들어오는 두 선이 탐색 방향 기준 어느 쪽인지.
        반대로 달리면 이 합류가 갈림길이 되고 좌우가 뒤집힌다.
      - `uturn: true`: 제자리에서 돌아 방향을 바꿔도 되는 노드(패치 가운데 - 패치 위 교차 제외, 스퍼 끝).
      - `zones`: 대기 지점(waiting, 스퍼 끝마다 하나)과 같은 자리 교차 쌍(shared). 서버 구간 예약용.

    shape: topo.shape.estimate_shape 결과(노드 좌표·진행 방향·엣지 곡선). 있으면 그 좌표를 쓴다.
    pos_mm: 노드 좌표를 직접 줄 때. 둘 다 없으면 엣지 길이로 배치를 추정한다."""
    if shape is not None:
        pos_mm = shape["pos"]
    estimated = pos_mm is None
    pos = dict(pos_mm) if pos_mm is not None else estimate_layout(m)
    heading = (shape or {}).get("heading") or {}
    shapes = (shape or {}).get("shapes") or {}
    spur_end = (shape or {}).get("spur_end") or {}
    nodes_out, edges_out = [], []
    dest = []      # (스퍼가 달린 노드, 쪽, 길이mm 또는 None)
    for nd in m["nodes"]:
        o = {"id": nd["id"], "x": None, "y": None,
             "type": TWIN_NODE_TYPE.get(nd["type"], "junction"), "kind": nd["type"]}
        if nd.get("color"):
            o["color"] = nd["color"]
        o["visits"] = nd.get("visits")
        for k in ("crossing", "through", "same_spot", "side"):
            if nd.get(k) not in (None, False, []):
                o[k] = nd[k]
        if nd.get("branches"):
            o["branches"] = nd["branches"]
        if nd.get("inputs"):
            o["inputs"] = nd["inputs"]
        if nd["type"] == "patch" and not nd.get("crossing"):
            o["uturn"] = True
        sp = nd.get("spur")
        if sp:
            o["spur"] = {"side": sp.get("side"), "seen": sp.get("seen"), "length_m": _m(sp.get("length_mm")),
                         "node": "D_" + nd["id"]}
            dest.append((nd["id"], sp.get("side") or "left", sp.get("length_mm")))
        if nd["type"] == "tee":
            o["length_m"] = _m(nd.get("length_mm"))
            o["spur"] = {"side": nd.get("side"), "length_m": _m(nd.get("length_mm")), "node": "D_" + nd["id"]}
            dest.append((nd["id"], nd.get("side") or "left", nd.get("length_mm")))
        nodes_out.append(o)
    for e in m["edges"]:
        pts = shapes.get(e["id"]) or []
        edges_out.append({
            "id": e["id"], "from": e["from"], "to": e["to"], "type": "main",
            "points": [{"x": round(q[0] / 1000.0, 4), "y": round(-q[1] / 1000.0, 4) + 0.0} for q in pts[1:-1]],
            "branch": e.get("branch"), "length_m": _m(e.get("length_mm")),
            "length_std_m": _m(e.get("length_std_mm")), "length_src": e.get("length_src"),
            "traversals": e.get("traversals"), "bidirectional": not one_way, "lanes": e.get("lanes", 1)})
    # 스퍼 끝 = 트윈의 destination 노드(작업하는 로봇이 비켜서는 곳)
    for nid, side, L in dest:
        if nid not in pos:
            continue
        dx, dy = heading.get(nid) or _travel_dir(m, nid, pos)
        nx, ny = (-dy, dx) if side == "left" else (dy, -dx)
        length = L if L else DEFAULT_SPUR_MM
        p = pos[nid]
        did = "D_" + nid
        pos[did] = spur_end.get(nid) or (p[0] + nx * length, p[1] + ny * length)
        nodes_out.append({"id": did, "x": None, "y": None, "type": "destination", "kind": "spur_end",
                          "spur_of": nid, "length_estimated": not L, "uturn": True})
        edges_out.append({"id": "E_" + did, "from": nid, "to": did, "type": "destination", "points": [],
                          "branch": side, "length_m": _m(L), "length_std_m": None,
                          "length_src": "odo" if L else None, "traversals": None,
                          "bidirectional": True, "lanes": 1})
    # 미터. 원점 패치가 (0, 0). 내부(수학 좌표, y가 왼쪽)를 언리얼 방향(y가 오른쪽)으로 뒤집는다
    for o in nodes_out:
        p = pos.get(o["id"])
        if p is not None:
            o["x"], o["y"] = round(p[0] / 1000.0, 4), round(-p[1] / 1000.0, 4) + 0.0
        else:
            o["x"] = o["y"] = 0.0
    # 운영 구역: 스퍼 끝 = 대기 지점, 같은 자리 교차 쌍 = 공유 구역(선마다 노드가 따로라 노드 예약만으로는
    # 겹침을 모름)
    zones = [{"id": "Z_" + o["id"], "type": "waiting", "nodes": [o["id"]]}
             for o in nodes_out if o.get("kind") == "spur_end"]
    seen = set()
    for o in nodes_out:
        if o.get("kind") == "cross" and o.get("same_spot") and o["id"] not in seen:
            group = sorted(set([o["id"]] + list(o["same_spot"])))
            seen.update(group)
            zones.append({"id": "Z_" + "_".join(group), "type": "shared", "nodes": group})
    dbg = dict(m.get("debug") or {})
    if dbg.get("dropped_edges"):
        dbg["dropped_edges"] = [dict(d, length_m=_m(d.get("length_mm"))) for d in dbg["dropped_edges"]]
    out = {
        "map_id": m.get("map_id"),
        "version": m.get("version", 0),
        "schema": SCHEMA_V1,
        "coordinate_unit": "meter",
        "coordinate_frame": "x_forward_y_right",
        "created_at": m.get("created_at"),
        "origin_node": m.get("origin_node"),
        "pos_source": "estimated" if estimated else ("odometry" if shape is not None else "measured"),
        "one_way": bool(one_way),
        "nodes": nodes_out,
        "edges": edges_out,
        "zones": zones,
        "explore": m.get("explore"),
        "warnings": m.get("warnings", []),
    }
    if shape is not None:
        out["shape_fit"] = shape.get("fit")
    if m.get("debug") is not None:
        out["debug"] = dbg
    return out


def twin_problems(doc):
    """트윈 로더(WarehouseMapLoader::LoadMap)와 같은 검사. 문제 목록(비면 트윈이 읽을 수 있음)."""
    num = (int, float)
    probs = []
    if not isinstance(doc.get("nodes"), list):
        return ["nodes 배열 없음"]
    ids = set()
    for n in doc["nodes"]:
        if not isinstance(n, dict) or not isinstance(n.get("id"), str) \
                or not isinstance(n.get("x"), num) or isinstance(n.get("x"), bool) \
                or not isinstance(n.get("y"), num) or isinstance(n.get("y"), bool):
            probs.append("노드 필드(id, x, y) 이상: %r" % (n.get("id") if isinstance(n, dict) else n))
            continue
        ids.add(n["id"])
    if not isinstance(doc.get("edges"), list):
        return probs + ["edges 배열 없음"]
    for e in doc["edges"]:
        if not isinstance(e, dict) or not isinstance(e.get("from"), str) or not isinstance(e.get("to"), str):
            probs.append("엣지 from/to 이상: %r" % e)
            continue
        if not isinstance(e.get("type"), str):
            probs.append("엣지 type 없음: %s -> %s" % (e["from"], e["to"]))
        pts = e.get("points")
        if not isinstance(pts, list):
            probs.append("엣지 points 배열 없음: %s -> %s" % (e["from"], e["to"]))
        else:
            for p in pts:
                if not isinstance(p, dict) or not isinstance(p.get("x"), num) or not isinstance(p.get("y"), num):
                    probs.append("엣지 point 이상: %s -> %s" % (e["from"], e["to"]))
        if e["from"] not in ids or e["to"] not in ids:
            probs.append("엣지 끝 노드 없음(트윈은 이 엣지만 건너뜀): %s -> %s" % (e["from"], e["to"]))
    return probs
