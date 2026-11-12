# -*- coding: utf-8 -*-
"""색 패치 HSV 범위와 라인 밝기 문턱을 주행 영상에서 자동으로 잡는다.

맵을 한 바퀴 도는 영상만 있으면 된다. 사람이 패치마다 사진을 찍고 HSV를 잴 필요가 없다.

    # 1) 한 바퀴 녹화: 손으로 밀거나(passive) ask 모드로 한 바퀴
    python3 -m agv.explore --mode passive --record
    # 2) 그 영상으로 색 잡기 -> 결과 표, 확인용 그림(autocolor.png), 다시 돌려 본 패치 순서
    python3 -m agv.tools.autocolor data/runs/<시각>
    # 3) 괜찮으면 config/explore.json에 쓰기(원래 파일은 .bak으로 남김)
    python3 -m agv.tools.autocolor data/runs/<시각> --write

    # 녹화 없이 카메라로 바로(그동안 로봇을 손으로 밀어 한 바퀴)
    python3 -m agv.tools.autocolor --camera gst --seconds 60

원리
- 화면 아래쪽(패치 판정 영역)에서 채도가 높은 큰 덩어리를 프레임마다 찾고, 덩어리별 색상(H) 분포를 모은다.
- 색상 히스토그램의 봉우리 = 패치 색. 이웃한 두 색의 경계는 서로 잘못 분류되는 픽셀이 가장 적은 곳에 둔다.
- 이름(red, orange, ...)은 기본 색상 순서와 가장 잘 맞게 붙인다. 조명 때문에 색이 통째로 밀려도
  무지개 순서는 그대로라서 맞게 붙는다.
- 라인 문턱(v_max)은 채도가 낮은 픽셀의 밝기를 두 무리(검은 라인 / 매트 바탕)로 나눠 그 사이에 둔다.
  패치의 채도·밝기 하한(s_min, v_min)도 바탕·라인과 패치 사이에 둔다.

결과는 반드시 autocolor.png와 "다시 돌려 본 패치 순서"로 확인한다. 매트에 없는 색이 잡혔거나
(바닥, 신발 등) 두 패치가 한 색으로 합쳐졌으면 --colors로 기대 색을 주거나 영상을 다시 찍는다.
"""
from __future__ import print_function

import argparse
import io
import itertools
import json
import os
import shutil
import sys
import time

import cv2
import numpy as np

from ..explore import HERE, load_config
from ..linecam import DEFAULT_CONFIG, PatchTracker, analyze, merge_config

H = 180   # OpenCV 색상 범위


def _range_center(ranges):
    """기본 설정의 H 범위들(빨강처럼 0/180에 걸칠 수 있음)의 원형 중심."""
    ang = []
    for lo, hi in ranges:
        for h in range(int(lo), int(hi) + 1):
            ang.append(h)
    a = np.array(ang, float) * 2 * np.pi / H
    c = np.arctan2(np.sin(a).mean(), np.cos(a).mean()) * H / (2 * np.pi)
    return c % H


def _cdist(a, b):
    d = abs(a - b) % H
    return min(d, H - d)


def _smooth_circ(hist, sigma=1.5):
    k = np.exp(-0.5 * (np.arange(-6, 7) / sigma) ** 2)
    k /= k.sum()
    ext = np.concatenate([hist[-6:], hist, hist[:6]])
    return np.convolve(ext, k, mode="same")[6:-6]


def base_stats(frames, cfg, step=5):
    """영상 전체에서 바탕(매트)과 라인의 밝기·채도를 먼저 잡는다. 화면 대부분이 매트라는 점을 쓴다."""
    lc = cfg["line"]
    vs, ss = [], []
    for f in frames[::step]:
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        h = f.shape[0]
        sub = hsv[int(lc["roi_top"] * h):int(lc["roi_bottom"] * h):3, ::3].reshape(-1, 3)
        vs.append(sub[:, 2])
        ss.append(sub[:, 1])
    v = np.concatenate(vs)
    s = np.concatenate(ss)
    thr, _ = cv2.threshold(v.astype(np.uint8).reshape(-1, 1), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bright = v >= thr
    st = {"otsu": float(thr),
          "line_v": float(np.median(v[~bright])) if (~bright).any() else 0.0,
          "mat_v": float(np.median(v[bright])) if bright.any() else 255.0,
          "mat_s": 30.0}
    if bright.any():
        # 밝은 픽셀에는 패치도 섞여 있다. 대부분은 매트이므로 중앙값 + 3 x (중앙값 절대편차)로 매트 채도 위쪽을 잡는다
        sb = s[bright].astype(float)
        med = np.median(sb)
        mad = np.median(np.abs(sb - med)) * 1.4826
        st["mat_s"] = float(med + 3 * max(mad, 2.0))
    return st


class Collector(object):
    """프레임마다 채도 높은 덩어리의 H 분포와, 바탕·라인의 밝기 표본을 모은다."""

    def __init__(self, cfg, stats, min_area_frac=0.02):
        self.cfg = cfg
        self.stats = stats
        # 바탕보다 확실히 채도 높고, 라인보다 확실히 밝은 픽셀만 패치 후보
        self.s_lo = max(45.0, stats["mat_s"] + 25)
        self.v_lo = stats["line_v"] + 0.25 * (stats["mat_v"] - stats["line_v"])
        self.min_area_frac = min_area_frac
        self.blobs = []           # dict(frame, hist(180), s, v, area, crop)
        self.n = 0

    def add(self, bgr):
        lc = self.cfg["line"]
        src = cv2.GaussianBlur(bgr, (lc["blur"], lc["blur"]), 0) if lc.get("blur") else bgr
        hsv = cv2.cvtColor(src, cv2.COLOR_BGR2HSV)
        h_img = bgr.shape[0]
        top = int(self.cfg["patch"]["region_top"] * h_img)
        reg = hsv[top:]
        hh, ss, vv = reg[:, :, 0], reg[:, :, 1], reg[:, :, 2]
        sat = ((ss >= self.s_lo) & (vv >= self.v_lo)).astype(np.uint8)
        sat = cv2.morphologyEx(sat, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(sat, 8)
        min_area = self.min_area_frac * sat.size
        for i in range(1, n):
            area = stats[i, cv2.CC_STAT_AREA]
            if area < min_area:
                continue
            m = lab == i
            hs = hh[m]
            hist = np.bincount(hs.astype(np.int64), minlength=H)[:H].astype(float)
            x, y, w, hgt = stats[i, 0], stats[i, 1], stats[i, 2], stats[i, 3]
            crop = bgr[top + y: top + y + hgt, x: x + w]
            self.blobs.append({"frame": self.n, "hist": hist, "s": np.percentile(ss[m], [5, 50]),
                               "v": np.percentile(vv[m], [5, 50]), "area": float(area) / sat.size,
                               "crop": cv2.resize(crop, (64, 48))})
        self.n += 1


def _peaks(hist, min_frac=0.02, min_sep=6):
    sm = _smooth_circ(hist)
    total = sm.sum()
    if total <= 0:
        return []
    cand = [i for i in range(H) if sm[i] >= sm[(i - 1) % H] and sm[i] > sm[(i + 1) % H]]
    cand.sort(key=lambda i: -sm[i])
    out = []
    for i in cand:
        # 봉우리 주변 질량이 충분하고 이미 고른 봉우리와 떨어져 있어야 함
        mass = sum(sm[(i + d) % H] for d in range(-4, 5))
        if mass < min_frac * total:
            continue
        if any(_cdist(i, j) < min_sep for j in out):
            continue
        out.append(i)
    return sorted(out)


def _assign_names(centers, nominal):
    """무지개 순서를 지키면서 봉우리에 이름을 붙인다. nominal: {이름: 기본 중심 H}."""
    names = sorted(nominal, key=lambda k: nominal[k])
    k = len(centers)
    if k == 0:
        return {}
    best, best_cost = None, None
    for combo in itertools.combinations(range(len(names)), min(k, len(names))):
        for rot in range(k):
            cs = centers[rot:] + centers[:rot]
            cost = sum(_cdist(c, nominal[names[j]]) ** 2 for c, j in zip(cs, combo))
            if best_cost is None or cost < best_cost:
                best_cost, best = cost, dict(zip(cs, [names[j] for j in combo]))
    return best


def _boundary(hist, a, b):
    """색상 a에서 b로(원형 증가 방향) 가는 사이에서 두 색을 가르는 경계. 그 사이 질량이 가장 적은 곳."""
    span = (b - a) % H
    sm = _smooth_circ(hist)
    best, best_v = None, None
    for d in range(1, span):
        i = (a + d) % H
        # 골짜기 중 가운데에 가까운 곳을 약간 선호
        val = sm[i] + 1e-6 * abs(d - span / 2.0)
        if best_v is None or val < best_v:
            best_v, best = val, i
    return best if best is not None else (a + span // 2) % H


def _to_ranges(lo, hi):
    """원형 구간 [lo, hi] -> OpenCV H 범위 목록(0/180에 걸치면 둘로 나눔)."""
    lo, hi = int(lo) % H, int(hi) % H
    if lo <= hi:
        return [[lo, hi]]
    return [[0, hi], [lo, H]]


def solve(col, cfg, expect=None, margin=4):
    """모은 표본으로 설정값을 계산한다. 반환: (patch/line 덮어쓸 dict, 보고용 정보)."""
    info = {"warnings": []}
    if not col.blobs:
        info["warnings"].append(u"채도 높은 덩어리를 하나도 못 찾았어요. 패치가 화면 아래쪽을 지나가게 찍었는지 확인하세요.")
        return None, info
    total = np.zeros(H)
    for b in col.blobs:
        total += b["hist"]
    peaks = _peaks(total)
    # 덩어리마다 가장 가까운 봉우리로 묶고, 여러 프레임에 나온 봉우리만 남긴다
    groups = dict((p, []) for p in peaks)
    for b in col.blobs:
        c = int(np.argmax(_smooth_circ(b["hist"])))
        p = min(peaks, key=lambda q: _cdist(q, c))
        groups[p].append(b)
    nfr = max(1, col.n)
    bg = [p for p in peaks if len(set(x["frame"] for x in groups[p])) > 0.5 * nfr]
    if bg:
        info["warnings"].append(u"거의 모든 프레임에 나오는 색(H %s)은 바탕으로 보고 뺐어요." % bg)
    peaks = [p for p in peaks if p not in bg and len(set(x["frame"] for x in groups[p])) >= 3]
    nominal = dict((k, _range_center(v)) for k, v in DEFAULT_CONFIG["patch"]["colors"].items())
    if expect:
        nominal = dict((k, nominal[k]) for k in expect if k in nominal)
    names = _assign_names(peaks, nominal)
    if len(peaks) > len(nominal):
        info["warnings"].append(u"색이 %d개 잡혔는데 이름은 %d개뿐이에요. 덜 나온 색은 버렸어요." % (len(peaks), len(nominal)))
        keep = sorted(peaks, key=lambda p: -len(groups[p]))[:len(nominal)]
        peaks = sorted(keep)
        names = _assign_names(peaks, nominal)
    missing = [k for k in nominal if k not in names.values()]
    if missing:
        info["warnings"].append(u"영상에서 못 찾은 색: %s (기존 설정을 그대로 둠)" % u", ".join(missing))
    # 각 봉우리의 H 범위: 이웃 봉우리와의 경계 + 자기 분포(1~99%) 바깥은 자름
    colors = {}
    clusters = []
    for idx, p in enumerate(peaks):
        hist = np.zeros(H)
        for b in groups[p]:
            hist += b["hist"]
        if len(peaks) > 1:
            prev_p, next_p = peaks[idx - 1], peaks[(idx + 1) % len(peaks)]
            lo_b = (_boundary(total, prev_p, p) + 1) % H
            hi_b = _boundary(total, p, next_p)
        else:
            lo_b, hi_b = (p - 20) % H, (p + 20) % H
        # 자기 분포 폭(중심에서 원형 거리 기준 99%)
        offs = np.array([((i - p + H // 2) % H) - H // 2 for i in range(H)])
        order = np.argsort(offs)
        cum = np.cumsum(hist[order]) / max(1.0, hist.sum())
        lo_own = p + offs[order][np.searchsorted(cum, 0.01)] - margin
        hi_own = p + offs[order][min(len(cum) - 1, np.searchsorted(cum, 0.99))] + margin
        lo_d = max(-((p - lo_b) % H), lo_own - p)
        hi_d = min((hi_b - p) % H, hi_own - p)
        lo, hi = (p + lo_d) % H, (p + hi_d) % H
        name = names.get(p, u"?%d" % p)
        colors[name] = _to_ranges(lo, hi)
        clusters.append({"name": name, "peak": p, "range": colors[name],
                         "frames": len(set(b["frame"] for b in groups[p])),
                         "s_med": float(np.percentile([b["s"][1] for b in groups[p]], 10)),
                         "v_med": float(np.percentile([b["v"][1] for b in groups[p]], 10)),
                         "blobs": groups[p]})
    # 라인·바탕·패치 사이 문턱. 라인은 어두운 쪽에 붙여 둔다(가장자리 흐림까지 라인으로 잡지 않게)
    st = col.stats
    line, patch = {}, {}
    lv, mv = st["line_v"], st["mat_v"]
    info["line_v"], info["mat_v"], info["mat_s"] = lv, mv, st["mat_s"]
    line["v_max"] = int(round(lv + 0.3 * (mv - lv)))
    if mv - lv < 60:
        info["warnings"].append(u"라인(%.0f)과 바탕(%.0f) 밝기 차이가 작아요. 조명을 확인하세요." % (lv, mv))
    if clusters:
        min_s = min(c["s_med"] for c in clusters)
        min_v = min(c["v_med"] for c in clusters)
        # 패치 채도 하한: 바탕 채도와 가장 흐린 패치 채도 사이
        patch["s_min"] = int(round((st["mat_s"] + min_s) / 2.0))
        line["s_max"] = patch["s_min"]
        # 채도와 상관없이 라인으로 볼 밝기: 가장 어두운 패치(보라 등)보다 확실히 아래
        line["v_dark"] = int(round(min(line["v_max"], lv + 0.3 * (min_v - lv))))
        patch["v_min"] = int(round(lv + 0.45 * (min_v - lv)))
    # 지금 값이 안전한 범위 안이면 그대로 둔다. 라인 문턱은 갈림길 판정(라인 폭)에도 영향을 주므로
    # 꼭 필요할 때만 바꾼다.
    safe = {("line", "v_max"): (lv + 0.15 * (mv - lv), lv + 0.5 * (mv - lv))}
    if clusters:
        safe[("line", "s_max")] = (st["mat_s"] - 1000, min_s - 20)
        safe[("line", "v_dark")] = (lv + 5, min(line["v_max"], min_v - 15))
        safe[("patch", "s_min")] = (st["mat_s"] + 10, min_s - 20)
        safe[("patch", "v_min")] = (lv + 10, min_v - 15)
    info["kept"] = []
    for (sec, k), (lo, hi) in safe.items():
        cur = cfg[sec].get(k)
        res = line if sec == "line" else patch
        if k in res and cur is not None and lo <= cur <= hi:
            res.pop(k)
            info["kept"].append((sec, k, cur))
        if min_s - st["mat_s"] < 30:
            info["warnings"].append(u"가장 흐린 패치 채도(%.0f)가 바탕(%.0f)과 비슷해요. 조명이 너무 노랗거나 "
                                    u"패치 색이 바랬을 수 있어요." % (min_s, st["mat_s"]))
    if colors:
        patch["colors"] = colors
    info["clusters"] = clusters
    return {"patch": patch, "line": line}, info


def swatch(info, path):
    """색마다 실제 덩어리 사진 몇 장 + 이름 + H 범위를 붙인 확인용 그림."""
    rows = []
    for c in info.get("clusters", []):
        bl = sorted(c["blobs"], key=lambda b: -b["area"])
        pick = [bl[int(i)]["crop"] for i in np.linspace(0, len(bl) - 1, min(6, len(bl)))]
        label = np.full((48, 230, 3), 255, np.uint8)
        cv2.putText(label, str(c["name"]), (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
        cv2.putText(label, "H %s" % c["range"], (6, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (60, 60, 60), 1)
        strip = np.hstack([label] + pick + [np.full((48, 64, 3), 255, np.uint8)] * (6 - len(pick)))
        rows.append(strip)
    if rows:
        cv2.imwrite(path, np.vstack(rows))


def frames_from(a, cfg):
    if a.run:
        p = a.run
        if os.path.isdir(p):
            p = os.path.join(p, "camera.mp4")
        cap = cv2.VideoCapture(p)
        if not cap.isOpened():
            raise SystemExit(u"영상을 못 열었어요: %s (주행할 때 --record를 붙였는지 확인)" % p)
        i = 0
        while True:
            ok, f = cap.read()
            if not ok:
                break
            if i % a.every == 0:
                yield f
            i += 1
        cap.release()
    else:
        from ..hw import open_camera
        if not getattr(a, "no_arm", False):
            from ..arm import apply_saved
            apply_saved(cfg)
        cam = open_camera(a.camera, cfg["camera"])
        print(u"%d초 동안 찍어요. 로봇을 라인 위로 밀어 맵을 한 바퀴 돌리세요." % a.seconds)
        t0 = time.time()
        i = 0
        try:
            while time.time() - t0 < a.seconds:
                f = cam.read()
                if f is None:
                    continue
                if i % a.every == 0:
                    yield f
                i += 1
        finally:
            cam.close()


def replay_sequence(frames, cfg):
    """새 설정으로 패치 진입 순서를 다시 뽑는다(확인용)."""
    pt = PatchTracker(cfg)
    seq = []
    for f in frames:
        ev = pt.update(analyze(f, cfg))
        if ev:
            seq.append(ev)
    return seq


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="?", help="주행 결과 폴더(camera.mp4가 있는) 또는 영상 파일")
    ap.add_argument("--camera", default=None, help="영상 대신 카메라로 바로 찍기(gst | jetbot)")
    ap.add_argument("--no-arm", action="store_true", help="--camera일 때 저장한 카메라 자세로 팔을 맞추지 않음")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--every", type=int, default=2, help="N프레임마다 하나 씀")
    ap.add_argument("--config", default=os.path.join(HERE, "config", "explore.json"))
    ap.add_argument("--colors", default=None, help="매트에 있는 색 이름(쉼표). 기본은 설정의 patch.colors 전부")
    ap.add_argument("--out", default=None, help="확인 그림·결과 JSON 폴더(기본: 주행 폴더 또는 data/autocolor)")
    ap.add_argument("--write", action="store_true", help="결과를 --config 파일에 써 넣음(원래 파일은 .bak)")
    a = ap.parse_args(argv)
    if not a.run and not a.camera:
        ap.error(u"주행 폴더(영상)나 --camera 중 하나가 필요해요")
    cfg = load_config(a.config)
    expect = [c.strip() for c in a.colors.split(",")] if a.colors else list(cfg["patch"]["colors"])
    out = a.out or (a.run if a.run and os.path.isdir(a.run) else os.path.join(os.getcwd(), "data", "autocolor"))
    if not os.path.isdir(out):
        os.makedirs(out)

    kept = list(frames_from(a, cfg))
    if not kept:
        print(u"프레임이 없어요.")
        return 1
    col = Collector(cfg, base_stats(kept, cfg))
    for f in kept:
        col.add(f)
    print(u"프레임 %d장, 색 덩어리 %d개" % (col.n, len(col.blobs)))
    res, info = solve(col, cfg, expect)
    for w in info["warnings"]:
        print(u"[주의] " + w)
    if res is None:
        return 1

    print(u"\n== 잡은 색 ==")
    print(u"  %-8s %-22s %-22s %6s %5s %5s" % (u"이름", u"새 H 범위", u"기존", u"프레임", u"S", u"V"))
    for c in info["clusters"]:
        print(u"  %-8s %-22s %-22s %6d %5.0f %5.0f" % (
            c["name"], c["range"], cfg["patch"]["colors"].get(c["name"], u"-"), c["frames"], c["s_med"], c["v_med"]))
    print(u"\n== 문턱 ==")
    for sec, k, cur in sorted(info.get("kept", [])):
        print(u"  %s.%s: %s (안전한 범위라 그대로 둠)" % (sec, k, cur))
    for sec in ("line", "patch"):
        for k, v in sorted(res[sec].items()):
            if k == "colors":
                continue
            print(u"  %s.%s: %s -> %s" % (sec, k, cfg[sec].get(k), v))
    if "line_v" in info:
        print(u"  (라인 밝기 %.0f, 바탕 밝기 %.0f, 바탕 채도 %.0f)" % (info["line_v"], info["mat_v"], info["mat_s"]))

    new_cfg = merge_config(cfg, res)
    before = replay_sequence(kept, cfg)
    after = replay_sequence(kept, new_cfg)
    print(u"\n== 이 영상에서 잡히는 패치 순서 ==")
    print(u"  기존 설정: " + (u" → ".join(before) or u"(없음)"))
    print(u"  새 설정:   " + (u" → ".join(after) or u"(없음)"))

    sw = os.path.join(out, "autocolor.png")
    swatch(info, sw)
    with io.open(os.path.join(out, "autocolor.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False, indent=1))
    print(u"\n확인 그림: %s (색 이름이 실제 패치와 맞는지 보세요)" % sw)

    if a.write:
        with io.open(a.config, encoding="utf-8") as f:
            user = json.load(f)
        bak = a.config + time.strftime(".bak-%Y%m%d-%H%M%S")
        shutil.copy(a.config, bak)
        for sec in ("line", "patch"):
            if not res[sec]:
                continue
            user.setdefault(sec, {})
            for k, v in res[sec].items():
                if k == "colors":
                    cols = dict(user[sec].get("colors") or cfg["patch"]["colors"])
                    cols.update(v)
                    user[sec]["colors"] = cols
                else:
                    user[sec][k] = v
        with io.open(a.config, "w", encoding="utf-8") as f:
            f.write(json.dumps(user, ensure_ascii=False, indent=2) + u"\n")
        print(u"%s에 썼어요(원래 파일: %s)" % (a.config, bak))
    else:
        print(u"설정에 넣으려면 --write를 붙여 다시 실행하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
