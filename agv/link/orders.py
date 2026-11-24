# -*- coding: utf-8 -*-
"""서버 주문(agv/{id}/order) 검사: 형식, 맵 버전, 중복, 순서.

주문 형식(설계 문서 3장)은 필드 여섯 개다.
    {"order_id": "T-0012", "seq": 3, "map_id": "layout_A@v4",
     "nodes": ["N3", "N4", "N7", "N9"], "released_until": "N7",
     "action_at_end": {"type": "unload", "shelf": "S2"}}

규칙(서버 브랜치의 가상 로봇 sim/order_inbox.py와 같은 뜻으로 맞춤)
  - 같은 (order_id, seq)를 같은 내용으로 다시 받으면 중복: 아무것도 다시 하지 않는다(QoS 1은 두 번 올 수 있음).
    같은 (order_id, seq)에 내용이 다르면 거절.
  - 새 작업(order_id가 처음)은 seq 1로 시작하고, 이전 작업이 끝났을 때(마지막 노드에 서 있음)만 받는다.
    첫 노드가 로봇이 서 있는(또는 놓인) 노드여야 하는지는 위치를 아는 쪽(controller)이 본다.
  - 같은 작업의 다음 주문은 seq가 1씩 늘고, nodes·action_at_end는 같고, released_until이 경로에서 더
    앞으로 가야 한다(해제를 늘리는 것만 됨). 주행 중에 받아도 된다(멈추지 않고 이어 감).
  - 끝난 작업이나 지난 seq는 거절(늦게 온 중복이 아닌 한).

기록은 프로세스 메모리에만 둔다. 로봇이 재시작하면 위치도 모르므로 서버와 다시 맞춰야 한다
(재시작 동기화 규약은 아직 없음). 표준 라이브러리만 쓴다. Python 3.6 호환.
"""
import hashlib
import json

ORDER_KEYS = frozenset(["order_id", "seq", "map_id", "nodes", "released_until", "action_at_end"])
MAX_ORDER_BYTES = 64 * 1024
MAX_ID_LEN = 128
MAX_NODES = 256


class OrderRejected(ValueError):
    """주문을 받을 수 없음. args[0]은 짧은 영문 이유(상태 메시지에 그대로 실음)."""


class Decision(object):
    """check() 결과. kind: new(새 작업) | extend(해제 늘림) | duplicate(이미 받음)."""

    def __init__(self, kind, order, digest):
        self.kind = kind
        self.order = order
        self.digest = digest

    def __repr__(self):
        return "Decision(%s, %s#%s -> %s)" % (self.kind, self.order["order_id"], self.order["seq"],
                                             self.order["released_until"])


def _text(v, name):
    if not isinstance(v, str) or not v.strip() or len(v) > MAX_ID_LEN:
        raise OrderRejected("invalid %s" % name)
    return v


def canonical(obj):
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse(payload):
    """bytes/str/dict -> 검사한 주문 dict와 digest(sha256 hex)."""
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_ORDER_BYTES:
            raise OrderRejected("order too large")
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise OrderRejected("order is not UTF-8")
    if isinstance(payload, str):
        try:
            order = json.loads(payload)
        except ValueError:
            raise OrderRejected("order is not JSON")
    else:
        order = payload
    if not isinstance(order, dict) or set(order) != ORDER_KEYS:
        raise OrderRejected("order must have exactly the fields %s" % ",".join(sorted(ORDER_KEYS)))
    _text(order["order_id"], "order_id")
    _text(order["map_id"], "map_id")
    seq = order["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise OrderRejected("invalid seq")
    nodes = order["nodes"]
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= MAX_NODES:
        raise OrderRejected("nodes must be a list of 2 or more node IDs")
    for n in nodes:
        _text(n, "node id")
    if any(a == b for a, b in zip(nodes[:-1], nodes[1:])):
        raise OrderRejected("nodes repeat back to back")
    if order["released_until"] not in nodes:
        raise OrderRejected("released_until is not in nodes")
    act = order["action_at_end"]
    if not isinstance(act, dict) or not isinstance(act.get("type"), str) or not act["type"].strip():
        raise OrderRejected("action_at_end needs a type")
    try:
        text = canonical(order)
    except (TypeError, ValueError):
        raise OrderRejected("order has values that are not plain JSON")
    return order, hashlib.sha256(text.encode("ascii")).hexdigest()


def release_index(nodes, release, after=0):
    """경로에서 release의 순번(after 다음에 처음 나오는 곳). 같은 노드를 두 번 지나는 경로도 다룬다."""
    for i in range(after, len(nodes)):
        if nodes[i] == release:
            return i
    return None


class OrderBook(object):
    def __init__(self, map_ref):
        """map_ref: 이 로봇이 쓰는 맵의 "map_id@v버전". 다른 맵 기준 주문은 거절한다."""
        self.map_ref = map_ref
        self.seen = {}              # (order_id, seq) -> digest
        self.done_ids = set()       # 끝난 작업
        self.current = None         # 지금 작업의 마지막으로 받은 주문
        self.current_digest = None
        self.release_i = None       # 지금 해제 노드의 경로 순번
        self.segment_start_i = None # 지금 해제 구간이 시작한 경로 순번(이전 해제 노드, 첫 구간이면 0)
        self.finished = True        # 지금 작업이 마지막 노드까지 끝났는지(처음엔 작업 없음 = 끝난 셈)

    def check(self, payload):
        """주문을 검사해 Decision을 돌려준다(아직 기록하지 않음). 안 되면 OrderRejected."""
        order, digest = parse(payload)
        if order["map_id"] != self.map_ref:
            raise OrderRejected("map mismatch (robot uses %s)" % self.map_ref)
        key = (order["order_id"], order["seq"])
        if key in self.seen:
            if self.seen[key] != digest:
                raise OrderRejected("order_id and seq reused with different content")
            return Decision("duplicate", order, digest)
        cur = self.current
        if cur is None or order["order_id"] != cur["order_id"]:
            if order["order_id"] in self.done_ids:
                raise OrderRejected("order already finished")
            if order["seq"] != 1:
                raise OrderRejected("a new order must start at seq 1")
            if not self.finished:
                raise OrderRejected("previous order %s is not finished" % cur["order_id"])
            i = release_index(order["nodes"], order["released_until"], 1)
            if i is None:
                raise OrderRejected("released_until must be past the first node")
            return Decision("new", order, digest)
        if order["seq"] != cur["seq"] + 1:
            raise OrderRejected("expected seq %d" % (cur["seq"] + 1))
        if order["nodes"] != cur["nodes"] or order["action_at_end"] != cur["action_at_end"]:
            raise OrderRejected("nodes and action_at_end must not change within an order")
        i = release_index(order["nodes"], order["released_until"], self.release_i + 1)
        if i is None:
            raise OrderRejected("released_until must move forward")
        return Decision("extend", order, digest)

    def commit(self, decision):
        """주행 쪽이 주문을 받아들인 뒤 기록한다."""
        o = decision.order
        if decision.kind == "duplicate":
            return
        if decision.kind == "new" and self.current is not None:
            self.done_ids.add(self.current["order_id"])
        after = 1 if decision.kind == "new" else self.release_i + 1
        self.segment_start_i = 0 if decision.kind == "new" else self.release_i
        self.release_i = release_index(o["nodes"], o["released_until"], after)
        self.seen[(o["order_id"], o["seq"])] = decision.digest
        self.current = o
        self.current_digest = decision.digest
        self.finished = False

    def mark_finished(self):
        """마지막 노드에 도착했다(작업 끝). 다음 새 작업을 받을 수 있다."""
        self.finished = True

    @property
    def at_final_release(self):
        return self.current is not None and self.release_i == len(self.current["nodes"]) - 1
