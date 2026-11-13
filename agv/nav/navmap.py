# -*- coding: utf-8 -*-
"""이동용 맵: 맵 JSON을 읽어 "어느 엣지에서 어느 엣지로 갈 수 있는지"와 남은 거리를 계산한다.

두 형식을 다 읽는다.
    agv-map-v1 (탐색기 지금 출력)  노드 x, y(m), 엣지 length_m, directed
    통일 제안(docs/mapping/map-schema.md)  노드 pos(mm), 엣지 shape(mm)·length_mm, bidirectional,
                                          중간 노드 V_<엣지>(kind via)
이동에 쓰는 것은 노드 종류(kind)·색, 엣지 연결·길이·갈림길 방향(branch, branches)과
패치 위 교차(crossing, through), 스퍼(spur)뿐이다. 좌표는 길이를 모를 때만 쓴다.

길은 한쪽으로만 돈다(라인 엣지). 스퍼 엣지(type destination)는 경로 계산에서 뺀다. 스퍼 끝 노드(D_*)로
가는 길은 "부모 노드(패치·스퍼 갈림)까지 가서 옆으로 들어감"으로 다룬다(spur_parent).

표준 라이브러리만 쓴다. Python 3.6 호환.
"""
import heapq
import io
import json
import math

OBSERVED = ("patch", "split", "merge", "cross", "tee")   # 로봇이 이벤트로 알아보는 노드 종류
DEFAULT_EDGE_MM = 300.0


def _node_kind(n):
    k = n.get("kind")
    if k:
        return k
    t = n.get("type")
    if t in OBSERVED:                  # 예전 topo-v0
        return t
    if t == "destination":
        return "spur_end"
    if t == "access":
        return "patch"
    return t or "unknown"


def _pos_mm(n):
    if n.get("pos") is not None:
        return float(n["pos"][0]), float(n["pos"][1])
    if n.get("x") is not None and n.get("y") is not None:
        return float(n["x"]) * 1000.0, float(n["y"]) * 1000.0
    return None


class NavMap(object):
    def __init__(self, d):
        self.raw = d
        self.map_id = d.get("map_id")
        self.version = d.get("version")
        self.nodes = {}
        for n in d.get("nodes", []):
            spur = n.get("spur") or None
            self.nodes[n["id"]] = {
                "id": n["id"], "kind": _node_kind(n), "color": n.get("color"),
                "branches": dict(n.get("branches") or {}),
                "crossing": bool(n.get("crossing")),
                "through": [tuple(t) for t in (n.get("through") or [])],
                "spur_side": (spur or {}).get("side") if isinstance(spur, dict) else None,
                "side": n.get("side"),           # 스퍼 갈림(tee) 노드의 스퍼 방향
                "pos": _pos_mm(n),
            }
        self.edges = {}
        self.out = dict((k, []) for k in self.nodes)
        self.inn = dict((k, []) for k in self.nodes)
        for i, e in enumerate(d.get("edges", [])):
            eid = e.get("id") or "e%d" % i
            if e.get("from") not in self.nodes or e.get("to") not in self.nodes:
                continue
            spur = e.get("type") == "destination" or self.nodes[e["to"]]["kind"] == "spur_end"
            ed = {"id": eid, "from": e["from"], "to": e["to"], "branch": e.get("branch"),
                  "len": self._edge_len(e), "spur": spur}
            self.edges[eid] = ed
            if spur:
                continue
            self.out[e["from"]].append(eid)
            self.inn[e["to"]].append(eid)
            # 라인은 한쪽으로만 돈다. bidirectional: true인 라인 엣지만 반대로도 넣는다
            if e.get("bidirectional") is True and e.get("directed") is not True:
                rid = eid + "~r"
                self.edges[rid] = {"id": rid, "from": e["to"], "to": e["from"], "branch": None,
                                   "len": ed["len"], "spur": False}
                self.out[e["to"]].append(rid)
                self.inn[e["from"]].append(rid)
        # 갈림길 branches가 없으면 엣지의 branch로 채운다
        for eid, ed in self.edges.items():
            n = self.nodes[ed["from"]]
            if n["kind"] == "split" and ed["branch"] in ("left", "right") and not ed["spur"]:
                n["branches"].setdefault(ed["branch"], eid)
        self.by_color = dict((n["color"], k) for k, n in self.nodes.items()
                             if n["kind"] == "patch" and n["color"])
        self._dist_cache = {}

    @classmethod
    def load(cls, path):
        with io.open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    def _edge_len(self, e):
        if e.get("length_mm") is not None:
            return float(e["length_mm"])
        if e.get("length_m") is not None:
            return float(e["length_m"]) * 1000.0
        sh = e.get("shape")
        if sh and len(sh) >= 2:
            return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(sh[:-1], sh[1:]))
        a, b = self.nodes[e["from"]]["pos"], self.nodes[e["to"]]["pos"]
        if a and b:
            return max(1.0, math.hypot(b[0] - a[0], b[1] - a[1]))
        return DEFAULT_EDGE_MM

    # ---------- 연결 ----------
    def kind(self, node_id):
        return self.nodes[node_id]["kind"]

    def next_edges(self, eid):
        """엣지 eid로 노드에 들어왔을 때 이어서 갈 수 있는 엣지들.
        패치 위 X자 교차(crossing)는 들어온 선 그대로만 지나간다(through)."""
        node = self.edges[eid]["to"]
        n = self.nodes[node]
        outs = self.out[node]
        if n["crossing"] and n["through"]:
            allowed = [o for (i, o) in n["through"] if i == eid]
            if allowed:
                return [o for o in outs if o in allowed]
        return list(outs)

    def main_edges(self):
        return [k for k, e in self.edges.items() if not e["spur"]]

    def spur_parent(self, node_id):
        """스퍼 끝 노드의 부모(스퍼가 갈라지는 패치·스퍼 갈림 노드). 스퍼 끝이 아니면 None."""
        if node_id not in self.nodes or self.nodes[node_id]["kind"] != "spur_end":
            return None
        for e in self.edges.values():
            if e["spur"] and e["to"] == node_id:
                return e["from"]
        return None

    def spur_length(self, node_id):
        for e in self.edges.values():
            if e["spur"] and e["to"] == node_id:
                return e["len"]
        return None

    def spur_side_at(self, node_id):
        """이 노드에서 옆으로 빠지는 스퍼 방향(패치 스퍼, 스퍼 갈림 노드)."""
        n = self.nodes[node_id]
        if n["kind"] == "tee":
            return n["side"] or n["spur_side"]
        return n["spur_side"]

    # ---------- 남은 거리 ----------
    def dist_to(self, goal, allowed=None):
        """{엣지 ID: 그 엣지 끝(to 노드)에서 goal까지 남은 거리(mm)}. 못 가는 엣지는 없음.
        allowed(엣지 ID 집합)를 주면 그 엣지만 쓴다(정해진 경로 따라가기)."""
        key = (goal, None if allowed is None else frozenset(allowed))
        if key in self._dist_cache:
            return self._dist_cache[key]
        ok = (lambda e: True) if allowed is None else (lambda e: e in allowed)
        pred = {}
        for e in self.main_edges():
            if not ok(e):
                continue
            for f in self.next_edges(e):
                if ok(f):
                    pred.setdefault(f, []).append(e)
        R = {}
        heap = []
        for e in self.main_edges():
            if ok(e) and self.edges[e]["to"] == goal:
                R[e] = 0.0
                heapq.heappush(heap, (0.0, e))
        while heap:
            r, e = heapq.heappop(heap)
            if r > R.get(e, 1e18):
                continue
            for p in pred.get(e, []):
                c = r + self.edges[e]["len"]
                if c < R.get(p, 1e18):
                    R[p] = c
                    heapq.heappush(heap, (c, p))
        self._dist_cache[key] = R
        return R

    def route_edges(self, nodes):
        """노드 목록(서버 주문의 nodes)을 잇는 엣지 집합. 이어지지 않는 쌍이 있으면 ValueError."""
        allowed = set()
        for a, b in zip(nodes[:-1], nodes[1:]):
            if self.spur_parent(b) == a or self.spur_parent(a) == b:
                continue            # 스퍼로 들어가기·나오기(경로 계산과 따로 다룸)
            es = [e for e in self.out.get(a, []) if self.edges[e]["to"] == b]
            if not es:
                raise ValueError(u"경로가 이어지지 않아요: %s -> %s" % (a, b))
            allowed.update(es)
        return allowed

    def shortest_path(self, start_edge, goal):
        """start_edge 끝에서 goal까지 최단 노드 목록(시작 엣지의 to 노드부터)."""
        R = self.dist_to(goal)
        if start_edge not in R:
            return None
        path = [self.edges[start_edge]["to"]]
        e = start_edge
        guard = 0
        while self.edges[e]["to"] != goal and guard < 1000:
            guard += 1
            e = min((f for f in self.next_edges(e) if f in R),
                    key=lambda f: self.edges[f]["len"] + R[f])
            path.append(self.edges[e]["to"])
        return path
