# -*- coding: utf-8 -*-
"""2026-11-25 새 매트(트윈 맵 warehouse_01)를 가상 매트 JSON으로 만든다.

    python3 sim/mat/mats/gen_ssafy_v3.py   ->  sim/mat/mats/ssafy_v3.json

좌표는 트윈 맵 maps/twin/warehouse_01.json(단위 m, 원점 왼쪽 위, x 오른쪽, y 아래)을 그대로 mm로 옮긴다.
가상 매트는 y가 위로 커지므로 y만 뒤집는다(트윈 y=0 -> 매트 y=1016).

구조(옛 S자 선·렌즈·X자 교차·V자는 지움):
- 왼쪽 고리와 오른쪽 고리가 위·아래 가로선으로 이어진 모양(트윈 맵 기준 가로 168.6cm, 세로 72cm).
- 곡선 반지름은 원래 매트(ssafy_v2)와 같다: 모서리·갈림 곡선 모두 약 14.7cm(도안 130px).
  트윈 맵은 곡선을 작게(약 4~5cm) 그려 두었지만 실제 테이프는 원래 매트와 같다(2026-11-26 확인).
  반지름이 이만하면 로봇이 갈림길로 알아본다(약 4cm면 직각 옆 가지로 보여 못 탐 - 시뮬레이터 확인).
- 안쪽 세로선 둘(x 853.5, 1128.5, 사이 27.5cm)은 위·아래 가로선에서 그 반지름으로 갈라진다.
  그래서 갈림점은 트윈 맵의 J02·J07(813.5), J03·J06(1168.5)보다 바깥(706.9, 1275.1)이다.
- 갈림길은 J02, J03, J06, J07 네 곳. 바깥 모서리 J01, J04, J05, J08은 로봇에게는 그냥 곡선이다.
- 패치 8개: 가로선 위 6개(N01~N06, 직사각형, 이전 매트와 비슷한 크기, 위치는 트윈 맵),
  좌우 세로선 위 충전 패치 2개(N07 분홍, N08 하늘, 14.9cm 정사각형).
  N03·N04는 원래 매트의 빨강·보라처럼 갈림점과 겹친다(원래 매트도 그랬고 로봇이 알아봤음).
- 패치마다 바깥쪽으로 나가는 스퍼(D01~D08, 14.8cm). 선 두께 2cm.

N01 주황 정렬 안 된 물체 / N02 파랑 우유 / N03 빨강 휴지 / N04 보라 계란 / N05 초록 주스 /
N06 노랑 정렬된 물체 / N07 분홍 agv01 충전 / N08 하늘 agv02 충전.

실제 매트를 줄자로 재서 다르면 아래 숫자를 고치고 다시 생성한 뒤 mapping/design_map.py를 다시 돌린다.
"""
import json
import math
import os

H = 1016.0                      # 매트 세로(mm). 트윈 맵 D02 y=1.016
W = 1982.0                      # 매트 가로(mm). 트윈 맵 D08 x=1.982
TOP, BOT = 148.0, 868.0         # 위·아래 가로선 y(트윈 좌표, 아래로 +)
LEFT, RIGHT = 148.0, 1834.0     # 왼쪽·오른쪽 세로선 x
R_OUT = 130.0 * 2000.0 / 1774.0  # 바깥 모서리 반지름(원래 매트 도안 130px = 146.6mm)
INNER_L, INNER_R = 853.5, 1128.5  # 안쪽 세로선 x(트윈 맵, 사이 27.5cm)
R_IN = R_OUT                    # 안쪽 세로선 갈림 곡선 반지름(원래 매트와 같음)
J02X, J03X = INNER_L - R_IN, INNER_R + R_IN   # 가로선에서 갈라지는 x: 706.9, 1275.1
SPUR = 148.0                    # 스퍼 길이(패치 가운데 -> 선 끝)

PATCH = (160.0, 142.0)          # 가로선 위 패치(진행 방향 x 폭 방향), 이전 매트 평균과 비슷
CHARGE = (149.0, 149.0)         # 충전 패치(트윈 size_m 0.149)

# (노드, 색, 가운데(트윈 좌표 mm), 크기(가로, 세로), 스퍼 방향)
PATCHES = [
    ("N01", "orange", (466.0, TOP), PATCH, "up"),
    ("N02", "blue", (343.0, BOT), PATCH, "down"),
    ("N03", "red", (693.0, BOT), PATCH, "down"),
    ("N04", "purple", (1256.0, TOP), PATCH, "up"),
    ("N05", "green", (1606.0, TOP), PATCH, "up"),
    ("N06", "yellow", (1493.0, BOT), PATCH, "down"),
    ("N07", "pink", (LEFT, 508.0), CHARGE, "left"),
    ("N08", "skyblue", (RIGHT, 508.0), CHARGE, "right"),
]


def mm(p):
    """트윈 좌표(y 아래) -> 가상 매트 좌표(y 위)."""
    return [round(p[0], 1), round(H - p[1], 1)]


def arc(cx, cy, r, a0, a1, step=10):
    """트윈 좌표(y 아래) 원호. 각도는 도(0=오른쪽, 90=아래)."""
    n = max(2, int(abs(a1 - a0) / step) + 1)
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / (n - 1))),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / (n - 1)))) for i in range(n)]


def line(p, q, step=60):
    n = max(2, int(math.hypot(q[0] - p[0], q[1] - p[1]) / step) + 1)
    return [(p[0] + (q[0] - p[0]) * i / (n - 1), p[1] + (q[1] - p[1]) * i / (n - 1)) for i in range(n)]


def join(*parts):
    out = []
    for part in parts:
        for p in part:
            if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1.0:
                out.append(p)
    return out


def build():
    J02, J03 = (J02X, TOP), (J03X, TOP)
    J07, J06 = (J02X, BOT), (J03X, BOT)
    lx, rx = J02X + R_IN, J03X - R_IN       # 안쪽 세로선 x
    paths = [
        # 왼쪽 고리 바깥: J07 -> 아래 가로선(N03, N02) -> 왼쪽 아래 모서리 -> 왼쪽 세로선(N07) -> 왼쪽 위 모서리 -> 위(N01) -> J02
        {"from": "J07", "to": "J02", "points": join(
            line(J07, (LEFT + R_OUT, BOT)), arc(LEFT + R_OUT, BOT - R_OUT, R_OUT, 90, 180),
            line((LEFT, BOT - R_OUT), (LEFT, TOP + R_OUT)), arc(LEFT + R_OUT, TOP + R_OUT, R_OUT, 180, 270),
            line((LEFT + R_OUT, TOP), J02))},
        # 위 연결선
        {"from": "J02", "to": "J03", "points": line(J02, J03)},
        # 오른쪽 고리 바깥: J03 -> 위(N04, N05) -> 오른쪽 위 모서리 -> 오른쪽 세로선(N08) -> 오른쪽 아래 모서리 -> 아래(N06) -> J06
        {"from": "J03", "to": "J06", "points": join(
            line(J03, (RIGHT - R_OUT, TOP)), arc(RIGHT - R_OUT, TOP + R_OUT, R_OUT, 270, 360),
            line((RIGHT, TOP + R_OUT), (RIGHT, BOT - R_OUT)), arc(RIGHT - R_OUT, BOT - R_OUT, R_OUT, 0, 90),
            line((RIGHT - R_OUT, BOT), J06))},
        # 아래 연결선
        {"from": "J06", "to": "J07", "points": line(J06, J07)},
        # 왼쪽 고리 안쪽 세로선: J02에서 오른쪽 아래로 갈라져 내려가 J07로
        {"from": "J02", "to": "J07", "points": join(
            arc(J02X, TOP + R_IN, R_IN, 270, 360), line((lx, TOP + R_IN), (lx, BOT - R_IN)),
            arc(J02X, BOT - R_IN, R_IN, 0, 90))},
        # 오른쪽 고리 안쪽 세로선: J03에서 왼쪽 아래로 갈라져 내려가 J06으로
        {"from": "J03", "to": "J06", "points": join(
            arc(J03X, TOP + R_IN, R_IN, 270, 180), line((rx, TOP + R_IN), (rx, BOT - R_IN)),
            arc(J03X, BOT - R_IN, R_IN, 180, 90))},
    ]
    for nid, color, c, _, d in PATCHES:
        end = {"up": (c[0], c[1] - SPUR), "down": (c[0], c[1] + SPUR),
               "left": (c[0] - SPUR, c[1]), "right": (c[0] + SPUR, c[1])}[d]
        paths.append({"from": "P_" + color, "to": "end_" + color, "spur": True, "node": nid,
                      "points": line(c, end, step=40)})
    return paths


def make():
    start = (300.0, TOP)
    return {
        "name": "ssafy_v3",
        "note": "2026-11-25 새 매트(트윈 맵 maps/twin/warehouse_01.json). gen_ssafy_v3.py로 생성. "
                "옛 S자·렌즈·교차는 지움. 치수는 트윈 맵 기준이라 실측 후 고칠 것.",
        "size_mm": [W, H],
        "line_width_mm": 20,
        "start_pose": [start[0], H - start[1], 0],
        "twin_nodes": dict((nid, {"color": color, "center": mm(c)}) for nid, color, c, _, _ in PATCHES),
        "patches": [{"color": color, "node": nid, "center": mm(c), "size": list(sz)}
                    for nid, color, c, sz, _ in PATCHES],
        "paths": [dict(p, points=[mm(q) for q in p["points"]]) for p in build()],
    }


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    mat = make()
    out = os.path.join(here, mat["name"] + ".json")
    with open(out, "w") as f:
        json.dump(mat, f, ensure_ascii=False, indent=1)
    print("wrote", out, len(mat["paths"]), "paths")
