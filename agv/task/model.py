# -*- coding: utf-8 -*-
"""선반 작업의 안쪽 객체. 서버 메시지 형식과 상관없이 이 모양으로만 주고받는다.

Task       무엇을 할지: action(load 집기 | unload 놓기 | observe 보기만), shelf(선반 노드, 예: N01),
           slot(1~4, observe면 None), item(물건 종류, 없으면 아무거나 / 들고 있는 것)
SlotView   칸 하나를 본 결과: state(물건 종류 | "empty" | "unknown"), marker_id, center_mm(몸체 좌표,
           물건 가운데 또는 빈 칸에 놓을 자리), yaw_deg
ShelfView  선반 하나를 본 결과: seen(선반 칸 마커를 찾았나), slots{칸: SlotView}, pose(몸체 좌표 선반 자세)
TaskOutcome 작업 결과: ok, reason(실패 이유 코드), carrying(끝난 뒤 들고 있는 것), before/after(ShelfView)

실패 이유 코드(reason):
  bad_task         작업 내용이 잘못됨(칸 번호 범위 밖 등)
  shelf_not_seen   선반(그 선반의 칸 마커)을 못 찾음
  wrong_shelf      다른 선반의 마커만 보임
  slot_unknown     그 칸을 판단할 수 없음(가려짐, 마커 둘 등)
  slot_empty       집을 칸이 비어 있음
  wrong_item       집을 칸에 다른 물건이 있음
  slot_occupied    놓을 칸에 이미 물건이 있음
  already_carrying 집으려는데 이미 들고 있음
  not_carrying     놓으려는데 든 게 없음(또는 다른 물건을 들고 있음)
  arm_unavailable  팔 동작이 연결되지 않음
  arm_failed       팔이 실패를 알림(집기 놓침 등)
  verify_failed    동작 뒤 다시 봤더니 기대한 상태가 아님
  timeout          단계가 제한 시간을 넘김
"""
from collections import namedtuple

ACTIONS = ("load", "unload", "observe")

Task = namedtuple("Task", ["action", "shelf", "slot", "item"])


class SlotView(namedtuple("SlotView", ["slot", "state", "marker_id", "center_mm", "yaw_deg"])):
    __slots__ = ()

    @property
    def has_item(self):
        return self.state not in ("empty", "unknown")


class ShelfView(namedtuple("ShelfView", ["shelf", "seen", "slots", "pose", "wrong_shelf"])):
    """slots: {칸 번호: SlotView}. wrong_shelf: 다른 선반의 칸 마커가 보였으면 그 선반 ID."""
    __slots__ = ()

    def state_of(self, k):
        s = self.slots.get(k)
        return s.state if s is not None else "unknown"


TaskOutcome = namedtuple("TaskOutcome", ["task", "ok", "reason", "carrying", "before", "after"])
