# -*- coding: utf-8 -*-
"""온라인 탐색 정책: 갈림길에서 안 가 본 쪽을 고르고, 다 돌았으면 끝낸다.

맵 빌더와 같은 "뒤쪽 서명"(직전 패치 + 그 뒤 토큰)으로 지금 갈림길이 어디인지 판단한다.
종료 조건: 아는 모든 갈림길의 양쪽을 다 가 봤고, 마지막 새 발견 이후 같은 패치로
한 바퀴 돌아왔을 때. 이때 그 패치에서 멈춘다.

Python 3.6 호환, 표준 라이브러리만 사용.
"""

BRANCHES = ("left", "right")


class Explorer(object):
    def __init__(self, branch_order=BRANCHES, max_laps=8):
        self.branch_order = tuple(branch_order)
        self.max_laps = max_laps
        self.last_patch = None
        self.tokens = []
        self.tried = {}            # split 서명 -> [가 본 쪽]
        self.seen = set()          # 노드 서명
        self.patch_odo = {}        # 색 -> 마지막 통과 odo
        self.patch_count = {}
        self.last_discovery_odo = 0.0
        self.origin = None
        self.done = False
        self.done_reason = None

    # --- 내부 ---
    def _discover(self, key, odo):
        if key not in self.seen:
            self.seen.add(key)
            self.last_discovery_odo = odo
            return True
        return False

    def _key(self):
        return (self.last_patch, tuple(self.tokens))

    def splits_complete(self):
        return all(len(set(v)) >= len(self.branch_order) for v in self.tried.values())

    # --- 이벤트 ---
    def on_patch(self, color, odo):
        if self.origin is None:
            self.origin = color
        new = self._discover(("P", color), odo)
        if (not new and self.splits_complete()
                and self.patch_odo.get(color, -1.0) >= self.last_discovery_odo):
            self.done = True
            self.done_reason = "explored"
        self.patch_count[color] = self.patch_count.get(color, 0) + 1
        if self.origin == color and self.patch_count[color] > self.max_laps:
            self.done = True
            self.done_reason = "max_laps"
        self.patch_odo[color] = odo
        self.last_patch = color
        self.tokens = []

    def choose_split(self, odo):
        """갈림길을 만났을 때 갈 쪽을 고른다. 아직 안 가 본 쪽 우선."""
        key = self._key()
        self._discover(("S",) + key, odo)
        tried = self.tried.setdefault(key, [])
        choice = None
        for b in self.branch_order:
            if b not in tried:
                choice = b
                self.last_discovery_odo = odo
                break
        if choice is None:
            choice = self.branch_order[0]
        return choice

    def on_split(self, choice, odo):
        """실제로 따라간 쪽을 기록한다(명령과 다를 수 있음)."""
        key = self._key()
        self._discover(("S",) + key, odo)
        tried = self.tried.setdefault(key, [])
        if choice in self.branch_order and choice not in tried:
            tried.append(choice)
            self.last_discovery_odo = odo
        self.tokens.append("S" + (choice or "?")[0].upper())

    def on_merge(self, odo):
        self._discover(("M",) + self._key(), odo)
        self.tokens.append("M")

    def on_gap(self):
        self.last_patch = None
        self.tokens = []

    def status(self):
        return {"origin": self.origin, "splits": {str(k): v for k, v in self.tried.items()},
                "done": self.done, "reason": self.done_reason}
