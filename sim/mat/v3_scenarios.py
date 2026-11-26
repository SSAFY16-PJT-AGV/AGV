# -*- coding: utf-8 -*-
"""새 매트(ssafy_v3)와 그 맵(maps/mat_ssafy_v3.json)으로 목표 가기 시나리오를 한꺼번에 돌린다.

    python3 sim/mat/v3_scenarios.py                 # 전부
    python3 sim/mat/v3_scenarios.py --only spur     # 이름에 spur가 들어간 것만
    python3 sim/mat/v3_scenarios.py --out data/v3sim --video   # 결과 폴더에 영상까지

시나리오마다 도착 여부, 목표와의 거리(패치 가운데 또는 스퍼 선 끝), 위치를 다시 잡은 횟수, 지나간 패치·갈림길을 찍는다.
agv/tests/test_map_v3.py는 이 중 일부를 테스트로 돌린다.
"""
from __future__ import print_function

import argparse
import io
import json
import math
import os
import shutil
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sim", "mat"))

import run_sim  # noqa: E402
from mat import Mat  # noqa: E402
from agv.nav import NavMap, Navigator  # noqa: E402

MAT = os.path.join(ROOT, "sim", "mat", "mats", "ssafy_v3.json")
MAP = os.path.join(ROOT, "maps", "mat_ssafy_v3.json")
CFG = {"patch": {"length_mm": 160.0, "length_mm_by_color": {"pink": 149.0, "skyblue": 149.0}}}
TOP_START = [300, 868, 0]           # 위 가로선, N01 앞, 동쪽(시계 방향)


class World(object):
    def __init__(self):
        self.m = NavMap.load(MAP)
        d = Mat(MAT).d
        self.center = dict((p["node"], p["center"]) for p in d["patches"])
        self.spur = dict(("D" + p["node"][1:], (p["points"][0], p["points"][-1]))
                         for p in d["paths"] if p.get("spur"))

    def target_err(self, node, pose):
        """패치면 가운데까지, 스퍼 끝이면 (선 끝까지 남은 거리, 선에서 옆으로 벗어난 거리)."""
        if node.startswith("N"):
            c = self.center[node]
            return round(math.hypot(pose[0] - c[0], pose[1] - c[1]), 1)
        (jx, jy), (ex, ey) = self.spur[node]
        L = math.hypot(ex - jx, ey - jy)
        ux, uy = (ex - jx) / L, (ey - jy) / L
        dx, dy = pose[0] - jx, pose[1] - jy
        along = dx * ux + dy * uy
        return (round(L - along, 1), round(abs(-dx * uy + dy * ux), 1))


def run_case(w, goals, start="N01", start_dir="f", pose=None, seed=1, speed_scale=None, cfg=None,
             known_start=True, out=None, video=False, max_time_s=150):
    """goals: 목표 노드나 경로(list)를 차례로. 하나 끝나면(대기) 1초 뒤 다음 목표."""
    nav = Navigator(w.m, start=start if known_start else None, start_dir=start_dir,
                    hold_at_end=len(goals) > 1)
    st = {"i": 0, "t": None}

    def go(g):
        nav.set_route(g) if isinstance(g, list) else nav.set_goal(g)
    go(goals[0])

    def on_step(runner, t):
        if runner.state != "hold":
            return
        if st["t"] is None:
            st["t"] = t
        elif t - st["t"] > 1.0 and st["i"] + 1 < len(goals):
            st["i"] += 1
            nav.hold_at_end = st["i"] + 1 < len(goals)
            go(goals[st["i"]])
            runner.resume()
            st["t"] = None
    tmp = out or tempfile.mkdtemp()
    try:
        ev, gt = run_sim.run(MAT, tmp, seed=seed, max_time_s=max_time_s, policy=nav, start_pose=pose or TOP_START,
                             on_step=on_step, speed_scale=speed_scale, cfg_override=cfg or CFG, video=video)
        with io.open(ev, encoding="utf-8") as f:
            evs = [json.loads(l) for l in f]
    finally:
        if out is None:
            shutil.rmtree(tmp, ignore_errors=True)
    last = goals[-1][-1] if isinstance(goals[-1], list) else goals[-1]
    msgs = [e.get("msg") for e in evs]
    return {
        "finish": gt["finish_reason"],
        "goals_done": st["i"] + 1,
        "err": w.target_err(last, gt["final_pose"]),
        "relocalized": msgs.count("nav_relocalized"),
        "mismatch": sum(1 for e in evs if e.get("msg") == "nav_mismatch"),
        "patches": [e["color"] for e in evs if e["type"] == "patch"],
        "splits": ["%s:%s" % (e.get("node", "?"), e.get("choice") or e.get("detail")) for e in evs
                   if e["type"] in ("split", "merge")],
        "msgs": [m for m in msgs if m in ("uturn_done", "spur_out_done", "spur_park", "vee_done", "nav_off_route",
                                         "nav_gave_up")],
        "speed_cal": sum(1 for e in evs if e.get("msg") == "speed_cal" and "reject" not in e),
    }


def scenarios():
    s = []
    # 1) 위 가로선 N01 앞에서 출발해 패치마다(시계 방향, 필요하면 안쪽 세로선)
    for g in ("N02", "N03", "N04", "N05", "N06", "N07", "N08"):
        s.append(("patch_" + g, dict(goals=[g])))
    # 2) 안쪽 세로선을 타야 짧은 길
    s.append(("inner_left_N01_to_N03", dict(goals=[["N01", "J02", "J07", "N03"]])))
    s.append(("inner_right_N06_to_N04", dict(goals=["N06", ["N06", "J06", "J03", "N04"]])))
    # 3) 스퍼 끝마다 주차했다가 다음 패치로
    nxt = {"D01": "N04", "D02": "N07", "D03": "N02", "D04": "N05", "D05": "N08", "D06": "N03",
           "D07": "N01", "D08": "N06"}
    for d, n in sorted(nxt.items()):
        s.append(("spur_" + d, dict(goals=[d, n])))
    # 4) 반대 방향, 유턴
    s.append(("reverse_start_to_N07", dict(goals=["N07"], start="N01", start_dir="r", pose=[600, 868, 180])))
    s.append(("uturn_N04_back_N01", dict(goals=["N04", "N01"])))
    # 5) 시작 위치 모름 + 실제 속도 15% 느림(패치로 속도 보정)
    s.append(("unknown_start_slow_to_N05", dict(goals=["N05"], known_start=False, speed_scale=0.85, seed=7)))
    # 6) 모서리를 막 돈 뒤 스퍼(N01, 모서리 끝에서 17cm)
    s.append(("corner_spur_D01", dict(goals=[["N07", "N01", "D01"]], start="N07", pose=[148, 380, 90], seed=3)))
    # 7) 충전 패치 들렀다 선반으로(충전 패치 길이 다름)
    s.append(("charge_N08_then_N03", dict(goals=["N08", "N03"], speed_scale=0.9, seed=4)))
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--video", action="store_true")
    a = ap.parse_args()
    w = World()
    bad = 0
    for name, kw in scenarios():
        if a.only and a.only not in name:
            continue
        out = os.path.join(a.out, name) if a.out else None
        if out:
            os.makedirs(out, exist_ok=True)
        r = run_case(w, out=out, video=a.video, **kw)
        ok = r["finish"] == "nav_arrived" and r["goals_done"] == len(kw["goals"])
        bad += 0 if ok else 1
        print(u"%-26s %-4s %-12s err=%-14s reloc=%d cal=%d %s | %s | %s" % (
            name, "OK" if ok else "FAIL", r["finish"], r["err"], r["relocalized"], r["speed_cal"],
            ",".join(r["msgs"]), "-".join(r["patches"]), " ".join(r["splits"])))
    print(u"실패 %d" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
