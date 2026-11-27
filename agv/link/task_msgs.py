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
  서버 선반 모델(server/team_shelf_order_slots.py) 이름도 받는다: type pick/place(= load/unload),
  slot_id(전체 칸 번호 1~24 = 칸 마커 ID, shelf·slot 대신), product_type(= item).

로봇 -> 서버(새 토픽. 기존 event 토픽에는 다른 종류를 보내지 않는다):
  agv/{id}/task   작업 결과(QoS 1)  encode_result()
  agv/{id}/shelf  선반 관측(QoS 1)  encode_shelf()  작업 전·후 한 번씩
  agv/{id}/state  기존 상태 메시지에 carrying(들고 있는 물건)과 task(진행 중 작업·단계)를 채움  state_fields()

Python 3.6 호환, 표준 라이브러리만.
"""
from ..task.model import ACTIONS, Task

ACTION_TYPES = {"load": "load", "unload": "unload", "observe": "observe",   # 메시지 type -> 안쪽 action
                "pick": "load", "place": "unload"}                          # 서버 선반 모델 이름
NO_ACTION = ("none",)


class TaskRejected(ValueError):
    pass


def decode_action(action, nodes, navmap, slot_of=None):
    """주문의 action_at_end -> Task. 끝 동작이 없거나(none) 모르는 종류면 None.
    잘못된 선반 작업이면 TaskRejected. slot_of(전체 칸 번호) -> (선반, 칸)|None: slot_id를 받을 때."""
    if not isinstance(action, dict):
        return None
    typ = action.get("type")
    if typ in NO_ACTION or typ not in ACTION_TYPES:
        return None
    act = ACTION_TYPES[typ]
    last = nodes[-1]
    parent = navmap.spur_parent(last) if navmap.kind(last) == "spur_end" else None
    shelf = action.get("shelf", parent)
    slot = action.get("slot")
    if "slot_id" in action:
        sid = action["slot_id"]
        where = slot_of(sid) if (slot_of is not None and isinstance(sid, int) and not isinstance(sid, bool)) else None
        if where is None:
            raise TaskRejected("unknown slot_id %r" % (sid,))
        if (action.get("shelf") not in (None, where[0])) or (slot is not None and slot != where[1]):
            raise TaskRejected("slot_id %d is %s slot %d, not shelf/slot given" % (sid, where[0], where[1]))
        shelf, slot = where
    if not isinstance(shelf, str) or not shelf:
        raise TaskRejected("%s needs a shelf (or a route ending at a shelf spur end)" % typ)
    if parent != shelf:
        raise TaskRejected("%s at %s: route must end at that shelf's spur end (ends at %s)" % (typ, shelf, last))
    if act in ("load", "unload"):
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
            raise TaskRejected("%s needs slot (1, 2, ...)" % typ)
    else:
        slot = None
    item = action.get("item", action.get("product_type"))
    if "item" in action and "product_type" in action and action["item"] != action["product_type"]:
        raise TaskRejected("item and product_type differ")
    if item is not None and (not isinstance(item, str) or not item):
        raise TaskRejected("item must be a name")
    assert act in ACTIONS
    return Task(act, shelf, slot, item)


def _slots(view, slot_ids):
    """칸마다: slot(선반 안 1~4), slot_id(전체 칸 번호, 서버 선반 모델), state(unknown|empty|occupied),
    product_type(occupied일 때 물건, 아니면 null), item(물건|empty|unknown, 읽기 쉬운 한 칸 표현)."""
    out = []
    for k in sorted(view.slots):
        s = view.slots[k]
        e = {"slot": k, "slot_id": slot_ids[k - 1] if slot_ids and k <= len(slot_ids) else None,
             "state": "occupied" if s.has_item else s.state,
             "product_type": s.state if s.has_item else None, "item": s.state}
        if s.has_item and s.marker_id is not None:
            e["marker_id"] = s.marker_id
        out.append(e)
    return out


def shelf_body(view, slot_ids=None):
    """선반 관측 본문(결과 메시지 안에도 넣음). slot_ids: 이 선반 칸 1..n의 전체 칸 번호."""
    if view is None:
        return None
    return {"shelf": view.shelf, "seen": view.seen, "slots": _slots(view, slot_ids),
            "wrong_shelf": view.wrong_shelf}


def encode_shelf(view, phase, ctx):
    """선반 관측 메시지. phase: before(작업 전) | after(작업 후). ctx: session, event_id, ts, order_id, map_id."""
    msg = {"type": "shelf_state", "event_id": ctx["event_id"], "robot_session": ctx["session"],
           "ts": ctx["ts"], "map_id": ctx["map_id"], "order_id": ctx.get("order_id"), "phase": phase}
    msg.update(shelf_body(view, ctx.get("slot_ids")))
    return msg


def encode_result(outcome, ctx):
    """작업 결과 메시지."""
    t = outcome.task
    shelf_after = outcome.after if outcome.after is not None else outcome.before
    return {"type": "task_result", "event_id": ctx["event_id"], "robot_session": ctx["session"],
            "ts": ctx["ts"], "map_id": ctx["map_id"], "order_id": ctx.get("order_id"),
            "action": t.action, "shelf": t.shelf, "slot": t.slot,
            "slot_id": _slot_id(ctx.get("slot_ids"), t.slot), "item": t.item,
            "result": "done" if outcome.ok else "failed", "reason": outcome.reason,
            "carrying": outcome.carrying, "shelf_state": shelf_body(shelf_after, ctx.get("slot_ids"))}


def _slot_id(slot_ids, k):
    return slot_ids[k - 1] if (slot_ids and k is not None and 1 <= k <= len(slot_ids)) else None


def state_fields(executor):
    """상태 메시지에 넣을 칸: carrying, task."""
    if executor is None:
        return {"carrying": None, "task": None}
    return {"carrying": executor.carrying, "task": executor.info()}
