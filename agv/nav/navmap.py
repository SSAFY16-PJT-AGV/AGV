# -*- coding: utf-8 -*-
"""이동용 맵: 맵 JSON을 읽어 "어느 주행 엣지에서 어느 주행 엣지로 갈 수 있는지"와 남은 비용을 계산한다.

두 형식을 다 읽는다.
    agv-map-v1 (탐색기 지금 출력)  노드 x, y(m), 엣지 length_m, bidirectional(예전 맵은 directed)
    통일 제안(docs/mapping/map-schema.md)  노드 pos(mm), 엣지 shape(mm)·length_mm, bidirectional,
                                          중간 노드 V_<엣지>(kind via)
이동에 쓰는 것은 노드 종류(kind)·색, 엣지 연결·길이·갈림길 가지(branches)·합류 입력(inputs),
패치 위 교차(crossing, through), 스퍼(spur), 유턴 가능(uturn)뿐이다. 좌표는 길이를 모를 때만 쓴다.

주행 엣지와 방향
    맵 엣지 E(from -> to)는 탐색할 때 지나간 방향(정방향)이다. bidirectional: true인 본선 엣지는 반대 방향
    주행 엣지 "E~r"(to -> from)도 만든다(allow_reverse=False면 안 만듦). 방향은 노드를 지나도 그대로 이어지고,
    바꾸려면 유턴 가능 노드(uturn: 패치 가운데·스퍼 끝)에서 제자리 회전한다(비용 UTURN_COST_MM).
    같은 노드도 지나가는 방향에 따라 다르게 보인다.
      - 갈림길(split)은 반대로 지나면 합류로, 합류(merge)는 반대로 지나면 갈림길로 보인다.
        반대 방향 갈림길의 가지는 합류의 inputs(탐색 방향 기준 좌우)를 좌우 뒤집은 것이다.
      - 패치 스퍼·스퍼 갈림(T)의 쪽은 반대 방향이면 뒤집힌다.
      - 패치 위 X자 교차의 through 쌍은 반대 방향이면 (rev(나간), rev(들어온))이다.
    스퍼 엣지(type destination)는 경로 계산에서 뺀다. 스퍼 끝 노드(D_*)로 가는 길은 "부모 노드(패치·스퍼
    갈림)까지 가서 옆으로 들어감"으로 다룬다(spur_parent).

표준 라이브러리만 쓴다. Python 3.6 호환.
"""
import heapq
import io
import json
import math

OBSERVED = ("patch", "split", "merge", "cross", "tee")   # 로봇이 이벤트로 알아보는 노드 종류
DEFAULT_EDGE_MM = 300.0
UTURN_COST_MM = 500.0      # 제자리 유턴 한 번의 비용(mm 환산, 180도 회전 시간)
OPP = {"left": "right", "right": "left"}
FWD, REV = "f", "r"


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
    def __init__(self, d, allow_reverse=True):
        self.raw = d
        self.map_id = d.get("map_id")
        self.version = d.get("version")
        self.nodes = {}
        for n in d.get("nodes", []):
            spur = n.get("spur") or None
            kind = _node_kind(n)
            uturn = n.get("uturn")
            if uturn is None:   # 예전 맵: 패치(교차 없는)와 스퍼 끝
                uturn = (kind == "patch" and not n.get("crossing")) or kind == "spur_end"
            self.nodes[n["id"]] = {
                "id": n["id"], "kind": kind, "color": n.get("color"),
                "branches": dict(n.get("branches") or {}),
                "inputs": dict(n.get("inputs") or {}),
                "crossing": bool(n.get("crossing")),
                "through": [tuple(t) for t in (n.get("through") or [])],
                "spur_side": (spur or {}).get("side") if isinstance(spur, dict) else None,
                "side": n.get("side"),           # 스퍼 갈림(tee) 노드의 스퍼 방향
                "uturn": bool(uturn),
                "pos": _pos_mm(n),
            }
        self.edges = {}
        self.out = dict((k, []) for k in self.nodes)
        self.inn = dict((k, []) for k in self.nodes)
        self.reversible = False
        for i, e in enumerate(d.get("edges", [])):
            eid = e.get("id") or "e%d" % i
            if e.get("from") not in self.nodes or e.get("to") not in self.nodes:
                continue
            spur = e.get("type") == "destination" or self.nodes[e["to"]]["kind"] == "spur_end"
            ed = {"id": eid, "from": e["from"], "to": e["to"], "branch": e.get("branch"),
                  "len": self._edge_len(e), "spur": spur, "dir": FWD, "base": eid, "rev": None}
            self.edges[eid] = ed
            if spur:
                continue
            self.out[e["from"]].append(eid)
            self.inn[e["to"]].append(eid)
            two_way = e.get("bidirectional") is True and e.get("directed") is not True
            if allow_reverse and two_way:
                rid = eid + "~r"
                self.edges[rid] = {"id": rid, "from": e["to"], "to": e["from"], "branch": None,
                                   "len": ed["len"], "spur": False, "dir": REV, "base": eid, "rev": eid}
                ed["rev"] = rid
                self.out[e["to"]].append(rid)
                self.inn[e["from"]].append(rid)
                self.reversible = True
        # 갈림길 branches가 없으면 엣지의 branch로 채운다
        for eid, ed in self.edges.items():
            n = self.nodes[ed["from"]]
            if n["kind"] == "split" and ed["dir"] == FWD and ed["branch"] in ("left", "right") \
                    and not ed["spur"]:
                n["branches"].setdefault(ed["branch"], eid)
        self.by_color = dict((n["color"], k) for k, n in self.nodes.items()
                             if n["kind"] == "patch" and n["color"])
        self._dist_cache = {}

    @classmethod
    def load(cls, path, **kw):
        with io.open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f), **kw)

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

    # ---------- 방향에 따라 보이는 모습 ----------
    def kind(self, node_id):
        return self.nodes[node_id]["kind"]

    def dir(self, eid):
        return self.edges[eid]["dir"]

    def rev(self, eid):
        return self.edges[eid]["rev"]

    def kind_seen(self, node_id, d=FWD):
        """d 방향으로 지날 때 보이는 종류(갈림길·합류는 반대 방향이면 뒤바뀜)."""
        k = self.nodes[node_id]["kind"]
        if d == REV and k in ("split", "merge"):
            return "merge" if k == "split" else "split"
        return k

    def branches_seen(self, node_id, d=FWD):
        """d 방향으로 지날 때 갈림길이면 {쪽: 주행 엣지}."""
        n = self.nodes[node_id]
        if d == FWD:
            return dict(n["branches"]) if n["kind"] == "split" else {}
        if n["kind"] != "merge":
            return {}
        out = {}
        for side, eid in n["inputs"].items():
            r = self.edges.get(eid, {}).get("rev")
            if r:
                out[OPP[side]] = r
        return out

    def spur_side_at(self, node_id, d=FWD):
        """이 노드에서 옆으로 빠지는 스퍼 방향(패치 스퍼, 스퍼 갈림 노드). d 방향 기준."""
        n = self.nodes[node_id]
        side = (n["side"] or n["spur_side"]) if n["kind"] == "tee" else n["spur_side"]
        if side and d == REV:
            return OPP[side]
        return side

    def uturn_ok(self, node_id):
        return self.nodes[node_id]["uturn"]

    # ---------- 연결 ----------
    def next_edges(self, eid):
        """주행 엣지 eid로 노드에 들어왔을 때 같은 방향으로 이어서 갈 수 있는 주행 엣지들.
        패치 위 X자 교차(crossing)는 들어온 선 그대로만 지나간다(through)."""
        e = self.edges[eid]
        node, d = e["to"], e["dir"]
        n = self.nodes[node]
        outs = [o for o in self.out[node] if self.edges[o]["dir"] == d]
        if n["crossing"] and n["through"]:
            if d == FWD:
                allowed = [o for (i, o) in n["through"] if i == eid]
            else:
                allowed = [self.edges[i]["rev"] for (i, o) in n["through"]
                           if i in self.edges and self.edges.get(o, {}).get("rev") == eid]
            if allowed:
                return [o for o in outs if o in allowed]
        return outs

    def edges_into(self, node_id, d=None):
        return [e for e in self.inn[node_id] if d is None or self.edges[e]["dir"] == d]

    def edges_out_of(self, node_id, d=None):
        return [e for e in self.out[node_id] if d is None or self.edges[e]["dir"] == d]

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

    # ---------- 남은 비용 ----------
    def _succ(self, e, ok):
        """(다음 주행 엣지, 그 엣지로 넘어가는 비용) - 같은 방향으로 잇기 + 유턴."""
        out = [(f, self.edges[f]["len"]) for f in self.next_edges(e) if ok(f)]
        r = self.edges[e]["rev"]
        if r is not None and ok(r) and self.uturn_ok(self.edges[e]["to"]):
            out.append((r, UTURN_COST_MM + self.edges[r]["len"]))
        return out

    def dist_to(self, goal, allowed=None):
        """{주행 엣지 ID: 그 엣지 끝(to 노드)에서 goal까지 남은 비용(mm, 유턴 포함)}. 못 가는 엣지는 없음.
        allowed(주행 엣지 ID 집합)를 주면 그 엣지만 쓴다(정해진 경로 따라가기)."""
        key = (goal, None if allowed is None else frozenset(allowed))
        if key in self._dist_cache:
            return self._dist_cache[key]
        ok = (lambda e: True) if allowed is None else (lambda e: e in allowed)
        pred = {}
        for e in self.main_edges():
            if not ok(e):
                continue
            for f, c in self._succ(e, ok):
                pred.setdefault(f, []).append((e, c))
        R = {}
        heap = []
        for e in self.main_edges():
            if ok(e) and self.edges[e]["to"] == goal:
                R[e] = 0.0
                heapq.heappush(heap, (0.0, e))
        while heap:
            r, f = heapq.heappop(heap)
            if r > R.get(f, 1e18):
                continue
            for p, c in pred.get(f, []):
                v = r + c
                if v < R.get(p, 1e18):
                    R[p] = v
                    heapq.heappush(heap, (v, p))
        self._dist_cache[key] = R
        return R

    def best_next(self, e, goal, allowed=None):
        """엣지 e 끝 노드에서 goal로 가는 가장 싼 다음 걸음: (다음 주행 엣지, 유턴인가, 비용). 없으면 None."""
        R = self.dist_to(goal, allowed)
        ok = (lambda x: True) if allowed is None else (lambda x: x in allowed)
        best = None
        for f, c in self._succ(e, ok):
            if f not in R:
                continue
            v = c + R[f]
            if best is None or v < best[2] - 1e-9:
                best = (f, f == self.edges[e]["rev"], v)
        return best

    def route_edges(self, nodes):
        """노드 목록(서버 주문의 nodes)을 잇는 주행 엣지 집합. 이어지지 않거나, 유턴 못 하는 노드에서
        되돌아가는 경로면 ValueError."""
        allowed = set()
        for i, (a, b) in enumerate(zip(nodes[:-1], nodes[1:])):
            if self.spur_parent(b) == a or self.spur_parent(a) == b:
                continue            # 스퍼로 들어가기·나오기(경로 계산과 따로 다룸)
            es = [e for e in self.out.get(a, []) if self.edges[e]["to"] == b]
            if not es:
                raise ValueError(u"경로가 이어지지 않아요: %s -> %s" % (a, b))
            allowed.update(es)
            if i + 2 < len(nodes) and nodes[i + 2] == a and not self.uturn_ok(b):
                raise ValueError(u"%s에서는 유턴할 수 없어요(패치 가운데·스퍼 끝만)" % b)
        return allowed

    def shortest_path(self, start_edge, goal):
        """start_edge 끝에서 goal까지 가장 싼 노드 목록(시작 엣지의 to 노드부터, 유턴하면 노드가 되풀이됨)."""
        R = self.dist_to(goal)
        if start_edge not in R:
            return None
        path = [self.edges[start_edge]["to"]]
        e = start_edge
        guard = 0
        while self.edges[e]["to"] != goal and guard < 1000:
            guard += 1
            e = self.best_next(e, goal)[0]
            path.append(self.edges[e]["to"])
        return path
