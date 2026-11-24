# -*- coding: utf-8 -*-
"""매트 도안(가상 매트 JSON) -> 실제 매트 맵(agv-map-v1). 로봇을 돌리지 않는다.

    python3 mapping/design_map.py sim/mat/mats/ssafy_v2.json -o maps/mat_ssafy_v2.json
    python3 mapping/design_map.py sim/mat/mats/ssafy_v2.json -o maps/mat_ssafy_v2.json --twin <트윈 폴더>

노드 종류와 연결은 로봇이 그 매트에서 실제로 알아보는 것과 같아야 목표 패치로 가기(goto)가 맞는다.
그래서 도안을 시뮬레이터에서 탐색시켜(로봇과 같은 인식 코드) 위상을 만들고, 좌표·곡선·길이는 추정 대신
도안의 정답으로 채운다.
    - 노드 종류·갈림길 좌우·합류 입력·스퍼: 시뮬레이터 탐색 결과(build_map)
    - 패치 노드: 도안의 패치 가운데, 스퍼 끝: 도안의 선 끝
    - 갈라짐·합류·교차 노드: 실제 궤적에서 로봇이 그 노드를 알아본 자리(도안 분기점보다 몇 cm 뒤일 수
      있다. 로봇이 알아보는 자리와 엣지 길이가 맞아야 goto의 위치 추적이 맞아서 일부러 옮기지 않음)
    - 엣지 곡선: 실제 궤적(선 위를 지난 길)
    - 엣지 길이: 노드를 알아본 지점 사이 주행 거리(탐색 맵과 같은 기준, 실제 속도로 달려 정답과 같음).
      스퍼 엣지는 패치 가운데에서 도안의 선 끝까지
여러 난수로 탐색해 경고 없이 끝까지 탐색한 결과를 고른다.

도안 숫자가 실제 매트와 다르면(줄자로 잰 값) sim/mat/mats/gen_ssafy_v2.py를 고쳐 도안을 다시 만들고
이 스크립트를 다시 돌린다. 노트북용(opencv-python, numpy 필요).
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
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sim", "mat"))

from agv.topo import build_map, read_events, read_trace, to_agv_map_v1, twin_problems  # noqa: E402
from agv.topo.shape import read_truth, shape_from_truth  # noqa: E402


def _patch_length(design):
    """진행 방향 패치 길이: 패치가 놓인 선 방향의 변. 도안은 가로 선 위 패치라 size[0]."""
    ps = design.get("patches") or []
    return round(sum(p["size"][0] for p in ps) / len(ps), 1) if ps else None


def explore(mat_path, seeds, work, quiet=False):
    """시뮬레이터로 탐색. 반환: (맵 dict, 주행 폴더, 난수) - 경고 없이 끝까지 탐색한 첫 결과(없으면 경고가 가장 적은 것)."""
    import run_sim
    with io.open(mat_path, encoding="utf-8") as f:
        design = json.load(f)
    plen = _patch_length(design)
    best = None
    for seed in seeds:
        out = os.path.join(work, "seed%d" % seed)
        ev, gt = run_sim.run(mat_path, out, seed=seed, max_time_s=480, speed_scale=1.0,
                             cfg_override={"patch": {"length_mm": plen}} if plen else None)
        m = build_map([read_events(ev)], run_names=["r"])
        ok = gt["finish_reason"] == "explore_explored" and m["explore"]["complete"]
        score = (0 if ok else 1, len(m["warnings"]))
        if not quiet:
            print(u"  난수 %d: %s, 노드 %d, 엣지 %d, 경고 %d" % (
                seed, gt["finish_reason"], len(m["nodes"]), len(m["edges"]), len(m["warnings"])))
        if best is None or score < best[0]:
            best = (score, m, out, seed, gt)
        if score == (0, 0):
            break
    return best[1], best[2], best[3], best[4]


def design_snap(m, gt):
    """도안 좌표로 고정할 노드: 패치 -> 패치 가운데, 그 패치의 스퍼 끝(D_P_*) -> 도안의 선 끝."""
    snap = {}
    centers = dict(("P_" + p["color"], tuple(p["center"])) for p in gt["patches"])
    ends = dict((k[4:], tuple(v)) for k, v in gt["nodes"].items() if k.startswith("end_"))
    for n in m["nodes"]:
        nid = n["id"]
        if nid in centers:
            snap[nid] = centers[nid]
            if n.get("spur") and nid[2:] in ends:
                snap["D_" + nid] = ends[nid[2:]]
    return snap


def design_heading(design, center):
    """도안에서 점 center를 지나는 선의 방향(단위 벡터). 원점 패치를 지나는 진행 방향(+x)을 도안대로 맞출 때 씀."""
    best = None
    for p in design["paths"]:
        if str(p.get("to", "")).startswith("end_") or str(p.get("from", "")).startswith("end_"):
            continue                          # 스퍼(패치 가운데에서 시작)는 빼고 본선만
        pts = p["points"]
        for a, b in zip(pts[:-1], pts[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            if L2 < 1e-9:
                continue
            t = max(0.0, min(1.0, ((center[0] - a[0]) * dx + (center[1] - a[1]) * dy) / L2))
            d = math.hypot(a[0] + t * dx - center[0], a[1] + t * dy - center[1])
            if best is None or d < best[0]:
                L = math.sqrt(L2)
                best = (d, (dx / L, dy / L))
    return best[1] if best else None


def build(mat_path, map_id=None, seeds=range(6), quiet=False):
    work = tempfile.mkdtemp(prefix="design_map_")
    try:
        m, run_dir, seed, gt = explore(mat_path, seeds, work, quiet)
        traces = {"r": read_trace(os.path.join(run_dir, "trace.jsonl"))}
        truths = {"r": read_truth(os.path.join(run_dir, "truth.jsonl"))}
        snap = design_snap(m, gt)
        fix = None
        org = m.get("origin_node")
        if org in snap:
            with io.open(mat_path, encoding="utf-8") as f:
                h = design_heading(json.load(f), snap[org])
            if h:
                fix = {org: h}
                # 도안 선에는 방향이 없어서, 실제 궤적이 원점을 지날 때의 방향과 부호를 맞춤
                X = truths["r"]
                th = [math.atan2(math.sin(t), math.cos(t)) for t in X[:, 2]]
                near = min(range(len(X)), key=lambda i: math.hypot(X[i, 0] - snap[org][0], X[i, 1] - snap[org][1]))
                if math.cos(th[near]) * h[0] + math.sin(th[near]) * h[1] < 0:
                    fix = {org: (-h[0], -h[1])}
        shape, why = shape_from_truth(m, traces, truths, snap=snap, fix_heading=fix)
        if shape is None:
            raise SystemExit(u"좌표 계산 실패: %s" % why)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if map_id:
        m["map_id"] = map_id
    doc = to_agv_map_v1(m, shape=shape)
    # 스퍼 엣지 길이: 패치 가운데 -> 도안의 선 끝(본선 엣지는 탐색 맵과 같은 기준 그대로)
    xy = dict((n["id"], (n["x"], n["y"])) for n in doc["nodes"])
    for e in doc["edges"]:
        if e["type"] == "destination" and e["to"] in xy and e["from"] in shape["spur_end"]:
            (ax, ay), (bx, by) = xy[e["from"]], xy[e["to"]]
            e["length_m"] = round(math.hypot(bx - ax, by - ay), 4)
    for n in doc["nodes"]:
        if n["id"].startswith("D_") and n["id"][2:] in shape["spur_end"]:
            n["length_estimated"] = False
    doc["pos_source"] = "design"
    doc["design"] = {"mat": os.path.relpath(mat_path, ROOT), "explore_seed": seed,
                     "snapped": shape["fit"]["snapped"], "made_at": time.strftime("%Y-%m-%d %H:%M")}
    doc.pop("shape_fit", None)
    return doc, m


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mat", help="가상 매트(도안) JSON, 예: sim/mat/mats/ssafy_v2.json")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--map-id", default=None, help="기본: 출력 파일 이름")
    ap.add_argument("--seeds", type=int, default=6, help="시도할 탐색 난수 개수")
    ap.add_argument("--twin", default=None, metavar="트윈_프로젝트_폴더",
                    help="Content/Data/warehouse_map.json으로 복사(원래 파일은 .bak)")
    a = ap.parse_args()
    map_id = a.map_id or os.path.splitext(os.path.basename(a.out))[0]
    print(u"도안을 시뮬레이터로 탐색해 위상을 만들어요: %s" % a.mat)
    doc, m = build(a.mat, map_id, range(a.seeds))
    d = os.path.dirname(os.path.abspath(a.out))
    if not os.path.isdir(d):
        os.makedirs(d)
    with io.open(a.out, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=1))
    probs = twin_problems(doc)
    print(u"형식: agv-map-v1 (좌표: 도안, 원점 %s) / 트윈 로더 검사: %s" % (
        doc["origin_node"], u"통과" if not probs else u"문제 %d개" % len(probs)))
    for p in probs:
        print(u"  " + p)
    print(u"도안 좌표로 고정한 노드: %s" % u", ".join(doc["design"]["snapped"]))
    print(u"노드 %d개, 엣지 %d개 -> %s" % (len(doc["nodes"]), len(doc["edges"]), a.out))
    for e in doc["edges"]:
        if e["type"] == "main":
            print(u"  %-6s %-9s -> %-9s %-6s %6.0f mm" % (
                e["id"], e["from"], e["to"], e.get("branch") or "", e["length_m"] * 1000))
    for w in m["warnings"]:
        print(u"경고: " + w)
    if a.twin:
        dst = os.path.join(a.twin, "Content", "Data", "warehouse_map.json")
        if not os.path.isdir(os.path.dirname(dst)):
            raise SystemExit(u"트윈 프로젝트 폴더가 아닌 것 같아요(Content/Data 없음): %s" % a.twin)
        if os.path.exists(dst):
            shutil.copy(dst, dst + time.strftime(".bak-%Y%m%d-%H%M%S"))
        shutil.copy(a.out, dst)
        print(u"트윈에 복사: %s" % dst)


if __name__ == "__main__":
    main()
