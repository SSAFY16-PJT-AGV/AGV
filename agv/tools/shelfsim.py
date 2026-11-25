# -*- coding: utf-8 -*-
"""선반 칸 판정 시뮬레이션: 가상 선반 장면을 많이 그려 perceive_shelf의 정확도를 잰다.

선반이 없어도 마커 검출기·칸 판정·좌표 계산을 끝까지 돌려 볼 수 있다. 기본값은 agv/config/perception.json.

    # 기본: 50장, 정차 오차(±20mm, ±5도)와 조명·잡음을 무작위로
    python3 -m agv.tools.shelfsim
    # 카메라 보정이 틀렸을 때: 판정 쪽이 5번 각도를 2도 틀리게, 카메라 위치를 10mm 틀리게 안다고 가정
    python3 -m agv.tools.shelfsim --tilt-err 2 --cam-pos-err 10
    # 결과 그림(장면 + 검출 + 칸 판정) 10장 저장
    python3 -m agv.tools.shelfsim --n 30 --save 10 --out data/shelfsim

보는 것
- 칸 판정: 맞음 / 틀림(다른 종류, 빈 칸을 박스로, 박스를 빈 칸으로) / 판단 보류(unknown). 틀림은 0이어야 한다.
- 박스 위치 오차(mm): 판정한 박스 가운데와 정답의 수평 거리. 그리퍼 허용 오차 안이어야 한다.
- 선반 위치 오차: 선반 앞 모서리 가운데(x, y)와 각도.

장면은 광선 추적으로 그린 이상적인 그림이다. 실제 조명·인쇄·렌즈 왜곡과 다르므로, 통과해도
실물 사진 검증(설계 문서 구현 순서 8번)은 따로 한다.
"""
from __future__ import division, print_function

import argparse
import copy
import io
import json
import math
import os
import sys
import time

import cv2
import numpy as np

from ..perception import aruco, scene
from ..perception.camera import CameraModel
from ..perception.shelf import ROOT, ShelfLayout, camera_from_config, load_config, perceive_shelf


def overlay(img, res, truth=None):
    """검출 마커 + 칸 판정을 그린 BGR 그림."""
    out = aruco.draw(img, aruco.detect(img))
    y = 24
    sh = res["shelf"]
    if sh.get("ok"):
        txt = u"shelf %s x=%.0f y=%.0f yaw=%.1f" % (sh["shelf_id"], sh["x_mm"], sh["y_mm"], sh["yaw_deg"])
    else:
        txt = "shelf: not found"
    cv2.putText(out, txt, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
    for s in res["slots"]:
        y += 24
        t = truth["slots"][s["slot"]]["state"] if truth else None
        ok = (t is None) or (t == s["state"])
        col = (0, 220, 0) if ok else ((0, 200, 255) if s["state"] == "unknown" else (0, 0, 255))
        line = "slot %d: %s" % (s["slot"], s["state"])
        if t is not None and not ok:
            line += " (truth %s)" % t
        cv2.putText(out, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2)
    return out


def perturbed(cfg, cam, rng, a):
    """판정 쪽이 알고 있는(틀릴 수 있는) 카메라 모델·위치."""
    pc = copy.deepcopy(cfg)
    g = pc["robot_geom"]
    g["tilt_offset_deg"] = float(g.get("tilt_offset_deg", 0.0)) + rng.uniform(-a.tilt_err, a.tilt_err)
    if a.cam_pos_err:
        g["camera_z_mm"] += rng.uniform(-a.cam_pos_err, a.cam_pos_err)
        g["camera_x_from_s1_mm"] += rng.uniform(-a.cam_pos_err, a.cam_pos_err)
    K = cam.K.copy()
    if a.focal_err:
        f = 1.0 + rng.uniform(-a.focal_err, a.focal_err) / 100.0
        K[0, 0] *= f
        K[1, 1] *= f
    pcam = CameraModel(cam.model, K, cam.D, cam.size)
    if a.pan_err:
        pc["_pan_err"] = rng.uniform(-a.pan_err, a.pan_err)
    return pc, pcam


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=50, help="장면 수")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--config", default=None, help="perception.json 경로(기본 agv/config/perception.json)")
    ap.add_argument("--robot", default=None, help="로봇 ID(config/robots/<ID>/의 perception.json·렌즈 보정값을 덮어 씀)")
    ap.add_argument("--stop-xy", type=float, default=20.0, help="정차 위치 오차(mm, ±)")
    ap.add_argument("--stop-yaw", type=float, default=5.0, help="정차 각도 오차(도, ±)")
    ap.add_argument("--p-empty", type=float, default=0.35, help="칸이 비어 있을 확률")
    ap.add_argument("--noise", type=float, default=3.0, help="영상 잡음 표준편차")
    ap.add_argument("--blur", type=float, default=0.7, help="흐림(가우시안 시그마 px)")
    ap.add_argument("--gain", type=float, nargs=2, default=[0.6, 1.15], help="조명 세기 범위")
    ap.add_argument("--tilt-err", type=float, default=0.0, help="판정 쪽 5번(카메라 상하) 각도 오차(도, ±)")
    ap.add_argument("--pan-err", type=float, default=0.0, help="판정 쪽 1번(받침 좌우) 각도 오차(도, ±)")
    ap.add_argument("--cam-pos-err", type=float, default=0.0, help="판정 쪽 카메라 위치 오차(mm, ±, 앞·높이)")
    ap.add_argument("--focal-err", type=float, default=0.0, help="판정 쪽 초점거리 오차(%%, ±)")
    ap.add_argument("--save", type=int, default=0, help="그림을 저장할 장면 수")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "shelfsim"))
    a = ap.parse_args(argv)

    cfg = load_config(a.config, a.robot)
    layout = ShelfLayout(cfg)
    cam = camera_from_config(cfg, a.robot)
    servo = dict(cfg["observe_pose"]["servo_angles"])
    rng = np.random.RandomState(a.seed)
    rays = cam.pixel_rays()
    if a.save:
        if not os.path.isdir(a.out):
            os.makedirs(a.out)

    counts = {"ok": 0, "wrong": 0, "unknown": 0}
    wrong_cases = []
    pos_err, yaw_err, shelf_xy, shelf_yaw, times = [], [], [], [], []
    shelf_miss = 0
    for i in range(a.n):
        pose, contents = scene.random_case(rng, cfg, layout, a.stop_xy, a.stop_yaw, a.p_empty)
        light = {"gain": rng.uniform(a.gain[0], a.gain[1]), "noise": a.noise, "blur": a.blur,
                 "gradient": rng.uniform(-0.15, 0.15)}
        img, truth = scene.render_case(cfg, cam, layout, pose, contents, servo, rng, rays=rays, light=light)
        pcfg, pcam = perturbed(cfg, cam, rng, a)
        pservo = dict(servo)
        pservo["1"] = float(servo.get("1", 0.0)) + pcfg.pop("_pan_err", 0.0)
        t0 = time.time()
        res = perceive_shelf(img, pcfg, pcam, pservo, ShelfLayout(pcfg))
        times.append(time.time() - t0)
        sh = res["shelf"]
        if sh.get("ok"):
            shelf_xy.append(math.hypot(sh["x_mm"] - pose[0], sh["y_mm"] - pose[1]))
            shelf_yaw.append(abs(sh["yaw_deg"] - pose[2]))
        else:
            shelf_miss += 1
        for k, t, g, e, ye in scene.compare(res, truth):
            if g == t:
                counts["ok"] += 1
            elif g == "unknown":
                counts["unknown"] += 1
            else:
                counts["wrong"] += 1
                wrong_cases.append((i, k, t, g))
            if e is not None:
                pos_err.append(e)
                yaw_err.append(ye)
        if i < a.save:
            cv2.imwrite(os.path.join(a.out, "scene_%03d.png" % i), overlay(img, res, truth))
            with io.open(os.path.join(a.out, "scene_%03d.json" % i), "w", encoding="utf-8") as f:
                f.write(json.dumps({"truth": {"shelf": truth["shelf"],
                                              "slots": dict((str(k), v) for k, v in truth["slots"].items())},
                                    "result": res}, ensure_ascii=False, indent=1))

    total = sum(counts.values())
    print(u"장면 %d장, 칸 %d개 (정차 오차 ±%.0fmm ±%.0f도, 카메라 모델 오차: 5번 ±%.1f도, 1번 ±%.1f도, "
          u"위치 ±%.0fmm, 초점 ±%.1f%%)" % (a.n, total, a.stop_xy, a.stop_yaw, a.tilt_err, a.pan_err,
                                          a.cam_pos_err, a.focal_err))
    print(u"칸 판정   맞음 %d / 틀림 %d / 판단 보류 %d" % (counts["ok"], counts["wrong"], counts["unknown"]))
    for i, k, t, g in wrong_cases[:10]:
        print(u"  [틀림] 장면 %d 칸 %d: 정답 %s, 판정 %s" % (i, k, t, g))
    if pos_err:
        pe = np.array(pos_err)
        print(u"박스 위치 오차(mm)  평균 %.1f, 95%% %.1f, 최대 %.1f   (박스 %d개)" % (
            pe.mean(), np.percentile(pe, 95), pe.max(), len(pe)))
        print(u"박스 방향 오차(도)  평균 %.2f, 최대 %.2f" % (np.mean(yaw_err), np.max(yaw_err)))
    if shelf_xy:
        print(u"선반 위치 오차      %.1fmm(평균) / %.1fmm(최대), 각도 %.2f도(최대)   선반 못 찾음 %d장" % (
            np.mean(shelf_xy), np.max(shelf_xy), np.max(shelf_yaw), shelf_miss))
    print(u"판정 시간          평균 %.0fms (이 컴퓨터 기준)" % (1000 * np.mean(times)))
    if a.save:
        print(u"그림: %s/scene_*.png" % a.out)
    return 0 if counts["wrong"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
