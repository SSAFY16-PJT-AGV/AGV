# -*- coding: utf-8 -*-
"""노드 이벤트 로그(JSONL) 여러 개 -> 위상 맵 JSON(topo-v0).

    python3 mapping/build_map.py data/run1/events.jsonl [data/run2/events.jsonl ...] \\
        -o maps/mat_test.json --map-id mat_test

결과는 mapping/viewer/index.html 에 끌어다 놓아서 확인한다.
Python 3.6 이상, 표준 라이브러리만 사용(Jetson에서 바로 돌려도 된다).
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agv.topo import build_map, read_events  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("events", nargs="+", help="events.jsonl 경로(여러 개면 합쳐서 맵 하나)")
    ap.add_argument("-o", "--out", required=True, help="출력 맵 JSON 경로")
    ap.add_argument("--map-id", default=None)
    ap.add_argument("--version", type=int, default=0)
    ap.add_argument("--origin", default=None, help="원점 노드 ID (기본: 처음 만난 패치)")
    ap.add_argument("--no-debug", action="store_true", help="debug.events 섹션 제외")
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
    with io.open(a.out, "w", encoding="utf-8") as f:
        f.write(json.dumps(m, ensure_ascii=False, indent=1))

    print(u"노드 %d개, 엣지 %d개 -> %s" % (len(m["nodes"]), len(m["edges"]), a.out))
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
