# -*- coding: utf-8 -*-
"""JETANK 탐색 주행: 라인을 따라 돌며 색 패치·갈림길을 기록하고, 안 가 본 가지를 골라 맵을 완성한다.

저장소 루트에서 실행:
    # 1) 모터 없이 검출만 (손으로 밀거나 다른 프로그램으로 조종). 처음엔 꼭 이걸로 확인
    python3 -m agv.explore --mode passive --record

    # 2) 자율 주행 + 갈림길에서 사람에게 묻기
    python3 -m agv.explore --mode ask --enable-motors

    # 3) 완전 자율 탐색(다 돌면 출발 패치에서 멈춤)
    python3 -m agv.explore --mode drive --enable-motors --record

    # 녹화 영상으로 검출만 다시 돌리기(노트북에서도 됨)
    python3 -m agv.explore --camera video:data/runs/<run>/camera.mp4

결과: data/runs/<시각>/events.jsonl (+ camera.mp4, debug/*.jpg)
맵 만들기: python3 mapping/build_map.py data/runs/<시각>/events.jsonl -o maps/<이름>.json
Ctrl+C로 언제든 멈춘다(모터 정지 후 end 이벤트 기록).
"""
import argparse
import io
import json
import os
import sys
import time

import cv2

from .hw import JetbotMotors, NullMotors, open_camera, set_arm_pose
from .linecam import DEFAULT_CONFIG, draw_debug, merge_config
from .runner import ExploreRunner
from .topo import EventLogger, Explorer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_config(path):
    with io.open(path, "r", encoding="utf-8") as f:
        user = json.load(f)
    return merge_config(DEFAULT_CONFIG, user)


def make_ask_fn(motors):
    def ask(suggestion, hint=None):
        motors.stop()
        try:
            ans = input(u"\n갈림길이에요%s. [l]왼쪽 / [r]오른쪽 (Enter = 추천 %s): " % (
                u"(%s)" % hint if hint else u"", suggestion)).strip().lower()
        except EOFError:
            ans = ""
        if ans.startswith("l"):
            return "left"
        if ans.startswith("r"):
            return "right"
        return suggestion
    return ask


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["passive", "ask", "drive"], default="passive")
    ap.add_argument("--enable-motors", action="store_true",
                    help="ask/drive 모드에서 실제로 모터를 돌린다(없으면 거부)")
    ap.add_argument("--camera", default="gst", help="gst | jetbot | video:<경로>")
    ap.add_argument("--config", default=os.path.join(HERE, "config", "explore.json"))
    ap.add_argument("--out", default=None, help="결과 폴더(기본 data/runs/<시각>)")
    ap.add_argument("--record", action="store_true", help="카메라 영상을 camera.mp4로 저장")
    ap.add_argument("--debug-every", type=int, default=20, help="N프레임마다 검출 그림 저장(0이면 끔)")
    ap.add_argument("--max-time", type=float, default=600.0, help="최대 주행 시간(초)")
    ap.add_argument("--max-laps", type=int, default=6, help="출발 패치를 이만큼 지나면 강제 종료")
    ap.add_argument("--set-arm", action="store_true", help="설정의 arm.servo_angles로 카메라 자세를 맞춘다")
    ap.add_argument("--fps", type=float, default=20.0)
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    video_mode = a.camera.startswith("video:")
    if video_mode and a.mode != "passive":
        print(u"영상 재생은 passive 모드로만 돌려요.")
        a.mode = "passive"
    if a.mode != "passive" and not a.enable_motors:
        print(u"%s 모드는 로봇이 움직여요. 주변을 확인하고 --enable-motors를 붙여 다시 실행하세요." % a.mode)
        return 2

    out = a.out or os.path.join(ROOT, "data", "runs", time.strftime("%Y%m%d-%H%M%S"))
    dbg_dir = os.path.join(out, "debug")
    if not os.path.isdir(dbg_dir):
        os.makedirs(dbg_dir)
    with io.open(os.path.join(out, "config_used.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=1))

    if a.set_arm:
        set_arm_pose(cfg["arm"]["servo_angles"])

    cam = open_camera(a.camera, cfg["camera"])
    rob = cfg["robot"]
    motors = NullMotors()
    if a.mode != "passive":
        motors = JetbotMotors(rob.get("invert_left", False), rob.get("invert_right", False),
                              rob.get("trim", 0.0))

    # 영상 재생이면 영상 시간으로 기록
    clock = time.time
    vt = [0.0]
    if video_mode:
        t0 = time.time()
        clock = lambda: t0 + vt[0]  # noqa: E731

    logger = EventLogger(os.path.join(out, "events.jsonl"), echo=True, clock=clock)
    runner = ExploreRunner(cfg, logger, mode=a.mode, explorer=Explorer(max_laps=a.max_laps),
                           ask_fn=make_ask_fn(motors), speed_mm_s=rob["speed_mm_s"],
                           robot_id=rob.get("id", "agv1"), map_hint=None)
    writer = None
    period = 1.0 / a.fps
    t_start = time.time()
    last = time.time()
    last_print = 0.0
    n = 0
    print(u"결과 폴더: " + out)
    try:
        while not runner.finished:
            frame = cam.read()
            if frame is None:
                if video_mode:
                    runner.stop("video_end")
                    break
                continue
            now = time.time()
            if video_mode:
                dt = 1.0 / cam.fps
                vt[0] += dt
            else:
                dt = min(0.2, now - last)    # 갈림길에서 사람 입력을 기다린 시간은 넣지 않음
            last = now
            left, right = runner.step(frame, dt)
            motors.set(left, right)

            if a.record:
                if writer is None:
                    h, w = frame.shape[:2]
                    writer = cv2.VideoWriter(os.path.join(out, "camera.mp4"),
                                             cv2.VideoWriter_fourcc(*"mp4v"), a.fps, (w, h))
                writer.write(frame)
            if a.debug_every and n % a.debug_every == 0:
                dbg = draw_debug(frame, runner.last_an, runner.follow, runner.junc, runner.patch,
                                 "odo=%.0f L=%.2f R=%.2f" % (runner.odo, left, right))
                cv2.imwrite(os.path.join(dbg_dir, "%06d.jpg" % n), dbg)
            n += 1
            if now - last_print > 1.0:
                last_print = now
                sys.stdout.write(u"\r[%5.1fs] odo %6.0fmm  ctrl:%s  patch:%s  cmd %.2f/%.2f   " % (
                    now - t_start, runner.odo, runner.junc.state, runner.patch.active, left, right))
                sys.stdout.flush()
            if now - t_start > a.max_time:
                runner.stop("max_time")
            if not video_mode:
                sl = period - (time.time() - now)
                if sl > 0:
                    time.sleep(sl)
    except KeyboardInterrupt:
        runner.stop("user_stop")
    finally:
        motors.stop()
        cam.close()
        if writer is not None:
            writer.release()
        if not runner.finished:
            runner.stop("error")
        logger.close()
    print(u"\n종료: %s" % runner.finish_reason)
    print(u"맵 만들기: python3 mapping/build_map.py %s -o maps/<이름>.json" % os.path.join(out, "events.jsonl"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
