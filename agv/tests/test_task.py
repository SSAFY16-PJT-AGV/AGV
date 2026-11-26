# -*- coding: utf-8 -*-
"""선반 작업: 메시지 변환, 할 일 정하기, 실행기, 그리고 가상 매트 + 가상 선반에서 주문부터 보고까지."""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from agv.link.task_msgs import TaskRejected, decode_action, encode_result, encode_shelf, state_fields  # noqa: E402
from agv.nav import NavMap  # noqa: E402
from agv.task import NullArm, SlotView, ShelfView, Task, TaskExecutor, decide, merge_views  # noqa: E402
from agv.task.decide import combine_views, verify  # noqa: E402

MAP = os.path.join(ROOT, "maps", "mat_ssafy_v3.json")

try:
    sys.path.insert(0, os.path.join(ROOT, "sim", "mat"))
    import task_demo  # noqa: E402
except ImportError:
    task_demo = None


def view(states, seen=True, shelf="N01", wrong=None):
    slots = {}
    for k, st in enumerate(states, 1):
        slots[k] = SlotView(k, st, {"milk": 30, "tissue": 31}.get(st), [180.0, 105.0 - 70 * (k - 1), 170.0], 0.0)
    return ShelfView(shelf, seen, slots, {"x_mm": 170.0, "y_mm": 0.0, "z_mm": 150.0, "yaw_deg": 0.0} if seen else None,
                     wrong)


class TestMessages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = NavMap.load(MAP)

    def test_decode(self):
        t = decode_action({"type": "load", "shelf": "N01", "slot": 2, "item": "milk"}, ["N05", "N01", "D01"], self.m)
        self.assertEqual(t, Task("load", "N01", 2, "milk"))
        t = decode_action({"type": "unload", "slot": 1}, ["N01", "J02", "J07", "N03", "D03"], self.m)
        self.assertEqual(t, Task("unload", "N03", 1, None))          # 선반은 마지막 스퍼 끝의 부모
        t = decode_action({"type": "observe"}, ["N03", "N02", "D02"], self.m)
        self.assertEqual(t, Task("observe", "N02", None, None))
        self.assertIsNone(decode_action({"type": "none"}, ["N01", "D01"], self.m))
        self.assertIsNone(decode_action({"type": "dance"}, ["N01", "D01"], self.m))   # 모르는 종류는 무시

    def test_decode_rejects(self):
        bad = [({"type": "load", "shelf": "N02", "slot": 1}, ["N01", "D01"]),    # 다른 선반의 스퍼
               ({"type": "load", "slot": 1}, ["N01", "J02"]),                     # 스퍼 끝이 아님
               ({"type": "load", "shelf": "N01"}, ["N01", "D01"]),                # 칸 없음
               ({"type": "unload", "shelf": "N01", "slot": True}, ["N01", "D01"]),
               ({"type": "load", "shelf": "N01", "slot": 1, "item": 3}, ["N01", "D01"])]
        for act, nodes in bad:
            with self.assertRaises(TaskRejected, msg=act):
                decode_action(act, nodes, self.m)

    def test_encode(self):
        from agv.task import TaskOutcome
        ctx = {"session": "s", "event_id": "s-t1", "ts": 1.0, "order_id": "J1", "map_id": "m@v0"}
        before, after = view(["milk", "tissue", "empty", "unknown"]), view(["milk", "empty", "empty", "unknown"])
        out = TaskOutcome(Task("load", "N01", 2, "tissue"), True, None, "tissue", before, after)
        r = encode_result(out, ctx)
        self.assertEqual((r["type"], r["result"], r["reason"], r["carrying"]), ("task_result", "done", None, "tissue"))
        self.assertEqual([s["state"] for s in r["shelf_state"]["slots"]], ["milk", "empty", "empty", "unknown"])
        sh = encode_shelf(before, "before", ctx)
        self.assertEqual(sh["slots"][0], {"slot": 1, "state": "milk", "marker_id": 30})
        self.assertEqual(sh["slots"][2], {"slot": 3, "state": "empty"})
        self.assertEqual(state_fields(None), {"carrying": None, "task": None})


class TestDecide(unittest.TestCase):
    def test_load(self):
        v = view(["milk", "tissue", "empty", "unknown"])
        self.assertEqual(decide(Task("load", "N01", 2, "tissue"), v, None, 4)[0], "pick")
        self.assertEqual(decide(Task("load", "N01", 2, None), v, None, 4)[0], "pick")
        cases = [(Task("load", "N01", 3, None), None, "slot_empty"), (Task("load", "N01", 4, None), None, "slot_unknown"),
                 (Task("load", "N01", 1, "egg"), None, "wrong_item"), (Task("load", "N01", 1, None), "egg", "already_carrying"),
                 (Task("load", "N01", 5, None), None, "bad_task")]
        for t, carrying, reason in cases:
            self.assertEqual(decide(t, v, carrying, 4), ("fail", reason), t)

    def test_unload(self):
        v = view(["milk", "tissue", "empty", "unknown"])
        what, s = decide(Task("unload", "N01", 3, "egg"), v, "egg", 4)
        self.assertEqual((what, s.slot), ("place", 3))
        self.assertEqual(decide(Task("unload", "N01", 1, None), v, "egg", 4), ("fail", "slot_occupied"))
        self.assertEqual(decide(Task("unload", "N01", 3, None), v, None, 4), ("fail", "not_carrying"))
        self.assertEqual(decide(Task("unload", "N01", 3, "milk"), v, "egg", 4), ("fail", "not_carrying"))

    def test_shelf_not_seen(self):
        self.assertEqual(decide(Task("observe", "N01", None, None), view(["unknown"] * 4, seen=False), None, 4),
                         ("fail", "shelf_not_seen"))
        self.assertEqual(decide(Task("load", "N01", 1, None), view(["unknown"] * 4, seen=False, wrong="N02"), None, 4),
                         ("fail", "wrong_shelf"))

    def test_merge_and_combine(self):
        a, b = view(["milk", "empty", "unknown", "empty"]), view(["milk", "empty", "unknown", "tissue"])
        m = merge_views([a, a, b])
        self.assertEqual([m.state_of(k) for k in range(1, 5)], ["milk", "empty", "unknown", "unknown"])
        c = combine_views(view(["milk", "unknown", "unknown", "empty"]), view(["milk", "tissue", "unknown", "milk"]))
        self.assertEqual([c.state_of(k) for k in range(1, 5)], ["milk", "tissue", "unknown", "unknown"])

    def test_verify(self):
        self.assertIsNone(verify(Task("load", "N01", 2, None), view(["milk", "empty", "empty", "empty"]), "tissue"))
        self.assertEqual(verify(Task("load", "N01", 2, None), view(["milk", "tissue", "empty", "empty"]), "tissue"),
                         "verify_failed")
        self.assertIsNone(verify(Task("unload", "N01", 3, None), view(["milk", "empty", "egg", "empty"]), "egg"))


class FakePerceive(object):
    """perceive_shelf 대신: 정해 둔 칸 상태를 돌려줌(팔이 집으면 바꿔 줌)."""

    def __init__(self, states):
        self.states = list(states)

    def __call__(self, img, cfg, cam, servo, layout, expect_shelf=None):
        slots = []
        for k, st in enumerate(self.states, 1):
            e = {"slot": k, "state": st, "center_mm": [180.0, 105.0 - 70 * (k - 1), 170.0]}
            if st not in ("empty", "unknown"):
                e.update({"marker_id": 30, "yaw_deg": 0.0})
            slots.append(e)
        return {"shelf": {"ok": True, "shelf_id": expect_shelf, "x_mm": 170.0, "y_mm": 0.0, "z_mm": 150.0,
                          "yaw_deg": 0.0}, "slots": slots, "markers": []}


class GripArm(NullArm):
    def __init__(self, perceive):
        NullArm.__init__(self)
        self.p = perceive

    def start(self, cmd):
        NullArm.start(self, cmd)
        if cmd["op"] == "pick":
            self.p.states[cmd["slot"] - 1] = "empty"
            self._res = {"ok": True, "reason": None}
        elif cmd["op"] == "place":
            self.p.states[cmd["slot"] - 1] = "milk"
            self._res = {"ok": True, "reason": None}


class TestExecutor(unittest.TestCase):
    def make(self, states, arm=None):
        import numpy as np
        from agv.perception.shelf import ShelfLayout, camera_from_config, load_config
        pcfg = load_config()
        p = FakePerceive(states)
        arm = arm or GripArm(p)
        ex = TaskExecutor(pcfg, camera_from_config(pcfg), ShelfLayout(pcfg), arm, {"1": 0, "5": -25}, {"1": 0, "5": 30},
                          grab=lambda: np.zeros((240, 320), np.uint8), perceive=p, settle_s=0.0)
        return ex, p

    def run_task(self, ex, task, limit=100):
        ex.start(task, 0.0)
        for i in range(limit):
            out = ex.step(0.1 * (i + 1))
            if out is not None:
                return out
        self.fail("task did not finish")

    def test_pick_then_place(self):
        ex, p = self.make(["milk", "empty", "empty", "empty"])
        out = self.run_task(ex, Task("load", "N01", 1, "milk"))
        self.assertTrue(out.ok, out)
        self.assertEqual((out.carrying, out.after.state_of(1)), ("milk", "empty"))
        out = self.run_task(ex, Task("unload", "N01", 3, None))
        self.assertTrue(out.ok, out)
        self.assertEqual((out.carrying, out.after.state_of(3)), (None, "milk"))

    def test_no_arm(self):
        from agv.perception.shelf import ShelfLayout  # noqa: F401
        ex, p = self.make(["milk", "empty", "empty", "empty"], arm=NullArm())
        out = self.run_task(ex, Task("load", "N01", 1, None))
        self.assertEqual((out.ok, out.reason, out.carrying), (False, "arm_unavailable", None))
        out = self.run_task(ex, Task("observe", "N01", None, None))
        self.assertTrue(out.ok)


@unittest.skipIf(task_demo is None, "sim/mat/ 없음")
class TestSimTasks(unittest.TestCase):
    """가상 매트 + 가상 선반: 서버 주문 -> 이동 -> 칸 보기 -> 집기/놓기 -> 보고(sim/mat/task_demo.py 기본 시나리오)."""

    def test_default_jobs(self):
        r = task_demo.run(seed=2)
        link, world = r["link"], r["world"]
        self.assertEqual(r["gt"]["finish_reason"], "jobs_done", r["notes"][-5:])
        got = [(m["order_id"], m["result"], m["reason"], m["carrying"]) for m in link.tasks]
        self.assertEqual(got, [("J1", "done", None, "tissue"), ("J2", "done", None, None),
                               ("J3", "done", None, None), ("J4", "failed", "slot_empty", None)])
        snap = world.snapshot()
        self.assertEqual(snap["N01"], {1: "milk", 3: "egg", 4: "juice"})
        self.assertEqual(snap["N03"], {1: "tissue", 2: "tissue"})
        # 선반 관측: 작업 전 상태가 실제와 같음(판단 못 한 칸 빼고)
        first = link.shelves[0]
        self.assertEqual((first["shelf"], first["phase"]), ("N01", "before"))
        self.assertEqual([s["state"] for s in first["slots"]], ["milk", "tissue", "egg", "juice"])
        # 상태 메시지: 작업 중 WORKING, 들고 가는 동안 carrying
        sts = link.states
        self.assertIn("WORKING", set(s["status"] for s in sts))
        self.assertTrue(any(s["carrying"] == "tissue" and s["status"] == "MOVING" for s in sts))
        self.assertTrue(any(s["task"] and s["task"]["phase"] == "act" for s in sts))

    def test_calibration_error_reported(self):
        """카메라 위치를 25mm 틀리게 알면 팔이 빈 곳을 집는다: 실패로 보고하고 선반 상태는 그대로."""
        r = task_demo.run(jobs=task_demo.DEFAULT_JOBS[:1], seed=1,
                          geom_err={"camera_x_from_s1_mm": -25, "camera_z_mm": 15})
        m = r["link"].tasks[0]
        self.assertEqual((m["result"], m["reason"], m["carrying"]), ("failed", "grip_missed", None))
        self.assertEqual(m["shelf_state"]["slots"][1]["state"], "tissue")
        self.assertEqual(r["world"].snapshot()["N01"][2], "tissue")

    def test_bad_route_for_shelf_rejected(self):
        jobs = [(["N01", "J02", "J07", "N03"], {"type": "load", "shelf": "N03", "slot": 1})]
        r = task_demo.run(jobs=jobs, seed=1, max_time_s=20)
        self.assertEqual(r["gt"]["finish_reason"], "order_rejected")
        self.assertIn("spur end", r["ctl"].rejected["reason"])


if __name__ == "__main__":
    unittest.main()
