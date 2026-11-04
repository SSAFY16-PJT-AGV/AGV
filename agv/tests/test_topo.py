# -*- coding: utf-8 -*-
"""위상 맵 빌더·탐색 정책 테스트 (하드웨어 없음)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from agv.topo import Explorer, build_map, read_events, EventLogger  # noqa: E402

# 실제 매트를 본뜬 세계: 진행 방향 기준
#   orange -> green -> S2 -(L/R)-> purple(패치 진입) -> M2 -> yellow(패치 진입) -> S1 -(L/R)-> M1 -> orange
WORLD_LEN = {
    ("orange", "green"): 1100, ("green", "S2"): 450,
    ("S2", "left", "purple"): 420, ("S2", "right", "purple"): 520,
    ("purple", "M2"): 110, ("M2", "yellow"): 1150, ("yellow", "S1"): 100,
    ("S1", "left", "M1"): 950, ("S1", "right", "M1"): 800, ("M1", "orange"): 700,
}


def drive_world(policy_choices=None, explorer=None, laps=3, noise=0.0, start_odo=0.0):
    """세계를 따라가며 이벤트를 만든다. explorer가 있으면 갈림길 선택을 맡긴다."""
    import random
    rnd = random.Random(1)
    evs = [{"type": "start", "ts": 0, "speed_mm_s": 150, "odo_mm": 0}]
    odo = start_odo
    choices = list(policy_choices or [])

    def add(t, step, **kw):
        nonlocal_odo[0] += step * (1 + noise * (rnd.random() - 0.5))
        ev = {"type": t, "ts": nonlocal_odo[0] / 150.0, "odo_mm": nonlocal_odo[0]}
        ev.update(kw)
        evs.append(ev)
        return ev

    nonlocal_odo = [odo]
    add("patch", 0, color="orange")
    if explorer:
        explorer.on_patch("orange", nonlocal_odo[0])
    for _ in range(laps):
        add("patch", WORLD_LEN[("orange", "green")], color="green")
        if explorer:
            explorer.on_patch("green", nonlocal_odo[0])
        c2 = explorer.choose_split(nonlocal_odo[0]) if explorer else choices.pop(0)
        add("split", WORLD_LEN[("green", "S2")], choice=c2, cmd=c2)
        if explorer:
            explorer.on_split(c2, nonlocal_odo[0])
        add("patch", WORLD_LEN[("S2", c2, "purple")], color="purple")
        if explorer:
            explorer.on_patch("purple", nonlocal_odo[0])
        add("merge", WORLD_LEN[("purple", "M2")], from_side="left" if c2 == "right" else "right")
        if explorer:
            explorer.on_merge(nonlocal_odo[0])
        add("patch", WORLD_LEN[("M2", "yellow")], color="yellow")
        if explorer:
            explorer.on_patch("yellow", nonlocal_odo[0])
        c1 = explorer.choose_split(nonlocal_odo[0]) if explorer else choices.pop(0)
        add("split", WORLD_LEN[("yellow", "S1")], choice=c1, cmd=c1)
        if explorer:
            explorer.on_split(c1, nonlocal_odo[0])
        add("merge", WORLD_LEN[("S1", c1, "M1")], from_side=c1)
        if explorer:
            explorer.on_merge(nonlocal_odo[0])
        add("patch", WORLD_LEN[("M1", "orange")], color="orange")
        if explorer:
            explorer.on_patch("orange", nonlocal_odo[0])
            if explorer.done:
                break
    evs.append({"type": "end", "ts": nonlocal_odo[0] / 150.0})
    return evs


class TestBuildMap(unittest.TestCase):
    def test_full_exploration(self):
        evs = drive_world(["left", "left", "right", "right", "left", "right"], laps=3, noise=0.05)
        m = build_map([evs], map_id="mat")
        ids = sorted(n["id"] for n in m["nodes"])
        self.assertEqual(ids, ["M1", "M2", "P_green", "P_orange", "P_purple", "P_yellow", "S1", "S2"])
        self.assertTrue(m["explore"]["complete"], m["explore"])
        self.assertEqual(m["warnings"], [])
        self.assertEqual(len(m["edges"]), 10)
        by = {(e["from"], e["to"], e["branch"]): e for e in m["edges"]}
        s1 = [n for n in m["nodes"] if n["type"] == "split"]
        # yellow 다음 갈림길의 왼쪽 가지 길이 ~950
        sy = [e for e in m["edges"] if e["from"] == "P_yellow"][0]["to"]
        el = by[(sy, [e for e in m["edges"] if e["from"] == sy and e["branch"] == "left"][0]["to"], "left")]
        self.assertAlmostEqual(el["length_mm"], 950, delta=60)
        self.assertTrue(all(n["pos"] is None for n in m["nodes"]))
        self.assertEqual(len(s1), 2)

    def test_partial_exploration_reports_unexplored(self):
        evs = drive_world(["left", "left", "left", "left"], laps=2)
        m = build_map([evs])
        self.assertFalse(m["explore"]["complete"])
        self.assertEqual(sorted(u["branch"] for u in m["explore"]["unexplored"]), ["right", "right"])

    def test_patch_merge_order_swap_is_normalized(self):
        """보라 패치 위 합류: 들어오는 가지에 따라 merge가 patch보다 먼저 찍혀도 같은 그래프."""
        evs = drive_world(["left", "left", "right", "right"], laps=2)
        # 두 번째 바퀴의 purple/merge 순서를 뒤집고 거리도 살짝 앞당김
        idx = [i for i, e in enumerate(evs) if e.get("color") == "purple"][1]
        p, mg = evs[idx], evs[idx + 1]
        mg["odo_mm"], p["odo_mm"] = p["odo_mm"] - 20, p["odo_mm"]
        evs[idx], evs[idx + 1] = mg, p
        m = build_map([evs])
        self.assertEqual(len([n for n in m["nodes"] if n["type"] == "merge"]), 2, m["warnings"])

    def test_missing_detection_warns(self):
        evs = drive_world(["left", "left", "right", "right", "left", "left"], laps=3)
        # 두 번째 바퀴 S2 검출 누락
        idx = [i for i, e in enumerate(evs) if e["type"] == "split"][2]
        del evs[idx]
        m = build_map([evs])
        self.assertTrue(m["warnings"])

    def test_gap_breaks_chain(self):
        evs = drive_world(["left", "left", "right", "right"], laps=2)
        evs.insert(5, {"type": "gap", "reason": "line_lost"})
        m = build_map([evs])
        self.assertIn("P_orange", [n["id"] for n in m["nodes"]])

    def test_time_based_length_without_odo(self):
        evs = drive_world(["left", "left"], laps=1)
        for e in evs:
            e.pop("odo_mm", None)
        m = build_map([evs])
        self.assertEqual(m["edges"][0]["length_src"], "time")
        self.assertAlmostEqual(m["edges"][0]["length_mm"], 1100, delta=1)

    def test_logger_roundtrip(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.jsonl")
        lg = EventLogger(p, echo=False, clock=lambda: 1.0)
        lg.log("start", 0, robot="t", speed_mm_s=100)
        lg.log("patch", 10, color="green")
        lg.close()
        with open(p, "a") as f:
            f.write("{broken")
        evs = read_events(p)
        self.assertEqual([e["type"] for e in evs], ["start", "patch"])


class TestExplorer(unittest.TestCase):
    def test_explorer_covers_all_branches_and_stops(self):
        ex = Explorer()
        evs = drive_world(explorer=ex, laps=6)
        self.assertTrue(ex.done)
        self.assertEqual(ex.done_reason, "explored")
        m = build_map([evs])
        self.assertTrue(m["explore"]["complete"])
        self.assertEqual(len(m["nodes"]), 8)
        laps = sum(1 for e in evs if e.get("color") == "orange") - 1
        self.assertLessEqual(laps, 3)

    def test_max_laps(self):
        ex = Explorer(max_laps=1)
        drive_world(explorer=ex, laps=6)
        self.assertTrue(ex.done)


if __name__ == "__main__":
    unittest.main()
