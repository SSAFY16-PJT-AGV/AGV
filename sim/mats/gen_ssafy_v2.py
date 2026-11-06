# -*- coding: utf-8 -*-
"""2026-11-06 팀 도안(매트 두 장을 이은 2m x 1m 맵)을 가상 매트 JSON으로 만든다.

    python3 sim/mats/gen_ssafy_v2.py   ->  sim/mats/ssafy_v2.json      (도안 그대로)
                                       sim/mats/ssafy_v2_fix.json  (문제 지점을 옮긴 수정안)

수정안(fix)에서 바꾼 것 - 시뮬레이터에서 탐색이 막힌 지점들이다.
- S자 선이 오른쪽 아래 모서리에서 약 70도로 꺾여 갈라지던 것을, 오른쪽 변 직선 구간에서 20도로 갈라지게
- 빨강 스퍼: 왼쪽 고리 오른쪽 변이 아래 선에 붙는 곳 바로 뒤 -> 서쪽으로 약 18cm(직선에서 자세가 잡힌 뒤)
- 노랑 스퍼: X자 교차와 겹쳐 있어 뺌. 교차와 오른쪽 아래 모서리 사이가 24cm라 양쪽에서 15cm씩 띄울 곳이 없음
- 보라 스퍼: 합류(오른쪽 고리 왼쪽 변)와 렌즈 분기 사이 10cm 안이라 둘 곳이 없어 뺌

좌표는 도안 이미지(1774 x 887 px)에서 잰 픽셀값을 mm로 바꾼 것이다(1px = 2000/1774 mm).
실측이 나오면 이 파일의 숫자를 고치고 다시 생성한다.

진행 방향은 시계 방향(위쪽 선에서 오른쪽으로)으로 본다. 구조:
- 바깥 큰 고리: 위 선(동쪽) -> 오른쪽 고리 오른쪽 변(남쪽) -> 아래 선(서쪽) -> 왼쪽 고리 왼쪽 변(북쪽)
- 왼쪽 고리 오른쪽 변: 위 선에서 갈라져(S_a) 내려와 아래 선에 합류(M_c, 빨강 패치 바로 앞)
- 위 선의 보라~초록 사이 렌즈(S_l -> M_l)
- 오른쪽 고리 아래 오른쪽 모서리에서 갈라지는 S자 선(S_s): 노랑 패치 위에서 아래 선과 X자 교차(X1),
  아래 선 밑으로 내려갔다가 다시 아래 선과 교차(X2)한 뒤 오른쪽 고리 왼쪽 변으로 올라가
  위 선에 합류(M_b, 보라 패치 앞)
- 패치 6개(색 중복 없음)마다 바깥쪽으로 나가는 스퍼(막다른 선)
"""
import json
import math
import os

PX = 2000.0 / 1774.0
H_PX = 887.0
R = 130.0           # 모서리 반지름(px)


def mm(p):
    return [round(p[0] * PX, 1), round((H_PX - p[1]) * PX, 1)]


def arc(cx, cy, a0, a1, r=R, step=10):
    """이미지 좌표(y 아래) 원호. 각도는 도(0=오른쪽, 90=아래)."""
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


# 왼쪽 고리: x 154~757, y 140~720 / 오른쪽 고리: x 972~1619
TOP, BOT = 140.0, 720.0
LL, LR, RL, RR = 154.0, 757.0, 972.0, 1619.0
S_A = (LR - R, TOP)            # 왼쪽 고리 오른쪽 위 모서리 시작
M_C = (LR - R, BOT)
M_B = (RL + R, TOP)
S_L, M_L = (1195.0, TOP), (1345.0, TOP)
S_S_ANG = 62.0                 # 오른쪽 아래 모서리 원호에서 S자 선이 갈라지는 각도(도안)
rb_c = (RR - R, BOT - R)
S_S = (rb_c[0] + R * math.cos(math.radians(S_S_ANG)), rb_c[1] + R * math.sin(math.radians(S_S_ANG)))

def bezier(p0, p1, p2, p3, n=12):
    out = []
    for i in range(n + 1):
        t = i / float(n)
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


S_REST = [(1380, 636), (1340, 651), (1300, 684), (1278, 720),
          (1240, 746), (1200, 768), (1160, 777), (1128, 776), (1080, 758), (1040, 720), (1003, 680),
          (982, 640), (RL, 590)]


def build(variant):
  if variant == "fix":
    # 오른쪽 변(x=RR) y=470에서 20도로 갈라져 꼭대기(1411, 632)로 이어지는 S자 선
    j = (RR, 470.0)
    h = math.radians(90 + 20)          # 아래(90도)에서 오른쪽(서쪽)으로 20도
    s_head = bezier(j, (j[0] + 90 * math.cos(h), j[1] + 90 * math.sin(h)), (1490, 632), (1411, 632))
    s_split_paths = [
        {"from": "M_l", "to": "S_s", "points": join(
            line(M_L, (RR - R, TOP)), arc(RR - R, TOP + R, 270, 360), line((RR, TOP + R), j))},
        {"from": "S_s", "to": "M_c", "note": "아래 선(노랑 X1, X2 직진 통과)", "points": join(
            line(j, (RR, BOT - R)), arc(rb_c[0], rb_c[1], 0, 90, step=6), line((RR - R, BOT), M_C))},
        {"from": "S_s", "to": "M_b", "note": "S자 선 -> 오른쪽 고리 왼쪽 변", "points": join(
            s_head, S_REST, line((RL, 590), (RL, TOP + R)), arc(RL + R, TOP + R, 180, 270))},
    ]
    spur_x = {"orange": 459, "green": 1420, "blue": 307, "red": 470}
  else:
    s_split_paths = [
        {"from": "M_l", "to": "S_s", "points": join(
            line(M_L, (RR - R, TOP)), arc(RR - R, TOP + R, 270, 360), line((RR, TOP + R), (RR, BOT - R)),
            arc(rb_c[0], rb_c[1], 0, S_S_ANG, step=6))},
        {"from": "S_s", "to": "M_c", "note": "아래 선(노랑 X1, X2 직진 통과)", "points": join(
            arc(rb_c[0], rb_c[1], S_S_ANG, 90, step=6), line((RR - R, BOT), M_C))},
        {"from": "S_s", "to": "M_b", "note": "S자 선(노랑 X1, X2 교차) -> 오른쪽 고리 왼쪽 변", "points": join(
            [S_S, (1520, 679), (1480, 651), (1420, 633)], S_REST,
            line((RL, 590), (RL, TOP + R)), arc(RL + R, TOP + R, 180, 270))},
    ]
    spur_x = {"orange": 459, "purple": 1129, "green": 1420, "blue": 307, "red": 609, "yellow": 1281}
  paths = [
    {"from": "M_c", "to": "S_a", "points": join(
        line(M_C, (LL + R, BOT)), arc(LL + R, BOT - R, 90, 180), line((LL, BOT - R), (LL, TOP + R)),
        arc(LL + R, TOP + R, 180, 270), line((LL + R, TOP), S_A))},
    {"from": "S_a", "to": "M_b", "points": line(S_A, M_B)},
    {"from": "S_a", "to": "M_c", "points": join(
        arc(LR - R, TOP + R, 270, 360), line((LR, TOP + R), (LR, BOT - R)), arc(LR - R, BOT - R, 0, 90))},
    {"from": "M_b", "to": "S_l", "points": line(M_B, S_L)},
    {"from": "S_l", "to": "M_l", "points": line(S_L, M_L)},
    {"from": "S_l", "to": "M_l", "points": [S_L, (1222, 166), (1250, 186), (1270, 190), (1290, 186),
                                             (1318, 166), M_L]},
]
  paths += s_split_paths
  for color, x in spur_x.items():
    y0, y1 = (TOP, 0.0) if color in ("orange", "purple", "green") else (BOT, H_PX)
    paths.append({"from": "P_" + color, "to": "end_" + color, "spur": True,
                  "points": line((x, y0), (x, y1), step=40)})
  return paths


patches_px = [
    ("orange", (459, 138), (141, 126)), ("purple", (1126, 138), (142, 126)),
    ("green", (1422, 138), (145, 126)), ("blue", (309, 720), (137, 131)),
    ("red", (608, 721), (140, 129)), ("yellow", (1279, 720), (144, 131)),
]


def make(variant):
    name = "ssafy_v2" + ("_fix" if variant == "fix" else "")
    note = {"drawn": "2026-11-06 팀 도안(2m x 1m)을 옮긴 가상 매트.",
            "fix": "도안에서 탐색이 막힌 지점을 옮긴 수정안(파일 맨 위 설명 참고)."}[variant]
    return {
        "name": name,
        "note": note + " gen_ssafy_v2.py로 생성. 치수는 도안 비율 기준이라 실측 후 고칠 것.",
        "size_mm": [2000, 1000],
        "line_width_mm": 25,
        "start_pose": [mm((300, TOP))[0], mm((300, TOP))[1], 0],
        "patches": [{"color": c, "center": mm(ctr), "size": [round(sz[0] * PX), round(sz[1] * PX)]}
                    for c, ctr, sz in patches_px],
        "paths": [dict(p, points=[mm(q) for q in p["points"]]) for p in build(variant)],
    }


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    for v in ("drawn", "fix"):
        mat = make(v)
        out = os.path.join(here, mat["name"] + ".json")
        with open(out, "w") as f:
            json.dump(mat, f, ensure_ascii=False, indent=1)
        print("wrote", out, len(mat["paths"]), "paths")
