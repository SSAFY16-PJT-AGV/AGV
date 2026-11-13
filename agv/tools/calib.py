# -*- coding: utf-8 -*-
"""체커보드 사진으로 카메라 렌즈 보정값(내부 파라미터·왜곡)을 계산한다.

    # 1) 주행 자세 그대로, 체커보드를 화면 곳곳으로 옮겨 가며 25장 찍기(1.5초 간격)
    python3 -m agv.tools.camtool snap --count 25 --out data/calib
    # 2) 계산 -> 결과 표, 확인 그림(calib_check.png), data/calib/camera_calib.json
    python3 -m agv.tools.calib data/calib
    # 3) 괜찮으면 설정 폴더에 저장(agv/config/camera_calib.json)
    python3 -m agv.tools.calib data/calib --write

일반(핀홀) 모델과 어안 모델을 둘 다 계산해 재투영 오차가 작은 쪽을 고른다. 보드 크기(안쪽 모서리
가로x세로)는 흔한 크기 중에서 자동으로 찾는다. 칸 크기는 결과에 영향이 없다.

잘 찍는 법
- 보드가 평평해야 한다(두꺼운 판에 붙임). 사진마다 보드 전체가 화면 안에 들어오게.
- 화면 가운데뿐 아니라 네 귀퉁이와 가장자리까지 골고루. 어안 렌즈는 가장자리 왜곡이 커서 특히 중요.
- 정면만이 아니라 앞뒤·좌우로 30~45도 기울인 사진도 섞는다. 흔들리지 않게 잠깐 멈춘다.
"""
from __future__ import print_function

import argparse
import glob
import io
import json
import os
import shutil
import sys
import time

import cv2
import numpy as np

from ..camcalib import (CameraCalib, board_points, calib_fisheye, calib_pinhole, coverage, find_corners,
                        fov_deg, guess_board, scale_K)
from ..explore import HERE, load_config


def _parse_board(s):
    a, b = s.lower().split("x")
    return int(a), int(b)


def _snap_arm(folder):
    """camtool snap이 사진 폴더에 남긴 카메라 자세."""
    p = os.path.join(folder, "snap.json")
    if not os.path.exists(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f).get("arm")
    except (IOError, ValueError):
        return None


def run(folder, cfg, board=None, square=25.0, quiet=False):
    """반환: (결과 dict, 보고 정보). 결과는 camera_calib.json 내용."""
    say = (lambda *a: None) if quiet else print
    paths = sorted(glob.glob(os.path.join(folder, "full*.png")) + glob.glob(os.path.join(folder, "full*.jpg")))
    if not paths:
        paths = sorted(glob.glob(os.path.join(folder, "*.png")) + glob.glob(os.path.join(folder, "*.jpg")))
        paths = [p for p in paths if "calib_check" not in p]
    if not paths:
        raise SystemExit(u"사진이 없어요: %s (camtool snap --count 25 --out %s 로 찍으세요)" % (folder, folder))
    grays, sizes = [], set()
    for p in paths:
        im = cv2.imread(p)
        grays.append(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY))
        sizes.add((im.shape[1], im.shape[0]))
    if len(sizes) != 1:
        raise SystemExit(u"사진 해상도가 섞여 있어요: %s" % sorted(sizes))
    size = sizes.pop()
    if board is None:
        board = guess_board(grays)
        if board is None:
            raise SystemExit(u"체커보드를 못 찾았어요. 보드 전체가 보이는지 확인하거나 --board 9x6 처럼 안쪽 모서리 수를 주세요.")
        say(u"보드 크기(안쪽 모서리): %dx%d" % board)
    obj, imgs, names = [], [], []
    for p, g in zip(paths, grays):
        c = find_corners(g, board)
        ok = c is not None
        say(u"  %s %s" % (u"O" if ok else u"x", os.path.basename(p)))
        if ok:
            obj.append(board_points(board, square))
            imgs.append(c)
            names.append(os.path.basename(p))
    info = {"found": len(imgs), "total": len(paths), "board": board, "size": size, "warnings": []}
    if len(imgs) < 8:
        raise SystemExit(u"보드가 찍힌 사진이 %d장뿐이에요. 15장 이상 필요해요." % len(imgs))
    if len(imgs) < 15:
        info["warnings"].append(u"보드가 찍힌 사진이 %d장이에요. 15~25장이면 더 안정적이에요." % len(imgs))
    cov, empty = coverage(imgs, size)
    info["coverage"], info["empty_corners"] = cov, empty
    if cov < 0.6 or empty:
        info["warnings"].append(u"보드가 화면의 %.0f%%만 덮었어요%s. 가장자리·귀퉁이 사진을 더 찍으세요." % (
            cov * 100, (u" (빈 귀퉁이: %s)" % u", ".join(empty)) if empty else u""))

    res = {}
    for name, fn in (("pinhole", calib_pinhole), ("fisheye", calib_fisheye)):
        try:
            r = fn(obj, imgs, size)
        except cv2.error as e:
            r = None
            say(u"  %s 계산 실패: %s" % (name, str(e).splitlines()[-1][:120]))
        if r is not None:
            res[name] = r
    if not res:
        raise SystemExit(u"보정 계산이 실패했어요. 사진을 다시 찍으세요.")
    best = min(res.values(), key=lambda r: r["rms"])
    # 튀는 사진(흔들림·보드 휨)을 빼고 한 번 더
    per = np.array(best["per_view"])
    used = best.get("used", list(range(len(imgs))))
    thr = max(3.0 * float(np.median(per)), 1.0)
    bad = [used[i] for i, e in enumerate(per) if e > thr]
    if bad and len(imgs) - len(bad) >= 8:
        keep = [i for i in range(len(imgs)) if i not in bad]
        fn = calib_fisheye if best["model"] == "fisheye" else calib_pinhole
        r2 = fn([obj[i] for i in keep], [imgs[i] for i in keep], size)
        if r2 is not None:
            r2["used"] = [keep[i] for i in r2.get("used", range(len(keep)))]
            info["dropped"] = [names[i] for i in bad]
            res[best["model"]] = best = r2
    info["models"] = dict((k, v["rms"]) for k, v in res.items())
    fov = fov_deg(best["model"], best["K"], best["D"], size)
    c = cfg["camera"]
    run_size = (c.get("width", 320), c.get("height", 240))
    out = {
        "model": best["model"],
        "rms_px": round(best["rms"], 4),
        "capture_size": list(size),
        "runtime_size": list(run_size),
        "K": np.round(best["K"], 4).tolist(),
        "D": np.round(best["D"], 8).tolist(),
        "K_runtime": np.round(scale_K(best["K"], size, run_size), 4).tolist(),
        "fov_deg": [round(fov[0], 1), round(fov[1], 1)],
        "images": len(best.get("used", imgs)),
        "board": list(board),
        "camera": {"sensor_id": c.get("sensor_id", 0), "capture_width": c.get("capture_width"),
                   "capture_height": c.get("capture_height"), "flip": c.get("flip", 0)},
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "arm": _snap_arm(folder),
    }
    if out["arm"] is None:
        info["warnings"].append(u"사진을 찍을 때의 카메라 자세 기록(snap.json)이 없어요. 주행 자세와 같은 자세에서 찍었는지 "
                                u"확인하세요.")
    else:
        from ..arm import calib_pose_warning
        w = calib_pose_warning(cfg, out["arm"], u"이 사진들")
        if w:
            info["warnings"].append(w + u" (camtool arm으로 저장한 자세)")
    if (c.get("capture_width"), c.get("capture_height")) != size:
        info["warnings"].append(u"사진 해상도 %dx%d가 설정의 camera.capture %sx%s와 달라요. 주행과 같은 센서 모드로 찍어야 "
                                u"맞아요." % (size[0], size[1], c.get("capture_width"), c.get("capture_height")))
    if best["rms"] > 1.0:
        info["warnings"].append(u"재투영 오차가 %.2fpx로 커요(1px 아래가 좋음). 흔들린 사진이나 휜 보드가 없는지 보고 다시 "
                                u"찍으세요." % best["rms"])
    info["corners"], info["names"], info["paths"] = imgs, names, paths
    return out, info


def check_image(out, info, path):
    """보드 모서리를 그린 원본 | 편 사진을 나란히 놓은 확인 그림 두 줄."""
    cal = CameraCalib(out, tuple(out["capture_size"]))
    rows = []
    picks = [0, len(info["names"]) // 2] if len(info["names"]) > 1 else [0]
    for i in picks:
        p = [q for q in info["paths"] if os.path.basename(q) == info["names"][i]][0]
        im = cv2.imread(p)
        drawn = im.copy()
        cv2.drawChessboardCorners(drawn, tuple(info["board"]), info["corners"][i], True)
        und = cal.undistort_image(im, balance=0.0)
        row = np.hstack([drawn, und])
        scale = 1280.0 / row.shape[1]
        rows.append(cv2.resize(row, None, fx=scale, fy=scale))
    canvas = np.vstack(rows)
    cv2.putText(canvas, "original | undistorted (%s, rms %.2fpx)" % (out["model"], out["rms_px"]),
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    cv2.imwrite(path, canvas)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", default=os.path.join(os.getcwd(), "data", "calib"),
                    help="체커보드 사진 폴더(full_*.png)")
    ap.add_argument("--board", default=None, help="안쪽 모서리 가로x세로(예: 9x6). 기본은 자동")
    ap.add_argument("--square", type=float, default=25.0, help="칸 한 변(mm). 렌즈 보정값에는 영향 없음")
    ap.add_argument("--config", default=os.path.join(HERE, "config", "explore.json"))
    ap.add_argument("--write", action="store_true", help="agv/config/camera_calib.json에 저장(원래 파일은 .bak)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    out, info = run(a.folder, cfg, _parse_board(a.board) if a.board else None, a.square)

    print(u"\n== 결과 ==")
    print(u"  보드가 찍힌 사진 %d / %d장, 계산에 쓴 사진 %d장" % (info["found"], info["total"], out["images"]))
    if info.get("dropped"):
        print(u"  튀어서 뺀 사진: %s" % u", ".join(info["dropped"]))
    for k, v in sorted(info["models"].items()):
        print(u"  %-8s 재투영 오차 %.3f px%s" % (k, v, u"  <- 선택" if k == out["model"] else u""))
    print(u"  화각: 가로 %.0f도, 세로 %.0f도" % tuple(out["fov_deg"]))
    print(u"  화면 덮은 비율: %.0f%%" % (info["coverage"] * 100))
    print(u"  K(%dx%d): fx=%.1f fy=%.1f cx=%.1f cy=%.1f" % (
        out["capture_size"][0], out["capture_size"][1], out["K"][0][0], out["K"][1][1], out["K"][0][2], out["K"][1][2]))
    print(u"  D: %s" % [round(x, 4) for x in out["D"]])
    for w in info["warnings"]:
        print(u"[주의] " + w)

    res_path = os.path.join(a.folder, "camera_calib.json")
    with io.open(res_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=1))
    chk = os.path.join(a.folder, "calib_check.png")
    check_image(out, info, chk)
    print(u"\n확인 그림: %s (오른쪽 사진에서 보드·매트의 곧은 선이 곧게 펴졌는지 보세요)" % chk)
    print(u"결과: %s" % res_path)
    if a.write:
        dst = os.path.join(HERE, "config", "camera_calib.json")
        if os.path.exists(dst):
            bak = dst + time.strftime(".bak-%Y%m%d-%H%M%S")
            shutil.copy(dst, bak)
            print(u"원래 파일: %s" % bak)
        shutil.copy(res_path, dst)
        print(u"%s에 저장했어요." % dst)
    else:
        print(u"설정 폴더에 넣으려면 --write를 붙여 다시 실행하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
