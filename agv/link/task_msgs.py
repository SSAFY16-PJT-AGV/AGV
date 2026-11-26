# -*- coding: utf-8 -*-
"""선반 작업 메시지 <-> 안쪽 객체(agv.task.model) 변환. 메시지 필드 이름은 이 파일에만 있다.

서버와 형식이 달라지면 이 파일만 고친다(controller·실행기는 Task/ShelfView/TaskOutcome만 본다).
형식 설명: docs/protocol/shelf-task.md

주문의 끝 동작(action_at_end):
    {"type": "load",    "shelf": "N01", "slot": 2, "item": "milk"}   집기(item 없으면 아무 물건)
    {"type": "unload",  "shelf": "N03", "slot": 1, "item": "milk"}   놓기(item 없으면 들고 있는 것)
    {"type": "observe", "shelf": "N02"}                              보기만
    {"type": "none"}                                                 아무것도 안 함
  shelf가 없으면 경로 마지막 노드(스퍼 끝 D0k)의 부모 패치(N0k). 경로는 그 선반의 스퍼 끝에서 끝나야 한다.

로봇 -> 서버(새 토픽. 기존 event 토픽에는 다른 종류를 보내지 않는다):
  agv/{id}/task   작업 결과(QoS 1)  encode_result()
  agv/{id}/shelf  선반 관측(QoS 1)  encode_shelf()  작업 전·후 한 번씩
  agv/{id}/state  기존 상태 메시지에 carrying(들고 있는 물건)과 task(진행 중 작업·단계)를 채움  state_fields()

Python 3.6 호환, 표준 라이브러리만.
"""
from ..task.model import ACTIONS, Task

ACTION_TYPES = {"load": "load", "unload": "unload", "observe": "observe"}   # 메시지 type -> 안쪽 action
NO_ACTION = ("none",)


class TaskRejected(ValueError):
    pass


def decode_action(action, nodes, navmap):
    """주문의 action_at_end -> Task. 끝 동작이 없거나(none) 모르는 종류면 None.
    잘못된 선반 작업이면 TaskRejected."""
    if not isinstance(action, dict):
        return None
    typ = action.get("type")
    if typ in NO_ACTION or typ not in ACTION_TYPES:
        return None
    act = ACTION_TYPES[typ]
    last = nodes[-1]
    parent = navmap.spur_parent(last) if navmap.kind(last) == "spur_end" else None
    shelf = action.get("shelf", parent)
    if not isinstance(shelf, str) or not shelf:
        raise TaskRejected("%s needs a shelf (or a route ending at a shelf spur end)" % typ)
    if parent != shelf:
        raise TaskRejected("%s at %s: route must end at that shelf's spur end (ends at %s)" % (typ, shelf, last))
    slot = action.get("slot")
    if act in ("load", "unload"):
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
            raise TaskRejected("%s needs slot (1, 2, ...)" % typ)
    else:
        slot = None
    item = action.get("item")
    if item is not None and (not isinstance(item, str) or not item):
        raise TaskRejected("item must be a name")
    assert act in ACTIONS
    return Task(act, shelf, slot, item)


def _slots(view):
    out = []
    for k in sorted(view.slots):
        s = view.slots[k]
        e = {"slot": k, "state": s.state}
        if s.has_item and s.marker_id is not None:
            e["marker_id"] = s.marker_id
        out.append(e)
    return out


def shelf_body(view):
    """선반 관측 본문(결과 메시지 안에도 넣음)."""
    if view is None:
        return None
    return {"shelf": view.shelf, "seen": view.seen, "slots": _slots(view),
            "wrong_shelf": view.wrong_shelf}


def encode_shelf(view, phase, ctx):
    """선반 관측 메시지. phase: before(작업 전) | after(작업 후). ctx: session, event_id, ts, order_id, map_id."""
    msg = {"type": "shelf_state", "event_id": ctx["event_id"], "robot_session": ctx["session"],
           "ts": ctx["ts"], "map_id": ctx["map_id"], "order_id": ctx.get("order_id"), "phase": phase}
    msg.update(shelf_body(view))
    return msg


def encode_result(outcome, ctx):
    """작업 결과 메시지."""
    t = outcome.task
    shelf_after = outcome.after if outcome.after is not None else outcome.before
    return {"type": "task_result", "event_id": ctx["event_id"], "robot_session": ctx["session"],
            "ts": ctx["ts"], "map_id": ctx["map_id"], "order_id": ctx.get("order_id"),
            "action": t.action, "shelf": t.shelf, "slot": t.slot, "item": t.item,
            "result": "done" if outcome.ok else "failed", "reason": outcome.reason,
            "carrying": outcome.carrying, "shelf_state": shelf_body(shelf_after)}


def state_fields(executor):
    """상태 메시지에 넣을 칸: carrying, task."""
    if executor is None:
        return {"carrying": None, "task": None}
    return {"carrying": executor.carrying, "task": executor.info()}
