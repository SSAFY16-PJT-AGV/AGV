# -*- coding: utf-8 -*-
"""가상 매트에서 실물과 같은 탐색 코드(agv/runner.py)를 돌려 이벤트 로그를 만든다.

    python3 sim/run_sim.py --out data/sim_run --video
    python3 mapping/build_map.py data/sim_run/events.jsonl -o maps/sim.json

산출물: events.jsonl(실물과 같은 형식), gt.json(정답 노드 위치·경로 길이·궤적),
--video면 run.mp4(카메라 시점 + 위에서 본 궤적).
"""
import argparse
import json
import math
import os
import sys

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
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
        cfg_override=None, quiet=True, max_laps=8, raw_video=False):
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
    runner = ExploreRunner(cfg, logger, mode=mode, explorer=Explorer(max_laps=max_laps),
                           speed_mm_s=base * vmax_mm_s, robot_id="sim", map_hint=mat.d["name"])
    # 실제 속도는 설정값과 조금 다르다(속도 실측 오차 재현)
    true_scale = 1.0 + speed_err * (2 * rnd.rand() - 1)
    bias = 1.0 + 0.02 * (2 * rnd.rand() - 1)
    x, y, th = mat.d["start_pose"]
    th = math.radians(th)
    traj = []
    vw = None
    if video:
        vw = cv2.VideoWriter(os.path.join(out_dir, "run.mp4"), cv2.VideoWriter_fourcc(*"mp4v"),
                             20, (320 + 480, 360))
        top_small = cv2.resize(mat.image, (480, 360))
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
        vl = left * vmax_mm_s * true_scale * (1 + slip * rnd.randn()) * bias
        vr = right * vmax_mm_s * true_scale * (1 + slip * rnd.randn()) / bias
        v = (vl + vr) / 2.0
        w = (vl - vr) / track_mm          # 왼쪽이 빠르면 오른쪽(시계방향)으로 돈다
        th -= w * dt
        x += v * math.cos(th) * dt
        y += v * math.sin(th) * dt
        t += dt
        clock.t += dt
        if int(t / dt) % 4 == 0:
            traj.append([round(x, 1), round(y, 1)])
        if vw is not None:
            dbg = draw_debug(frame, runner.last_an, runner.follow, runner.junc, runner.patch,
                             "t=%.1fs odo=%.0f" % (t, runner.odo))
            dbg = cv2.resize(dbg, (320, 240))
            top = top_small.copy()
            sx = 480.0 / mat.image.shape[1]
            for a, b in zip(traj[:-1], traj[1:]):
                pa, pb = mat.to_px(a), mat.to_px(b)
                cv2.line(top, (int(pa[0] * sx), int(pa[1] * sx)), (int(pb[0] * sx), int(pb[1] * sx)),
                         (255, 0, 0), 1)
            p = mat.to_px((x, y))
            cv2.circle(top, (int(p[0] * sx), int(p[1] * sx)), 5, (0, 0, 255), -1)
            canvas = np.full((360, 800, 3), 255, np.uint8)
            canvas[:240, :320] = dbg
            canvas[:, 320:] = top
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
          "finish_reason": runner.finish_reason, "trajectory": traj}
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
    a = ap.parse_args()
    ev, gt = run(a.mat, a.out, seed=a.seed, max_time_s=a.max_time, video=a.video, quiet=not a.verbose,
                 raw_video=a.raw_video)
    print("events:", ev)
    print("finish:", gt["finish_reason"], "sim time %.1fs" % gt["sim_time_s"])


if __name__ == "__main__":
    main()
