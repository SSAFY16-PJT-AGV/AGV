# -*- coding: utf-8 -*-
"""가상 매트에서 실물과 같은 탐색 코드(agv/runner.py)를 돌려 이벤트 로그를 만든다.

    python3 sim/mat/run_sim.py --out data/sim_run --video
    python3 mapping/build_map.py data/sim_run/events.jsonl -o maps/sim.json

산출물: events.jsonl·trace.jsonl(실물과 같은 형식), gt.json(정답 노드 위치·경로 길이·궤적),
--video면 run.mp4(카메라 시점 + 위에서 본 궤적).
"""
import argparse
import json
import math
import os
import sys

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from agv.linecam import DEFAULT_CONFIG, draw_debug, merge_config  # noqa: E402
from agv.runner import ExploreRunner  # noqa: E402
from agv.topo import EventLogger, Explorer  # noqa: E402
from mat import Mat  # noqa: E402

CAM = {"near_mm": 90, "far_mm": 380, "near_width_mm": 180, "far_width_mm": 460}


class SimClock(object):
    def __init__(self, t0=1.7e9):
        self.t = t0

    def __call__(self):
        return self.t


def run(mat_path, out_dir, seed=0, max_time_s=420.0, dt=0.05, video=False,
        mode="drive", speed_err=0.05, slip=0.03, vmax_mm_s=500.0, track_mm=140.0,
        cfg_override=None, quiet=True, max_laps=8, raw_video=False, start_pose=None,
        branch_order=("left", "right"), speed_scale=None, speed_drift=0.0, turn_slip=0.0, pivot_slip=None,
        policy=None, on_step=None):
    """speed_scale: 실제 속도 / 설정 속도를 이 값으로 고정(기본: 1 ± speed_err 무작위).
    speed_drift: 1분마다 실제 속도가 이 비율만큼 줄어듦(배터리 소모 흉내).
    turn_slip: 궤도 미끄러짐으로 회전이 덜 되는 비율(0.3이면 좌우 속도 차이로 계산한 회전의 70%만 돎.
    실효 궤도 폭 = track_mm / (1 - turn_slip)). pivot_slip: 제자리 회전 때의 비율(기본 turn_slip).
    policy: 탐색 정책 대신 쓸 정책(예: agv.nav.Navigator). 주면 max_laps·branch_order는 안 씀.
    on_step(runner, t): 프레임마다 부름(대기 중인 로봇을 재개하는 등 시험용)."""
    rnd = np.random.RandomState(seed)
    os.makedirs(out_dir, exist_ok=True)
    mat = Mat(mat_path)
    cfg = merge_config(DEFAULT_CONFIG, cfg_override or {})
    clock = SimClock()
    ev_path = os.path.join(out_dir, "events.jsonl")
    if os.path.exists(ev_path):
        os.remove(ev_path)
    logger = EventLogger(ev_path, echo=not quiet, clock=clock)
    base = cfg["follow"]["base_speed"]
    runner = ExploreRunner(cfg, logger, mode=mode,
                           explorer=policy or Explorer(max_laps=max_laps, branch_order=branch_order),
                           speed_mm_s=base * vmax_mm_s, robot_id="sim", map_hint=mat.d["name"],
                           trace_path=os.path.join(out_dir, "trace.jsonl"))
    # 실제 속도는 설정값과 조금 다르다(속도 실측 오차 재현)
    true_scale = 1.0 + speed_err * (2 * rnd.rand() - 1)
    if speed_scale is not None:
        true_scale = float(speed_scale)
    dist = 0.0
    odo_true = []
    bias = 1.0 + 0.02 * (2 * rnd.rand() - 1)
    x, y, th = start_pose or mat.d["start_pose"]
    th = math.radians(th)
    traj = []
    vw = None
    if video:
        sx = 480.0 / mat.image.shape[1]
        top_small = cv2.resize(mat.image, (480, int(round(mat.image.shape[0] * sx))))
        ch = max(240, top_small.shape[0])
        vw = cv2.VideoWriter(os.path.join(out_dir, "run.mp4"), cv2.VideoWriter_fourcc(*"mp4v"),
                             20, (320 + 480, ch))
    raw = None
    if raw_video:   # 로봇 --record와 같은 원본 카메라 영상
        raw = cv2.VideoWriter(os.path.join(out_dir, "camera.mp4"), cv2.VideoWriter_fourcc(*"mp4v"),
                              int(round(1 / dt)), (320, 240))
    t = 0.0
    while t < max_time_s and not runner.finished:
        frame = mat.camera_view((x, y, th), CAM)
        frame = np.clip(frame.astype(np.int16) + rnd.normal(0, 4, frame.shape), 0, 255).astype(np.uint8)
        if raw is not None:
            raw.write(frame)
        left, right = runner.step(frame, dt)
        # 궤도형 차체 기구학 + 미끄러짐
        sc = true_scale * (1.0 - speed_drift * t / 60.0)
        vl = left * vmax_mm_s * sc * (1 + slip * rnd.randn()) * bias
        vr = right * vmax_mm_s * sc * (1 + slip * rnd.randn()) / bias
        v = (vl + vr) / 2.0
        w = (vl - vr) / track_mm          # 왼쪽이 빠르면 오른쪽(시계방향)으로 돈다
        w *= 1.0 - (pivot_slip if (pivot_slip is not None and vl * vr < 0) else turn_slip)
        th -= w * dt
        x += v * math.cos(th) * dt
        y += v * math.sin(th) * dt
        dist += abs(v) * dt
        t += dt
        clock.t += dt
        if on_step is not None:
            on_step(runner, t)
        if int(t / dt) % 4 == 0:
            traj.append([round(x, 1), round(y, 1)])
            odo_true.append([round(clock.t, 3), round(dist, 1)])
        if vw is not None:
            dbg = draw_debug(frame, runner.last_an, runner.follow, runner.junc, runner.patch,
                             "t=%.1fs odo=%.0f" % (t, runner.odo), tee=runner.tee)
            dbg = cv2.resize(dbg, (320, 240))
            top = top_small.copy()
            for a, b in zip(traj[:-1], traj[1:]):
                pa, pb = mat.to_px(a), mat.to_px(b)
                cv2.line(top, (int(pa[0] * sx), int(pa[1] * sx)), (int(pb[0] * sx), int(pb[1] * sx)),
                         (255, 0, 0), 1)
            p = mat.to_px((x, y))
            cv2.circle(top, (int(p[0] * sx), int(p[1] * sx)), 5, (0, 0, 255), -1)
            canvas = np.full((ch, 800, 3), 255, np.uint8)
            canvas[:240, :320] = dbg
            canvas[:top.shape[0], 320:] = top
            vw.write(canvas)
    if not runner.finished:
        runner.stop("sim_timeout")
    logger.close()
    if vw is not None:
        vw.release()
    if raw is not None:
        raw.release()
    gt = {"mat": mat.d["name"], "nodes": {k: v for k, v in mat.nodes.items()},
          "patches": mat.d["patches"],
          "paths": [{"from": c["from"], "to": c["to"], "length_mm": round(c["length_mm"], 1)}
                    for c in mat.curves],
          "true_speed_scale": true_scale, "sim_time_s": round(t, 2),
          "finish_reason": runner.finish_reason, "trajectory": traj,
          "final_pose": [round(x, 1), round(y, 1), round(math.degrees(th), 1)],
          "odo_true": odo_true, "speed_drift": speed_drift, "turn_slip": turn_slip,
          "pivot_slip": turn_slip if pivot_slip is None else pivot_slip, "track_mm": track_mm}
    with open(os.path.join(out_dir, "gt.json"), "w") as f:
        json.dump(gt, f, ensure_ascii=False)
    cv2.imwrite(os.path.join(out_dir, "mat_topdown.png"), mat.image)
    return ev_path, gt


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mat", default=os.path.join(os.path.dirname(__file__), "mats", "ssafy_mat.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "sim_run"))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-time", type=float, default=420.0)
    ap.add_argument("--video", action="store_true", help="카메라 시점+궤적 디버그 영상(run.mp4)")
    ap.add_argument("--raw-video", action="store_true", help="원본 카메라 영상(camera.mp4) - explore.py 재생 시험용")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--start", default=None, help="시작 자세 x,y,각도(도) mm. 기본은 매트 파일의 start_pose")
    ap.add_argument("--right-first", action="store_true", help="갈림길에서 오른쪽부터 탐색")
    ap.add_argument("--speed-scale", type=float, default=None, help="실제 속도 / 설정 속도(예: 0.8 = 20%% 느림)")
    ap.add_argument("--speed-drift", type=float, default=0.0, help="1분마다 실제 속도가 줄어드는 비율(배터리 흉내)")
    ap.add_argument("--turn-slip", type=float, default=0.0,
                    help="궤도 미끄러짐으로 회전이 덜 되는 비율(예: 0.3). 맵 모양 계산 시험용")
    ap.add_argument("--patch-length", type=float, default=None,
                    help="패치 진행 방향 길이(mm). 넣으면 패치로 속도 보정. ssafy_v2 도안은 159.3")
    a = ap.parse_args()
    start = [float(v) for v in a.start.split(",")] if a.start else None
    ev, gt = run(a.mat, a.out, seed=a.seed, max_time_s=a.max_time, video=a.video, quiet=not a.verbose,
                 raw_video=a.raw_video, start_pose=start, speed_scale=a.speed_scale, speed_drift=a.speed_drift,
                 turn_slip=a.turn_slip,
                 cfg_override={"patch": {"length_mm": a.patch_length}} if a.patch_length else None,
                 branch_order=("right", "left") if a.right_first else ("left", "right"))
    print("events:", ev)
    print("finish:", gt["finish_reason"], "sim time %.1fs" % gt["sim_time_s"])


if __name__ == "__main__":
    main()
