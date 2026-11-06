# -*- coding: utf-8 -*-
"""온라인 탐색 정책: 갈림길에서 안 가 본 쪽을 고르고, 다 돌았으면 끝낸다.

맵 빌더와 같은 "뒤쪽 서명"(직전 패치 + 그 뒤 토큰)으로 지금 갈림길이 어디인지 판단한다.
종료 조건: 아는 모든 갈림길의 양쪽을 다 가 봤고, 마지막 새 발견 이후 같은 패치로
한 바퀴 돌아왔을 때. 이때 그 패치에서 멈춘다.

교차(X)·스퍼(T)도 서명 토큰으로 쓰되, 패치에서 absorb_mm 안에 있는 것은 패치의 일부로 보고
무시한다(맵 빌더와 같은 규칙). 패치 위 교차·스퍼는 패치보다 먼저 잡힐 수도 나중에 잡힐 수도 있어서다.

Python 3.6 호환, 표준 라이브러리만 사용.
"""

BRANCHES = ("left", "right")


class Explorer(object):
    def __init__(self, branch_order=BRANCHES, max_laps=8, absorb_mm=150.0):
        self.branch_order = tuple(branch_order)
        self.max_laps = max_laps
        self.absorb_mm = absorb_mm
        self.last_patch = None
        self.last_patch_pos = None
        self.tokens = []
        self.token_pos = []
        self._choose_key = None
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
        self._drop_absorbed(odo)
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
        self.last_patch_pos = odo
        self.tokens = []
        self.token_pos = []

    def choose_split(self, odo):
        """갈림길을 만났을 때 갈 쪽을 고른다. 아직 안 가 본 쪽 우선."""
        key = self._key()
        self._choose_key = key
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
        """실제로 따라간 쪽을 기록한다(명령과 다를 수 있음).
        고를 때 쓴 서명이 있으면 그것을 쓴다(그사이 패치 이벤트가 끼어도 같은 갈림길로 기록)."""
        key = self._choose_key if self._choose_key is not None else self._key()
        self._choose_key = None
        self._discover(("S",) + key, odo)
        tried = self.tried.setdefault(key, [])
        if choice in self.branch_order and choice not in tried:
            tried.append(choice)
            self.last_discovery_odo = odo
        self._add("S" + (choice or "?")[0].upper(), odo)

    def on_merge(self, odo):
        self._discover(("M",) + self._key(), odo)
        self._add("M", odo)

    def _near_patch(self, odo):
        return self.last_patch_pos is not None and odo - self.last_patch_pos < self.absorb_mm

    def on_cross(self, odo):
        if not self._near_patch(odo):
            self._add("X", odo)

    def on_tee(self, odo):
        if not self._near_patch(odo):
            self._add("T", odo)

    def _add(self, tok, odo):
        self.tokens.append(tok)
        self.token_pos.append(odo)

    def _drop_absorbed(self, odo):
        """패치 바로 앞의 교차·스퍼 토큰은 패치의 일부로 보고 지운다."""
        while self.tokens and self.tokens[-1] in ("X", "T") and odo - self.token_pos[-1] < self.absorb_mm:
            self.tokens.pop()
            self.token_pos.pop()

    def on_gap(self):
        self.last_patch = None
        self.last_patch_pos = None
        self.tokens = []
        self.token_pos = []

    def status(self):
        return {"origin": self.origin, "splits": {str(k): v for k, v in self.tried.items()},
                "done": self.done, "reason": self.done_reason}
