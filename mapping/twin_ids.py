# -*- coding: utf-8 -*-
"""로봇 맵(design_map 결과)의 노드 이름과 좌표를 트윈 맵에 맞춘다.

    python3 mapping/twin_ids.py <로봇 맵> --mat <가상 매트 JSON> --twin maps/twin/warehouse_01.json [-o 출력]

- 이름: 패치 P_<색> -> 매트 도안의 노드(N01~), 스퍼 끝 D_P_<색> -> 같은 번호의 D(D01~),
  갈라짐·합류(S1, M1 ...) -> 트윈 맵에서 가장 가까운 갈림길(J02 등, 서로 다른 것끼리 짝).
  엣지·구역·이벤트·경고 안의 이름도 같이 바꾼다(E_D_P_<색> -> E_D01, Z_D_P_<색> -> Z_D01).
- 좌표: 로봇 맵은 원점이 첫 패치 가운데, x는 그 패치를 지나는 진행 방향, y는 그 오른쪽이다. 새 매트는
  그 방향이 트윈 맵(원점 매트 왼쪽 위, x 오른쪽, y 아래)과 같으므로 평행이동만 한다.
- 트윈 노드의 역할(group, purpose)과 팀이 정한 역할 이름(role, label)을 노드에 붙인다.
- 갈림길 좌표는 로봇이 알아보는 자리라 트윈 맵이 그린 자리와 몇 cm 다를 수 있다. 바깥 모서리 노드
  (J01, J04, J05, J08)는 로봇이 알아보는 노드가 아니라 넣지 않는다(엣지 곡선에는 들어 있음).
"""
from __future__ import print_function

import argparse
import io
import json
import math
import re

# 팀이 정한 노드 역할(2026-11-25)
ROLES = {
    "N01": ("unsorted", u"정렬되지 않은 물체 공간"),
    "N02": ("shelf_milk", u"우유 선반"),
    "N03": ("shelf_tissue", u"휴지 선반"),
    "N04": ("shelf_egg", u"계란 선반"),
    "N05": ("shelf_juice", u"주스 선반"),
    "N06": ("sorted", u"정렬된 물체 공간"),
    "N07": ("charge_agv01", u"agv01 충전 공간"),
    "N08": ("charge_agv02", u"agv02 충전 공간"),
}


def _shift_points(obj, dx, dy):
    if isinstance(obj, dict):
        if "x" in obj and "y" in obj and isinstance(obj["x"], (int, float)):
            obj["x"] = round(obj["x"] + dx, 4)
            obj["y"] = round(obj["y"] + dy, 4)
        for k, v in obj.items():
            if k in ("nodes", "edges", "points"):
                for it in v:
                    _shift_points(it, dx, dy)
    return obj


def apply(doc, mat, twin):
    """doc(로봇 맵)를 고쳐 돌려준다. mat: 가상 매트 JSON(patches에 node, color), twin: 트윈 맵."""
    H = float(mat["size_mm"][1])
    color_node = dict((p["color"], p["node"]) for p in mat["patches"] if p.get("node"))
    twin_n = dict((n["id"], n) for n in twin["nodes"])
    # 평행이동: 로봇 맵 원점 패치의 트윈 좌표 - 로봇 맵 좌표
    nodes = dict((n["id"], n) for n in doc["nodes"])
    org = doc.get("origin_node")
    if org not in nodes or not org.startswith("P_"):
        org = next(i for i in nodes if i.startswith("P_"))
    tn = twin_n[color_node[org[2:]]]
    # 트윈 맵 패치 위치 대신 매트 도안의 위치(같은 노드인데 도안이 실측 기준)
    pc = next(p for p in mat["patches"] if p.get("node") == tn["id"])
    tx, ty = pc["center"][0] / 1000.0, (H - pc["center"][1]) / 1000.0
    dx, dy = tx - nodes[org]["x"], ty - nodes[org]["y"]
    _shift_points(doc, dx, dy)

    rename = {}
    for c, nid in color_node.items():
        num = nid[1:]
        rename["E_D_P_" + c] = "E_D" + num
        rename["Z_D_P_" + c] = "Z_D" + num
        rename["D_P_" + c] = "D" + num
        rename["P_" + c] = nid
    # 갈림길: 트윈 맵에서 연결이 셋 이상인 junction(T자)과 가장 가까운 것끼리
    deg = {}
    for e in twin["edges"]:
        for k in ("from", "to"):
            deg[e[k]] = deg.get(e[k], 0) + 1
    tj = [n for n in twin["nodes"] if n["type"] == "junction" and deg.get(n["id"], 0) >= 3]
    rj = [n for n in doc["nodes"] if n["type"] == "junction" and n.get("kind") in ("split", "merge", "cross", "tee")]
    pairs = sorted((math.hypot(a["x"] - b["x"], a["y"] - b["y"]), a["id"], b["id"]) for a in rj for b in tj)
    used_r, used_t, jinfo = set(), set(), {}
    for d, ra, tb in pairs:
        if ra in used_r or tb in used_t:
            continue
        used_r.add(ra)
        used_t.add(tb)
        rename[ra] = tb
        jinfo[tb] = round(d * 1000.0, 1)
    left = [n["id"] for n in rj if n["id"] not in used_r]
    keys = sorted(rename, key=len, reverse=True)
    # 경계는 ASCII 글자·숫자·_만 본다(한글 경고 문장 "M1에서" 안의 이름도 바꾸게)
    pat = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(k) for k in keys) + r")(?![A-Za-z0-9_])")
    text = json.dumps(doc, ensure_ascii=False)
    doc = json.loads(pat.sub(lambda m: rename[m.group(1)], text))
    for n in doc["nodes"]:
        t = twin_n.get(n["id"])
        if t:
            for k in ("group", "purpose"):
                if k in t:
                    n[k] = t[k]
            if n["id"] in jinfo:
                n["twin_drawn_offset_mm"] = jinfo[n["id"]]
        if n["id"] in ROLES:
            n["role"], n["label"] = ROLES[n["id"]]
    doc["origin_node"] = None
    doc["origin"] = u"트윈 맵과 같음(매트 왼쪽 위, x 오른쪽, y 아래)"
    doc["twin_ids"] = {"twin_map": twin.get("map_id"), "renamed": dict((k, v) for k, v in rename.items()
                                                                     if not k.startswith(("E_", "Z_"))),
                       "unmatched_junctions": left}
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("map")
    ap.add_argument("--mat", required=True)
    ap.add_argument("--twin", required=True)
    ap.add_argument("-o", "--out", default=None, help="기본: 그 자리에 고쳐 씀")
    a = ap.parse_args()
    with io.open(a.map, encoding="utf-8") as f:
        doc = json.load(f)
    with io.open(a.mat, encoding="utf-8") as f:
        mat = json.load(f)
    with io.open(a.twin, encoding="utf-8") as f:
        twin = json.load(f)
    doc = apply(doc, mat, twin)
    with io.open(a.out or a.map, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=1))
    print(u"이름 바꿈: %s" % ", ".join("%s->%s" % kv for kv in sorted(doc["twin_ids"]["renamed"].items())))
    if doc["twin_ids"]["unmatched_junctions"]:
        print(u"트윈 맵에 짝이 없는 갈림길: %s" % ", ".join(doc["twin_ids"]["unmatched_junctions"]))


if __name__ == "__main__":
    main()
