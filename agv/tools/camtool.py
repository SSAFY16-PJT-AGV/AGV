# -*- coding: utf-8 -*-
"""현장 보정 도구. 저장소 루트에서 실행한다.

    # 지금 카메라 화면 한 장 + 검출 결과 그림 저장 (카메라 자세·라인 검출 확인)
    python3 -m agv.tools.camtool snap [--camera gst] [--out data/snap]

    # 사진의 사각형 영역 HSV 분포 출력 -> config의 patch.colors / line.v_max 조정에 사용
    python3 -m agv.tools.camtool hsv data/snap/raw.png 120 150 40 30      # x y w h (px)

    # 기본 속도로 몇 초 직진 -> 간 거리를 자로 재서 robot.speed_mm_s에 넣는다
    python3 -m agv.tools.camtool speed --seconds 4 --enable-motors
"""
import argparse
import os
import sys
import time

import cv2
import numpy as np

from ..explore import HERE, ROOT, load_config
from ..hw import JetbotMotors, open_camera
from ..linecam import JunctionTracker, PatchTracker, analyze, draw_debug


def cmd_snap(a, cfg):
    cam = open_camera(a.camera, cfg["camera"])
    try:
        frame = None
        for _ in range(5):
            frame = cam.read()
    finally:
        cam.close()
    if frame is None:
        print(u"프레임을 못 읽었어요.")
        return 1
    if not os.path.isdir(a.out):
        os.makedirs(a.out)
    an = analyze(frame, cfg)
    cv2.imwrite(os.path.join(a.out, "raw.png"), frame)
    cv2.imwrite(os.path.join(a.out, "debug.png"), draw_debug(frame, an, None, JunctionTracker(cfg), PatchTracker(cfg)))
    cv2.imwrite(os.path.join(a.out, "line_mask.png"), an.line_mask * 255)
    print(u"저장: %s (raw.png, debug.png, line_mask.png)" % a.out)
    print(u"줄별 라인 조각 수(위=멂): " + " ".join(str(len(s)) for _, s in an.rows))
    print(u"패치 색 비율(화면 아래쪽): " + ", ".join("%s %.2f" % kv for kv in sorted(an.patch_fracs.items())))
    return 0


def cmd_hsv(a, cfg):
    img = cv2.imread(a.image)
    if img is None:
        print(u"이미지를 못 읽었어요: " + a.image)
        return 1
    roi = img[a.y:a.y + a.h, a.x:a.x + a.w]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    for i, name in enumerate(("H", "S", "V")):
        p = np.percentile(hsv[:, i], [5, 25, 50, 75, 95])
        print(u"%s  5%%:%3d  25%%:%3d  50%%:%3d  75%%:%3d  95%%:%3d" % ((name,) + tuple(int(v) for v in p)))
    print(u"색 패치면 H의 5~95% 범위를 patch.colors에, 라인이면 V 95%보다 조금 큰 값을 line.v_max에 넣으세요.")
    return 0


def cmd_speed(a, cfg):
    if not a.enable_motors:
        print(u"로봇이 직진해요. 앞을 비우고 --enable-motors를 붙여 다시 실행하세요.")
        return 2
    rob = cfg["robot"]
    m = JetbotMotors(rob.get("invert_left", False), rob.get("invert_right", False), rob.get("trim", 0.0))
    base = cfg["follow"]["base_speed"]
    print(u"%.1f초 동안 base_speed=%.2f로 직진합니다. 출발점을 표시해 두세요." % (a.seconds, base))
    time.sleep(2.0)
    try:
        m.set(base, base)
        time.sleep(a.seconds)
    finally:
        m.stop()
    try:
        d = float(input(u"간 거리(mm)를 입력하세요: ").strip())
    except (ValueError, EOFError):
        print(u"숫자가 아니라서 끝냅니다.")
        return 1
    v = d / a.seconds
    print(u"속도 = %.0f mm/s. %s의 robot.speed_mm_s에 넣으세요." % (v, a.config))
    print(u"옆으로 쏠렸다면 robot.trim을 조금씩(±0.02) 바꿔 다시 재 보세요.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "config", "explore.json"))
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("snap")
    s.add_argument("--camera", default="gst")
    s.add_argument("--out", default=os.path.join(ROOT, "data", "snap"))
    h = sub.add_parser("hsv")
    h.add_argument("image")
    for k in ("x", "y", "w", "h"):
        h.add_argument(k, type=int)
    sp = sub.add_parser("speed")
    sp.add_argument("--seconds", type=float, default=4.0)
    sp.add_argument("--enable-motors", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.cmd == "snap":
        return cmd_snap(a, cfg)
    if a.cmd == "hsv":
        return cmd_hsv(a, cfg)
    if a.cmd == "speed":
        return cmd_speed(a, cfg)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
