# -*- coding: utf-8 -*-
"""노드 이벤트 로그(JSONL) 여러 개 -> 팀 공통 맵 JSON(agv-map-v1).

    python3 mapping/build_map.py data/run1/events.jsonl [data/run2/events.jsonl ...] \\
        -o maps/mat_test.json --map-id mat_test

결과는 mapping/viewer/index.html 에 끌어다 놓아서 확인한다. 디지털 트윈(언리얼)에서 보려면
그 파일을 트윈 프로젝트의 Content/Data/warehouse_map.json 으로 복사한다(--twin 으로 바로 복사 가능).
agv-map-v1은 트윈 맵 형식에 탐색기 정보를 더한 것이다(mapping/README.md).

좌표: events.jsonl 옆에 trace.jsonl(탐색 중 회전 기록)이 있으면 로봇이 실제로 지나간 궤적으로
노드 좌표와 엣지 곡선을 계산한다(pos_source "odometry", numpy 필요). 없으면 엣지 길이로 추정한
배치다(pos_source "estimated").
Python 3.6 이상(Jetson에서 바로 돌려도 된다).
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agv.topo import (build_map, estimate_shape, load_traces_for, read_events, to_agv_map_v1,  # noqa: E402
                      twin_problems)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("events", nargs="+", help="events.jsonl 경로(여러 개면 합쳐서 맵 하나)")
    ap.add_argument("-o", "--out", required=True, help="출력 맵 JSON 경로")
    ap.add_argument("--map-id", default=None)
    ap.add_argument("--version", type=int, default=0)
    ap.add_argument("--origin", default=None, help="원점 노드 ID (기본: 처음 만난 패치)")
    ap.add_argument("--no-debug", action="store_true", help="debug.events 섹션 제외")
    ap.add_argument("--format", choices=["v1", "topo-v0"], default="v1",
                    help="v1: 팀 공통(트윈 호환, 기본). topo-v0: 예전 위상 맵 형식")
    ap.add_argument("--no-shape", action="store_true", help="trace.jsonl이 있어도 모양 계산을 하지 않음")
    ap.add_argument("--one-way", action="store_true",
                    help="탐색한 방향으로만 다니는 맵으로 내보냄(기본은 모든 엣지 양방향)")
    ap.add_argument("--twin", default=None, metavar="트윈_프로젝트_폴더",
                    help="결과를 <트윈 폴더>/Content/Data/warehouse_map.json 에도 복사(원래 파일은 .bak)")
    a = ap.parse_args()

    lists = [read_events(p) for p in a.events]
    names = [os.path.basename(os.path.dirname(os.path.abspath(p))) or p for p in a.events]
    map_id = a.map_id or os.path.splitext(os.path.basename(a.out))[0]
    m = build_map(lists, map_id=map_id, version=a.version, origin_node=a.origin, run_names=names)
    if a.no_debug:
        m.pop("debug", None)
    d = os.path.dirname(os.path.abspath(a.out))
    if not os.path.isdir(d):
        os.makedirs(d)
    shape = None
    if a.format == "v1" and not a.no_shape:
        traces = load_traces_for(a.events, names)
        if traces:
            shape, why = estimate_shape(m, traces)
            if shape is None:
                print(u"모양 계산 안 함: %s -> 엣지 길이로 추정한 배치를 씀" % why)
        else:
            print(u"trace.jsonl이 없어서 엣지 길이로 추정한 배치를 씀")
    doc = m if a.format == "topo-v0" else to_agv_map_v1(m, shape=shape, one_way=a.one_way)
    with io.open(a.out, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=1))
    if a.format == "v1":
        probs = twin_problems(doc)
        src = {"odometry": u"주행 궤적으로 계산", "estimated": u"엣지 길이로 추정한 배치"}.get(
            doc["pos_source"], doc["pos_source"])
        print(u"형식: agv-map-v1 (좌표: %s, 원점 %s) / 트윈 로더 검사: %s" % (
            src, doc["origin_node"], u"통과" if not probs else u"문제 %d개" % len(probs)))
        if shape is not None:
            f = shape["fit"]
            print(u"모양 계산: 실효 궤도 폭 %.0fmm%s, 같은 노드 방문 위치 차이 평균 %.0fmm(최대 %.0fmm, 보정 전 %.0fmm), "
                  u"곡선 있는 엣지 %d/%d" % (
                      f["track_eff_mm"], (u"(제자리 회전 %.0fmm)" % f["pivot_track_eff_mm"])
                      if f.get("pivot_track_eff_mm") else u"", f["loop_rms_mm"], f["loop_max_mm"],
                      f["loop_rms_before_mm"], f["edges_with_shape"], len(m["edges"])))
            for w in f["warnings"]:
                print(u"  [주의] " + w)
        for p in probs:
            print(u"  " + p)
    if a.twin:
        import shutil
        import time
        dst = os.path.join(a.twin, "Content", "Data", "warehouse_map.json")
        if not os.path.isdir(os.path.dirname(dst)):
            raise SystemExit(u"트윈 프로젝트 폴더가 아닌 것 같아요(Content/Data 없음): %s" % a.twin)
        if os.path.exists(dst):
            shutil.copy(dst, dst + time.strftime(".bak-%Y%m%d-%H%M%S"))
        shutil.copy(a.out, dst)
        print(u"트윈에 복사: %s" % dst)

    extra = u"" if a.format == "topo-v0" else u" (스퍼 끝 노드·엣지 %d개 더함)" % (len(doc["nodes"]) - len(m["nodes"]))
    print(u"노드 %d개, 엣지 %d개%s -> %s" % (len(m["nodes"]), len(m["edges"]), extra, a.out))
    for e in m["edges"]:
        print(u"  %-4s %-9s -> %-9s %-6s %6.0f ± %3.0f mm  (%d회)" % (
            e["id"], e["from"], e["to"], e["branch"] or "", e["length_mm"], e["length_std_mm"],
            e["traversals"]))
    if m["explore"]["unexplored"]:
        print(u"미탐색 가지: " + ", ".join("%s/%s" % (u["node"], u["branch"])
                                       for u in m["explore"]["unexplored"]))
    for w in m["warnings"]:
        print(u"경고: " + w)


if __name__ == "__main__":
    main()
