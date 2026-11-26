# -*- coding: utf-8 -*-
"""선반 앞에 로봇을 세워 두고 칸 인식만 확인한다(주행·서버 없이). 현장 시험 FIELD_TEST.md 10절.

    # 선반 N01 앞(스퍼 끝에 손으로 세움): 관측 자세로 10장 보고 칸 상태·선반 위치·밝기를 출력
    python3 -m agv.tools.shelfcheck --shelf N01

    # 5번(카메라 상하) 각도를 바꿔 가며 마커가 가장 잘 보이는 각도 찾기, 찾은 각도를 로봇 설정에 저장
    python3 -m agv.tools.shelfcheck --shelf N01 --sweep -45,-35,-25,-15,-5 --save-observe

    # 저장한 사진으로 노트북에서 다시(카메라·팔 없이)
    python3 -m agv.tools.shelfcheck --shelf N01 --camera "image:data/shelfcheck/<시각>/raw_*.png" --no-arm

- 카메라 해상도는 기본으로 주행과 같은 프레임(serve --tasks가 쓰는 것). --full이면 캡처 해상도(1280x720).
- 팔: 시작할 때 저장한 주행 자세에 관측 자세(perception.json observe_pose, --tilt/--pan으로 바꿈)를 덮어
  맞추고, 끝나면 주행 자세로 돌려 놓는다(--stay면 그대로).
- 결과 폴더(data/shelfcheck/<시각>/): raw_*.png(원본), debug_*.png(마커 테두리·ID와 칸 판정), report.json.
- 줄자로 잰 값과 비교: 출력의 "선반 앞 모서리 가운데"가 차체 회전 중심 기준 x(앞), y(왼쪽) mm다.
  칸 판정은 카메라 위치가 1cm 틀려도 맞지만, 이 값이 줄자와 1cm 넘게 다르면 팔로 집을 때 빗나간다
  (perception.json robot_geom을 다시 잰다).
"""
from __future__ import division, print_function

import argparse
import glob
import io
import json
import os
import sys
import time

import cv2
import numpy as np

from ..explore import ROOT, add_config_args, settings_from_args
from ..perception import aruco
from ..perception.shelf import ShelfLayout, camera_from_config, load_config, perceive_shelf
from ..task.decide import merge_views, view_from_perception
from ..task.executor import scaled_camera


class ImageCamera(object):
    """사진 파일들을 차례로(끝나면 처음부터)."""

    def __init__(self, pattern):
        self.files = sorted(glob.glob(pattern))
        if not self.files:
            raise RuntimeError(u"사진이 없어요: " + pattern)
        self.i = 0

    def read(self):
        f = self.files[self.i % len(self.files)]
        self.i += 1
        return cv2.imread(f)

    def close(self):
        pass


def brightness(img):
    g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return {"mean": round(float(g.mean()), 1), "bright_pct": round(100.0 * float((g >= 250).mean()), 1),
            "dark_pct": round(100.0 * float((g <= 5).mean()), 1)}


def debug_image(img, res, markers, layout):
    out = aruco.draw(img, markers)
    sh = res["shelf"]
    lines = [u"shelf %s" % (u"%s x=%.0f y=%.0f yaw=%.1f" % (sh["shelf_id"], sh["x_mm"], sh["y_mm"], sh["yaw_deg"])
                             if sh.get("ok") else u"not found")]
    lines.append(u" ".join(u"%d:%s" % (s["slot"], s["state"]) for s in res["slots"]))
    for i, t in enumerate(lines):
        cv2.putText(out, t, (6, 18 + 18 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)
    return out


def look(cam, n, pcfg, cmodel, angles, layout, shelf, out_dir, tag, say=print):
    """n장 보고 (합친 ShelfView, 장별 기록)."""
    for _ in range(3):
        cam.read()                  # 자세를 바꾼 뒤 버퍼에 남은 프레임 버림
    views, rows = [], []
    for i in range(n):
        img = cam.read()
        if img is None:
            continue
        cm = scaled_camera(cmodel, (img.shape[1], img.shape[0]))
        t0 = time.time()
        gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        markers = aruco.detect(gray)
        res = perceive_shelf(img, pcfg, cm, angles, layout, expect_shelf=shelf, markers=markers)
        ms = 1000.0 * (time.time() - t0)
        v = view_from_perception(res, shelf, layout)
        views.append(v)
        row = {"i": i, "ms": round(ms, 1), "markers": sorted(m.id for m in markers), "shelf": res["shelf"],
               "slots": dict((k, s.state) for k, s in v.slots.items()), "light": brightness(img)}
        rows.append(row)
        if out_dir:
            cv2.imwrite(os.path.join(out_dir, "raw_%s_%02d.png" % (tag, i)), img)
            if i < 3:
                cv2.imwrite(os.path.join(out_dir, "debug_%s_%02d.png" % (tag, i)),
                            debug_image(img, res, markers, layout))
    return (merge_views(views) if views else None), rows


def summarize(tag, merged, rows, say=print):
    seen = sum(1 for r in rows if r["shelf"].get("ok"))
    ms = [r["ms"] for r in rows]
    light = rows[0]["light"] if rows else {}
    say(u"[%s] %d장 중 선반 찾음 %d장, 판정 %.0fms(평균), 밝기 평균 %s, 하얗게 날아감 %s%%, 검게 묻힘 %s%%" % (
        tag, len(rows), seen, np.mean(ms) if ms else 0, light.get("mean"), light.get("bright_pct"),
        light.get("dark_pct")))
    ids = sorted(set(i for r in rows for i in r["markers"]))
    say(u"    본 마커 ID: %s" % (ids or u"없음"))
    if merged is not None and merged.seen:
        p = merged.pose
        say(u"    선반 앞 모서리 가운데: 앞 %.0fmm, 왼쪽 %+.0fmm, 높이 %.0fmm, 비틀림 %+.1f도  (줄자와 비교)" % (
            p["x_mm"], p["y_mm"], p["z_mm"] or 0, p["yaw_deg"]))
        say(u"    칸: %s" % u"  ".join(u"%d=%s" % (k, s.state) for k, s in sorted(merged.slots.items())))
    elif merged is not None and merged.wrong_shelf:
        say(u"    다른 선반(%s)의 칸 마커만 보여요. --shelf를 확인하세요." % merged.wrong_shelf)
    else:
        say(u"    선반 칸 마커를 못 찾았어요.")
    return seen


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_config_args(ap)
    ap.add_argument("--shelf", required=True, help="앞에 있는 선반 노드(N01~N06)")
    ap.add_argument("--camera", default="gst", help="gst | jetbot | video:<경로> | image:<사진 경로 패턴>")
    ap.add_argument("--full", action="store_true", help="캡처 해상도(1280x720)로(기본은 주행 프레임 크기)")
    ap.add_argument("--frames", type=int, default=10)
    ap.add_argument("--tilt", type=float, default=None, help="5번(카메라 상하) 각도. 기본 perception.json observe_pose")
    ap.add_argument("--pan", type=float, default=None, help="1번(받침 좌우) 각도. 기본 observe_pose")
    ap.add_argument("--sweep", default=None, help="5번 각도 여러 개(쉼표). 각도마다 보고 가장 잘 보인 각도를 알려 줌")
    ap.add_argument("--save-observe", action="store_true",
                    help="가장 잘 보인 각도를 로봇 설정(config/robots/<ID>/perception.json observe_pose)에 저장")
    ap.add_argument("--no-arm", action="store_true", help="서보를 건드리지 않음(사진 재생, 손으로 맞춘 자세)")
    ap.add_argument("--stay", action="store_true", help="끝나도 주행 자세로 돌리지 않음")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    res = settings_from_args(ap, a)
    cfg, rid = res.cfg, res.robot
    pcfg = load_config(robot=rid)
    layout = ShelfLayout(pcfg)
    if a.shelf not in layout.shelves:
        ap.error(u"선반은 %s 중 하나" % ", ".join(sorted(layout.shelves)))
    cmodel = camera_from_config(pcfg, rid)
    obs = dict((str(k), float(v)) for k, v in pcfg["observe_pose"]["servo_angles"].items())
    if a.tilt is not None:
        obs["5"] = a.tilt
    if a.pan is not None:
        obs["1"] = a.pan
    tilts = [float(v) for v in a.sweep.split(",")] if a.sweep else [obs.get("5", 0.0)]
    out = a.out or os.path.join(ROOT, "data", "shelfcheck", time.strftime("%Y%m%d-%H%M%S"))
    if not os.path.isdir(out):
        os.makedirs(out)

    arm_mod = None
    drive = None
    if not a.no_arm:
        from .. import arm as arm_mod
        drive = arm_mod.saved_pose(cfg)
    c = dict(cfg["camera"])
    if a.full:
        c["width"], c["height"] = c.get("capture_width", 1280), c.get("capture_height", 720)
    if a.camera.startswith("image:"):
        cam = ImageCamera(a.camera[len("image:"):])
    else:
        from ..hw import open_camera
        cam = open_camera(a.camera, c)
    print(u"로봇 %s, 선반 %s, 칸 마커 %s, 프레임 %s" % (rid, a.shelf, layout.shelves[a.shelf],
                                                u"캡처 해상도" if a.full else u"주행 해상도"))
    report = {"robot": rid, "shelf": a.shelf, "full": a.full, "tilts": []}
    best = None
    try:
        for tilt in tilts:
            ang = dict(obs, **{"5": tilt})
            if arm_mod is not None:
                pose = dict(drive or {})
                pose.update(ang)
                arm_mod.move(pose, settle_s=1.5)
                now = arm_mod.read_pose(("1", "5"))
                if all(now.get(k) is not None for k in ("1", "5")):
                    ang = now                       # 실제 각도로 판정(서보가 덜 갔을 수 있음)
            merged, rows = look(cam, a.frames, pcfg, cmodel, ang, layout, a.shelf, out, "t%+03.0f" % tilt)
            seen = summarize(u"1번 %+.0f도, 5번 %+.0f도" % (ang.get("1", 0), ang.get("5", 0)), merged, rows)
            n_ids = len(set(i for r in rows for i in r["markers"] if i in layout.shelves[a.shelf]))
            score = (seen, n_ids)
            report["tilts"].append({"angles": ang, "seen": seen, "slot_markers": n_ids, "frames": rows,
                                    "merged": None if merged is None else {
                                        "seen": merged.seen, "pose": merged.pose,
                                        "slots": dict((k, s.state) for k, s in merged.slots.items())}})
            if best is None or score > best[0]:
                best = (score, ang)
    finally:
        cam.close()
        if arm_mod is not None and drive and not a.stay:
            arm_mod.move(drive, settle_s=0.5)
    with io.open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=1) + u"\n")
    if len(tilts) > 1 and best is not None:
        print(u"가장 잘 보인 각도: 1번 %+.0f도, 5번 %+.0f도 (선반 찾은 장 %d, 칸 마커 %d개)" % (
            best[1].get("1", 0), best[1].get("5", 0), best[0][0], best[0][1]))
    if a.save_observe and best is not None and best[0][0] > 0 and rid:
        from ..robotcfg import update
        path = os.path.join(ROOT, "agv", "config", "robots", rid, "perception.json")
        keep = {"1": round(float(best[1].get("1", 0)), 1), "5": round(float(best[1].get("5", 0)), 1)}

        def put(user):
            user.setdefault("observe_pose", {})["servo_angles"] = keep
            user["observe_pose"]["_src"] = u"shelfcheck %s, 선반 %s" % (time.strftime("%Y-%m-%d"), a.shelf)
        update(path, put, robot=rid)
        print(u"관측 자세를 저장했어요: %s -> %s" % (keep, path))
    print(u"결과 폴더: %s" % out)
    return 0 if best is not None and best[0][0] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
