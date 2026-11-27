# -*- coding: utf-8 -*-
"""관측(ShelfView)과 들고 있는 것으로 할 일을 정한다. 하드웨어·메시지와 상관없는 순수 함수."""
from .model import ShelfView, SlotView


def view_from_perception(res, shelf, layout):
    """perceive_shelf() 결과 dict -> ShelfView."""
    sh = res.get("shelf") or {}
    seen = bool(sh.get("ok")) and sh.get("shelf_id") == shelf
    wrong = None
    if not seen:
        others = set()
        for m in res.get("markers") or []:
            if m.get("kind") == "slot" and m.get("id") in layout.slot_marker:
                sid = layout.slot_marker[m["id"]][0]
                if sid != shelf:
                    others.add(sid)
        wrong = sorted(others)[0] if others else None
    slots = {}
    for s in res.get("slots") or []:
        k = s["slot"]
        slots[k] = SlotView(k, s["state"] if seen else "unknown", s.get("marker_id"),
                            s.get("center_mm"), s.get("yaw_deg"))
    for k in range(1, layout.n + 1):
        slots.setdefault(k, SlotView(k, "unknown", None, None, None))
    pose = None
    if seen:
        pose = {"x_mm": sh["x_mm"], "y_mm": sh["y_mm"], "z_mm": sh.get("z_mm"), "yaw_deg": sh["yaw_deg"]}
    return ShelfView(shelf, seen, slots, pose, wrong)


def merge_views(views):
    """여러 장을 본 결과를 합친다. 선반을 본 장들만 쓰고, 칸마다 판단한 장들이 모두 같으면 그 상태,
    엇갈리면 unknown. 위치는 판단한 장들의 평균."""
    if not views:
        return None
    shelf = views[0].shelf
    good = [v for v in views if v.seen]
    if not good:
        wrong = next((v.wrong_shelf for v in views if v.wrong_shelf), None)
        slots = dict((k, SlotView(k, "unknown", None, None, None)) for k in views[0].slots)
        return ShelfView(shelf, False, slots, None, wrong)
    slots = {}
    for k in sorted(good[0].slots):
        obs = [v.slots[k] for v in good if v.slots[k].state != "unknown"]
        states = set(s.state for s in obs)
        if len(states) != 1 or len(obs) * 2 < len(good):     # 엇갈림, 또는 절반 넘게 판단 못 함
            slots[k] = SlotView(k, "unknown", None, _mean([s.center_mm for s in good_slots(good, k)]), None)
            continue
        c = _mean([s.center_mm for s in obs])
        yaws = [s.yaw_deg for s in obs if s.yaw_deg is not None]
        slots[k] = SlotView(k, obs[0].state, obs[0].marker_id, c, sum(yaws) / len(yaws) if yaws else None)
    poses = [v.pose for v in good]
    pose = dict((key, sum(p[key] for p in poses) / len(poses)) for key in ("x_mm", "y_mm", "yaw_deg"))
    pose["z_mm"] = poses[0].get("z_mm")
    return ShelfView(shelf, True, slots, pose, None)


def combine_views(a, b):
    """다른 카메라 방향(1번 각도)에서 본 두 관측을 합친다. 한쪽만 판단한 칸은 그 판단, 둘 다 판단했는데
    다르면 unknown. 선반 자세는 먼저 본 것(a)."""
    if a is None or not a.seen:
        return b if (b is not None and b.seen) else a
    if b is None or not b.seen:
        return a
    slots = {}
    for k in sorted(a.slots):
        x, y = a.slots[k], b.slots.get(k)
        if y is None or y.state == "unknown":
            slots[k] = x
        elif x.state == "unknown" or x.state == y.state:
            slots[k] = y if x.state == "unknown" else x
        else:
            slots[k] = SlotView(k, "unknown", None, x.center_mm, None)
    return ShelfView(a.shelf, True, slots, a.pose, None)


def pan_toward(view, slots, s1_axis_x_mm, pan_range):
    """보이지 않은 칸들(slots) 가운데를 보게 하는 1번 각도(도, 왼쪽 +). 못 정하면 None."""
    pts = [view.slots[k].center_mm for k in slots if view.slots[k].center_mm is not None]
    if not view.seen or not pts:
        return None
    import math
    x = sum(p[0] for p in pts) / len(pts) - s1_axis_x_mm
    y = sum(p[1] for p in pts) / len(pts)
    a = math.degrees(math.atan2(y, x))
    return max(pan_range[0], min(pan_range[1], round(a, 1)))


def good_slots(views, k):
    return [v.slots[k] for v in views if v.slots[k].center_mm is not None]


def _mean(pts):
    pts = [p for p in pts if p is not None]
    if not pts:
        return None
    n = float(len(pts))
    return [round(sum(p[i] for p in pts) / n, 1) for i in range(len(pts[0]))]


def decide(task, view, carrying, n_slots):
    """반환: ("pick", SlotView) | ("place", SlotView) | ("observe", None) | ("fail", 이유)."""
    if task.action == "observe":
        if not view.seen:
            return ("fail", "wrong_shelf" if view.wrong_shelf else "shelf_not_seen")
        return ("observe", None)
    if task.slot is None or not (1 <= task.slot <= n_slots):
        return ("fail", "bad_task")
    if task.action == "load" and carrying is not None:
        return ("fail", "already_carrying")
    if task.action == "unload" and (carrying is None or (task.item is not None and carrying != task.item)):
        return ("fail", "not_carrying")
    if not view.seen:
        return ("fail", "wrong_shelf" if view.wrong_shelf else "shelf_not_seen")
    s = view.slots[task.slot]
    if s.state == "unknown":
        return ("fail", "slot_unknown")
    if task.action == "load":
        if s.state == "empty":
            return ("fail", "slot_empty")
        if task.item is not None and s.state != task.item:
            return ("fail", "wrong_item")
        return ("pick", s)
    if s.state != "empty":
        return ("fail", "slot_occupied")
    if s.center_mm is None:
        return ("fail", "slot_unknown")
    return ("place", s)


def verify(task, after, item):
    """동작 뒤 다시 본 결과로 확인. 반환: None(맞음) 또는 이유."""
    if not after.seen:
        return "verify_failed"
    st = after.state_of(task.slot)
    if task.action == "load":
        return None if st == "empty" else "verify_failed"
    return None if st == item else "verify_failed"
