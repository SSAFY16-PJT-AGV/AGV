# -*- coding: utf-8 -*-
"""정지 이벤트(rear_clear)의 cleared_resources 계산 - 서버 브랜치 server/segments.py 규칙을 그대로 따름.

서버 제안(server/docs/segment_recovery_proposal.md)은 로봇이 해제 노드에 선 뒤 "후단이 벗어난 자원"을
서버 내부 자원 이름으로 보내라고 한다. 서버는 그 목록이 정확히 (지금 구간 자원 - 다음 구간 자원)과
같아야 받는다. 그래서 로봇이 서버의 구간 계산을 흉내 낸다.
  - 구간 = 경로에서 이전 해제 노드부터 이번 해제 노드까지.
  - 자원 = 구간의 노드 ("node", id), 이웃 노드 쌍 엣지 ("edge", 두 ID를 정렬한 JSON 문자열),
    구간 노드가 들어 있는 공유 구역 ("zone", 구역 ID).
  - 다음 구간 = 이번 해제 노드부터 경로상 다음 대기 노드까지(마지막이면 이번 해제 노드 하나).
대기 노드를 정하는 법이 서버와 같아야 한다: "zones"(맵 waiting 구역) 또는 "all"(모든 노드 - 지금 서버
주문 시연이 쓰는 가정). 이 계산은 로봇이 할 일이 아니라고 보고, 서버가 stopped_at으로 직접 계산하도록
바꾸자고 제안할 예정이다. 그때까지만 쓴다.

실제로 로봇 후단이 벗어났는지는 재지 않는다(해제 노드에 섰다 = 앞 구간을 다 지났다로 봄).
표준 라이브러리만 쓴다. Python 3.6 호환.
"""
import json


def edge_id(a, b):
    return json.dumps(sorted((a, b)), ensure_ascii=True, separators=(",", ":"))


class SegmentRules(object):
    def __init__(self, raw_map, waiting="zones"):
        if waiting not in ("zones", "all"):
            raise ValueError("waiting must be zones or all")
        self.waiting_mode = waiting
        self.all_nodes = set(n["id"] for n in raw_map.get("nodes", []))
        self.waiting = set()
        self.shared = []
        for z in raw_map.get("zones") or []:
            if z.get("type") == "waiting":
                if z.get("node") is not None:
                    self.waiting.add(z["node"])
                for n in z.get("nodes") or []:
                    self.waiting.add(n)
            elif z.get("type") == "shared":
                self.shared.append((z["id"], set(z.get("nodes") or [])))

    def is_waiting(self, node):
        return node in self.all_nodes if self.waiting_mode == "all" else node in self.waiting

    def resources(self, segment):
        res = set(("node", n) for n in segment)
        res.update(("edge", edge_id(a, b)) for a, b in zip(segment[:-1], segment[1:]))
        res.update(("zone", zid) for zid, members in self.shared if members.intersection(segment))
        return res

    def next_segment(self, nodes, start_i):
        """경로 nodes[start_i]에서 다음 대기 노드까지. 마지막 노드면 None."""
        if start_i >= len(nodes) - 1:
            return None
        for j in range(start_i + 1, len(nodes)):
            if self.is_waiting(nodes[j]):
                return nodes[start_i:j + 1]
        return None

    def cleared(self, nodes, seg_start_i, stop_i):
        """이번 구간에서 놓아줄 자원 목록(정렬된 [kind, id] 쌍)."""
        current = self.resources(nodes[seg_start_i:stop_i + 1])
        nxt = self.next_segment(nodes, stop_i)
        keep = self.resources(nxt) if nxt else set([("node", nodes[stop_i])])
        return [list(r) for r in sorted(current - keep)]
