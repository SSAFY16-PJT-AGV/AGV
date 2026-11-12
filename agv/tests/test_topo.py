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


def drive_world(policy_choices=None, explorer=None, laps=3, noise=0.0, start_odo=0.0, lens=None):
    """세계를 따라가며 이벤트를 만든다. explorer가 있으면 갈림길 선택을 맡긴다."""
    import random
    WORLD = dict(WORLD_LEN)
    WORLD.update(lens or {})
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
        add("patch", WORLD[("orange", "green")], color="green")
        if explorer:
            explorer.on_patch("green", nonlocal_odo[0])
        c2 = explorer.choose_split(nonlocal_odo[0]) if explorer else choices.pop(0)
        add("split", WORLD[("green", "S2")], choice=c2, cmd=c2)
        if explorer:
            explorer.on_split(c2, nonlocal_odo[0])
        add("patch", WORLD[("S2", c2, "purple")], color="purple")
        if explorer:
            explorer.on_patch("purple", nonlocal_odo[0])
        add("merge", WORLD[("purple", "M2")], from_side="left" if c2 == "right" else "right")
        if explorer:
            explorer.on_merge(nonlocal_odo[0])
        add("patch", WORLD[("M2", "yellow")], color="yellow")
        if explorer:
            explorer.on_patch("yellow", nonlocal_odo[0])
        c1 = explorer.choose_split(nonlocal_odo[0]) if explorer else choices.pop(0)
        add("split", WORLD[("yellow", "S1")], choice=c1, cmd=c1)
        if explorer:
            explorer.on_split(c1, nonlocal_odo[0])
        add("merge", WORLD[("S1", c1, "M1")], from_side=c1)
        if explorer:
            explorer.on_merge(nonlocal_odo[0])
        add("patch", WORLD[("M1", "orange")], color="orange")
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
        """보라 패치 위 합류: 바퀴마다 merge와 patch의 기록 순서가 뒤바뀌어도(거리 차 < reorder_mm)
        같은 그래프. 합류가 패치보다 앞으로 고정된다."""
        evs = drive_world(["left", "left", "right", "right"], laps=2, lens={("purple", "M2"): 15})
        idx = [i for i, e in enumerate(evs) if e.get("color") == "purple"][1]
        p, mg = evs[idx], evs[idx + 1]
        mg["odo_mm"], p["odo_mm"] = p["odo_mm"] - 15, p["odo_mm"]
        evs[idx], evs[idx + 1] = mg, p
        m = build_map([evs])
        self.assertEqual(len([n for n in m["nodes"] if n["type"] == "merge"]), 2, m["warnings"])
        self.assertEqual(m["warnings"], [])
        mp = [e for e in m["edges"] if e["to"] == "P_purple"]
        self.assertEqual(len(mp), 1)                 # 합류 노드 -> 보라 (한 길)
        self.assertEqual(mp[0]["from"][0], "M")

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


class TestDeadEnd(unittest.TestCase):
    def test_dead_end_turns_split_into_spur(self):
        """갈림길에서 고른 쪽이 막혀 되돌아 나왔으면 그 갈림길은 스퍼(패치 위면 패치 속성)."""
        evs = [{"type": "start", "seq": 0, "ts": 0, "speed_mm_s": 150, "odo_mm": 0},
               {"type": "patch", "seq": 1, "ts": 1, "odo_mm": 100, "color": "red"},
               {"type": "split", "seq": 2, "ts": 2, "odo_mm": 160, "choice": "left"},
               {"type": "dead_end", "seq": 3, "ts": 3, "odo_mm": 330, "side": "left", "ref_seq": 2,
                "ref_kind": "split", "spur_mm": 170.0},
               {"type": "patch", "seq": 4, "ts": 5, "odo_mm": 500, "color": "blue"},
               {"type": "split", "seq": 5, "ts": 6, "odo_mm": 900, "choice": "right"},
               {"type": "dead_end", "seq": 6, "ts": 7, "odo_mm": 1100, "side": "right", "ref_seq": 5,
                "ref_kind": "split", "spur_mm": 200.0},
               {"type": "patch", "seq": 7, "ts": 9, "odo_mm": 1300, "color": "green"},
               {"type": "end", "seq": 8, "ts": 10}]
        m = build_map([evs])
        nodes = dict((n["id"], n) for n in m["nodes"])
        self.assertEqual(nodes["P_red"]["spur"], {"side": "left", "seen": 1, "length_mm": 170.0})
        tee = [n for n in m["nodes"] if n["type"] == "tee"]
        self.assertEqual(len(tee), 1)
        self.assertEqual((tee[0]["side"], tee[0]["length_mm"]), ("right", 200.0))
        self.assertFalse([n for n in m["nodes"] if n["type"] == "split"])
        self.assertEqual([(e["from"], e["to"]) for e in m["edges"]],
                         [("P_red", "P_blue"), ("P_blue", tee[0]["id"]), (tee[0]["id"], "P_green")])

    def test_explorer_remembers_dead_end(self):
        ex = Explorer()
        ex.on_patch("red", 0)
        self.assertEqual(ex.choose_split(50), "left")
        ex.on_split("left", 50)
        ex.on_dead_end("left", 200)
        self.assertTrue(ex.splits_complete())
        ex.on_patch("blue", 400)
        ex.on_patch("red", 2000)
        self.assertEqual(ex.choose_split(2050), "right")    # 아는 막다른 길은 반대쪽
        self.assertEqual(ex.known_dead(), "left")


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


class TestExportV1(unittest.TestCase):
    """팀 공통 맵(agv-map-v1) 내보내기: 트윈 로더 호환, 좌표계, 거울상, 스퍼 끝."""

    def _map(self):
        # 원점 P_o -> S(갈림길) -> 왼쪽 L / 오른쪽 R -> M(합류) -> P_o. P_o에는 왼쪽 스퍼
        def e(i, a, b, L, br=None):
            return {"id": i, "from": a, "to": b, "branch": br, "length_mm": L, "length_std_mm": 0.0,
                    "length_src": "odo", "traversals": 1, "lanes": 1}
        return {"map_id": "t", "version": 0, "origin_node": "P_o", "created_at": "x",
                "nodes": [{"id": "P_o", "type": "patch", "color": "orange", "visits": 2,
                           "spur": {"side": "left", "seen": 2, "length_mm": 200.0}},
                          {"id": "S", "type": "split", "visits": 2, "branches": {"left": "E2", "right": "E3"}},
                          {"id": "L", "type": "patch", "color": "red", "visits": 1},
                          {"id": "R", "type": "patch", "color": "blue", "visits": 1},
                          {"id": "M", "type": "merge", "visits": 2}],
                "edges": [e("E1", "P_o", "S", 400), e("E2", "S", "L", 300, "left"), e("E3", "S", "R", 300, "right"),
                          e("E4", "L", "M", 300), e("E5", "R", "M", 300), e("E6", "M", "P_o", 900)],
                "explore": {"complete": True, "unexplored": []}, "warnings": []}

    def test_twin_compatible_and_frame(self):
        from agv.topo import to_agv_map_v1, twin_problems
        d = to_agv_map_v1(self._map())
        self.assertEqual(twin_problems(d), [])
        self.assertEqual((d["schema"], d["coordinate_unit"], d["coordinate_frame"]),
                         ("agv-map-v1", "meter", "x_forward_y_right"))
        P = dict((n["id"], (n["x"], n["y"])) for n in d["nodes"])
        self.assertEqual(P["P_o"], (0.0, 0.0))
        self.assertGreater(P["S"][0], 0.2)                       # 원점에서 나가는 방향이 +x 쪽
        # 갈림길 S의 왼쪽 가지(L)는 진행 방향 왼쪽 = 언리얼 방향 좌표에서 -y 쪽
        self.assertLess(P["L"][1], P["R"][1])
        # 스퍼 끝: 왼쪽 스퍼라 원점 패치의 -y 쪽, 길이 0.2m
        self.assertEqual(P["D_P_o"], (P["D_P_o"][0], P["D_P_o"][1]))
        self.assertLess(P["D_P_o"][1], -0.15)
        de = [e for e in d["edges"] if e["type"] == "destination"][0]
        self.assertEqual((de["from"], de["to"], de["length_m"]), ("P_o", "D_P_o", 0.2))
        self.assertTrue(all(e["type"] == "main" and e["directed"] for e in d["edges"] if e["to"] != "D_P_o"))

    def test_mirror_fix_is_independent_of_layout_flip(self):
        from agv.topo import to_agv_map_v1
        m = self._map()
        # 일부러 거울상 배치를 줘도(+왼쪽이 오른쪽에) 왼쪽 가지가 왼쪽에 오도록 바로잡히는지
        from agv.topo.export import estimate_layout, _orient
        pos = estimate_layout(m)
        flipped = dict((k, (p[0], -p[1])) for k, p in pos.items())
        fixed = _orient(m, flipped, "P_o")
        d = to_agv_map_v1(m, pos_mm=fixed)
        P = dict((n["id"], (n["x"], n["y"])) for n in d["nodes"])
        self.assertLess(P["L"][1], P["R"][1])
        self.assertEqual(d["pos_source"], "measured")

    def test_twin_problems_detects_missing_fields(self):
        from agv.topo import twin_problems
        bad = {"nodes": [{"id": "A", "x": 0, "y": 0}, {"id": "B", "pos": None}],
               "edges": [{"from": "A", "to": "B", "points": []}]}
        probs = twin_problems(bad)
        self.assertTrue(any(u"노드 필드" in p for p in probs))
        self.assertTrue(any(u"type" in p for p in probs))


if __name__ == "__main__":
    unittest.main()
