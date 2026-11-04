# -*- coding: utf-8 -*-
"""카메라 한 프레임에서 라인·색 패치·갈림길을 찾고, 라인을 따라가는 조향값을 낸다.

전제: 카메라가 차체 앞 바닥을 비스듬히 내려다본다(화면 아래 = 가까움, 위 = 멂).
좌표는 화면 픽셀 기준이다. 버드아이뷰·캘리브레이션 없이 동작하도록 만들었다.

- 라인: HSV에서 어둡고(V 낮음) 채도 낮은(S 낮음) 픽셀. 색 패치 위에 인쇄된 검은 선도 잡힌다.
- 갈림길: 조향 줄(control row, 차체 바로 앞)에서 라인 조각 수가 바뀌는 순간을 본다.
    1개 -> 2개로 바뀔 때 두 조각이 붙어 있었으면 split(방금 갈라짐)
    2개 -> 1개로 바뀔 때 두 조각이 붙어 있었으면 merge(방금 합쳐짐)
    떨어진 채로 바뀌면 다른 가지가 화면 옆으로 들어오거나 나간 것이라 무시한다.
  짧은 분기(갈라짐과 합쳐짐이 한 화면에 다 보이는 경우)도 순서대로 두 이벤트가 나온다.
  갈 쪽은 미리 정해야 하므로, 위쪽 줄에서 "아래 1개 + 위 2개" 갈래 모양이 보이면
  fork_ahead를 켠다.
- 색 패치: 화면 아래쪽 영역에서 색 비율이 임계값을 넘으면 진입 이벤트.

Python 3.6 + numpy + OpenCV(Jetson 기본 이미지).
"""
import copy

import cv2
import numpy as np

DEFAULT_CONFIG = {
    "frame": {"width": 320, "height": 240, "flip": 0},
    "line": {
        "v_max": 80,            # 이보다 어두우면 라인 후보
        "s_max": 90,            # 이보다 채도가 높으면 색 패치로 보고 제외
        "v_dark": 55,           # 이보다 어두우면 채도와 상관없이 라인(어두운 픽셀은 S가 노이즈로 튐)
        "blur": 5,              # 가우시안 블러 커널(0이면 끔)
        "roi_top": 0.30,        # 스캔 영역(화면 높이 비율)
        "roi_bottom": 0.96,
        "rows": 14,             # 스캔 줄 수
        "control_row": 0.80,    # 조향에 쓰는 줄 위치
        "min_w_frac": 0.02,     # 라인 조각 최소/최대 폭(화면 폭 비율)
        "max_w_frac": 0.60,
        "gap_close_px": 4,      # 이보다 좁은 끊김은 이어 붙임
    },
    "junction": {
        "confirm_frames": 3,    # 조각 수 변화가 이만큼 이어져야 인정
        "join_k": 1.7,          # 두 조각 중심 간격 < join_k x 라인폭 이면 "붙어 있음"(합류 판정)
        "split_sure_k": 1.5,    # 1->2 순간 간격이 이보다 좁으면 무조건 split
        "split_max_k": 2.6,     # 이 사이면 간격이 벌어지는지(split) 좁아지는지(옆 선 진입)로 판단
        "split_trend_px": 1.0,  # 확인 프레임 동안 간격이 이만큼 이상 벌어져야 split
        "fork_frames": 2,       # 앞쪽 갈래 모양이 이만큼 이어지면 fork_ahead
        "fork_gap_k": 3.0,      # 갈래 모양의 위쪽 두 조각 간격 상한(라인폭 배수)
    },
    "patch": {
        "region_top": 0.55,     # 패치 판정 영역(화면 아래쪽)
        "s_min": 90, "v_min": 50,
        "on_frac": 0.20, "off_frac": 0.06,
        "on_frames": 3, "off_frames": 5,
        # OpenCV HSV의 H 범위(0~180). 실제 조명에서 tools/hsv_stats.py로 재서 고칠 것
        "colors": {
            "orange": [[0, 18]],
            "yellow": [[19, 38]],
            "green": [[40, 90]],
            "purple": [[115, 165]],
        },
    },
    "follow": {
        "base_speed": 0.30,     # jetbot Robot.set_motors 단위(-1~1)
        "kp": 0.35, "kd": 0.04,
        "slow_k": 0.5,          # 오차가 크면 감속
        "max_cmd": 0.6,
        "lost_timeout_s": 1.0,  # 라인을 이만큼 못 보면 정지
    },
}


def merge_config(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_config(out[k], v)
        else:
            out[k] = v
    return out


def _runs(row_bool, close_px):
    """1차원 bool 배열에서 True 구간 [(x0, x1)] (x1 포함)."""
    r = row_bool.astype(np.int8)
    d = np.diff(np.concatenate(([0], r, [0])))
    starts = np.where(d == 1)[0]
    ends = np.where(d == -1)[0] - 1
    runs = []
    for s, e in zip(starts, ends):
        if runs and s - runs[-1][1] - 1 <= close_px:
            runs[-1] = (runs[-1][0], int(e))
        else:
            runs.append((int(s), int(e)))
    return runs


class Analysis(object):
    """한 프레임 분석 결과."""

    def __init__(self):
        self.w = self.h = 0
        self.rows = []          # [(y, [xc, ...]), ...] 위(먼 곳)부터
        self.widths = []        # rows와 같은 순서의 조각 폭 [[w, ...], ...]
        self.ctrl_idx = 0
        self.patch_fracs = {}
        self.line_mask = None


def analyze(bgr, cfg):
    h, w = bgr.shape[:2]
    lc = cfg["line"]
    src = cv2.GaussianBlur(bgr, (lc["blur"], lc["blur"]), 0) if lc.get("blur") else bgr
    hsv = cv2.cvtColor(src, cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1], hsv[:, :, 2]
    mask = ((v < lc["v_max"]) & ((s < lc["s_max"]) | (v < lc["v_dark"]))).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    an = Analysis()
    an.w, an.h = w, h
    an.line_mask = mask
    ys = np.linspace(lc["roi_top"] * h, lc["roi_bottom"] * h, lc["rows"]).astype(int)
    min_w, max_w = lc["min_w_frac"] * w, lc["max_w_frac"] * w
    for y in ys:
        y0, y1 = max(0, y - 1), min(h, y + 2)
        band = mask[y0:y1].mean(axis=0) > 0.5
        runs = [(a, b) for a, b in _runs(band, lc["gap_close_px"])
                if min_w <= (b - a + 1) <= max_w]
        an.rows.append((int(y), [(a + b) / 2.0 for a, b in runs]))
        an.widths.append([float(b - a + 1) for a, b in runs])
    an.ctrl_idx = int(np.argmin(np.abs(ys - lc["control_row"] * h)))

    pc = cfg["patch"]
    top = int(pc["region_top"] * h)
    hh, ss, vv = hsv[top:, :, 0], hsv[top:, :, 1], hsv[top:, :, 2]
    colored = (ss >= pc["s_min"]) & (vv >= pc["v_min"])
    area = float(colored.size)
    for name, ranges in pc["colors"].items():
        m = np.zeros_like(colored)
        for lo, hi in ranges:
            m |= (hh >= lo) & (hh <= hi)
        an.patch_fracs[name] = float((m & colored).sum()) / area
    return an


class PatchTracker(object):
    """색 패치 진입을 히스테리시스로 한 번만 잡는다."""

    def __init__(self, cfg):
        self.c = cfg["patch"]
        self.active = None
        self._cand = None
        self._cand_n = 0
        self._off_n = 0

    def update(self, an):
        fr = an.patch_fracs
        best = max(fr, key=fr.get) if fr else None
        bf = fr.get(best, 0.0)
        event = None
        if best is not None and bf >= self.c["on_frac"] and best != self.active:
            if self._cand == best:
                self._cand_n += 1
            else:
                self._cand, self._cand_n = best, 1
            if self._cand_n >= self.c["on_frames"]:
                self.active = best
                event = best
                self._off_n = 0
        else:
            self._cand, self._cand_n = None, 0
        if self.active is not None:
            if fr.get(self.active, 0.0) < self.c["off_frac"]:
                self._off_n += 1
                if self._off_n >= self.c["off_frames"]:
                    self.active = None
            else:
                self._off_n = 0
        return event


class JunctionTracker(object):
    """조향 줄의 라인 조각 수 변화로 split/merge를 판정한다."""

    def __init__(self, cfg):
        self.c = cfg["junction"]
        self.lw = None              # 조향 줄에서 라인 폭(px) 추정
        self.state = "one"          # 조향 줄: one | two
        self._pend = 0
        self._pend_gap = None
        self._pend_gaps = []
        self._last_gap = None
        self.last_onset = None
        self.just_changed = False   # 이번 프레임에 조각 수 상태가 바뀜(confirm_frames 늦게 확정됨)
        self._fork_n = 0
        self.fork_ahead = False     # 앞에 갈래(split)가 보임 -> 갈 쪽을 미리 정할 때
        self.in_split = False       # 갈라진 직후 두 가지 사이를 지나는 중
        self.side = {"left": 0, "right": 0}

    @property
    def active(self):
        return self.state == "two" or self.fork_ahead

    def _pick_two(self, segs, w):
        if len(segs) == 2:
            return segs
        two = sorted(sorted(segs, key=lambda s: abs(s - w / 2.0))[:2])
        return two

    def _fork(self, an):
        k = self.c["fork_gap_k"]
        for i in range(an.ctrl_idx):
            up, lo = an.rows[i][1], an.rows[i + 1][1]
            if len(up) == 2 and len(lo) == 1:
                lw = an.widths[i + 1][0]
                if self.lw is not None:
                    lw = min(lw, 2.2 * self.lw)
                l, r = up
                if l - 0.5 * lw <= lo[0] <= r + 0.5 * lw and (r - l) <= k * max(lw, 1.0):
                    return True
        return False

    def update(self, an, tracked_x=None):
        """프레임마다 호출. 반환: 이벤트 목록 [{"type": split|merge, "side": left|right|None}]
        side는 내가 지난(들어온) 쪽 가지."""
        events = []
        self.just_changed = False
        segs = an.rows[an.ctrl_idx][1]
        ws = an.widths[an.ctrl_idx]
        if len(segs) == 1 and (self.lw is None or ws[0] < 1.5 * self.lw):
            self.lw = ws[0] if self.lw is None else 0.9 * self.lw + 0.1 * ws[0]
        obs, gap = None, None
        if len(segs) == 1:
            obs = "one"
        elif len(segs) >= 2:
            obs = "two"
            l, r = self._pick_two(segs, an.w)
            gap = r - l
        join = self.c["join_k"] * (self.lw or 0.06 * an.w)

        if self.state == "two" and obs == "two":
            l, r = self._pick_two(segs, an.w)
            ref = an.w / 2.0
            self.side["left" if abs(l - ref) <= abs(r - ref) else "right"] += 1
        if obs is not None and obs != self.state:
            self._pend += 1
            if self._pend == 1:
                self._pend_gap = gap
                self._pend_gaps = []
            if gap is not None:
                self._pend_gaps.append(gap)
            if self._pend >= self.c["confirm_frames"]:
                self.just_changed = True
                if obs == "two":
                    lw = self.lw or 0.06 * an.w
                    g0 = self._pend_gap
                    trend = self._pend_gaps[-1] - self._pend_gaps[0] if self._pend_gaps else 0.0
                    self.last_onset = {"ratio": round(g0 / lw, 2), "trend": round(trend, 1)}
                    if g0 < self.c["split_sure_k"] * lw:
                        self.in_split = True
                    elif g0 < self.c["split_max_k"] * lw:
                        self.in_split = trend >= self.c["split_trend_px"]
                    else:
                        self.in_split = False
                    self.side = {"left": 0, "right": 0}
                else:
                    side = None
                    if self.side["left"] != self.side["right"]:
                        side = "left" if self.side["left"] > self.side["right"] else "right"
                    if self.in_split:
                        events.append({"type": "split", "side": side})
                    if self._last_gap is not None and self._last_gap < join:
                        events.append({"type": "merge", "side": side})
                    self.in_split = False
                self.state = obs
                self._pend = 0
        elif obs is not None:
            self._pend = 0
            if obs == "two":
                self._last_gap = gap

        if self._fork(an):
            self._fork_n += 1
        else:
            self._fork_n = 0
        self.fork_ahead = self._fork_n >= self.c["fork_frames"]
        return events


class Follower(object):
    """라인 위 목표점을 고르고 좌우 모터 명령을 만든다."""

    def __init__(self, cfg):
        self.c = cfg["follow"]
        self.last_x = None
        self.prev_err = 0.0

    def target(self, an, choice=None):
        """조향 목표 x(px). choice가 left/right면 갈림길에서 그쪽 가지를 택한다."""
        if self.last_x is None:
            self.last_x = an.w / 2.0
        ctrl_segs = an.rows[an.ctrl_idx][1]
        x = None
        if choice in ("left", "right") and len(ctrl_segs) >= 2:
            x = ctrl_segs[0] if choice == "left" else ctrl_segs[-1]
        if x is None:
            # 조향 줄 -> 그보다 가까운 줄 -> 먼 줄 순서로 찾는다
            order = [an.ctrl_idx] + list(range(an.ctrl_idx + 1, len(an.rows))) \
                + list(range(an.ctrl_idx - 1, -1, -1))
            for i in order:
                cand = an.rows[i][1]
                if cand:
                    x = min(cand, key=lambda s: abs(s - self.last_x))
                    break
        if x is not None:
            self.last_x = float(x)
        return x

    def command(self, x, w, dt):
        if x is None:
            return 0.0, 0.0
        err = (x - w / 2.0) / (w / 2.0)
        d = (err - self.prev_err) / max(dt, 1e-3)
        self.prev_err = err
        turn = self.c["kp"] * err + self.c["kd"] * d
        base = self.c["base_speed"] * max(0.4, 1.0 - self.c["slow_k"] * abs(err))
        m = self.c["max_cmd"]
        left = max(-m, min(m, base + turn))
        right = max(-m, min(m, base - turn))
        return left, right


def draw_debug(bgr, an, follower=None, junction=None, patch=None, text=None):
    """검출 결과를 그린 디버그 이미지."""
    img = bgr.copy()
    for i, (y, segs) in enumerate(an.rows):
        col = (0, 200, 255) if len(segs) == 2 else (0, 255, 0)
        if i == an.ctrl_idx:
            cv2.line(img, (0, y), (an.w, y), (255, 120, 0), 1)
        for xs in segs:
            cv2.circle(img, (int(xs), y), 3, col, -1)
    if follower is not None and follower.last_x is not None:
        y = an.rows[an.ctrl_idx][0]
        cv2.drawMarker(img, (int(follower.last_x), y), (0, 0, 255), cv2.MARKER_CROSS, 14, 2)
    lines = []
    if patch is not None and patch.active:
        lines.append("patch:" + patch.active)
    if junction is not None:
        lines.append("ctrl:%s%s%s" % (junction.state, " FORK" if junction.fork_ahead else "",
                                       " in_split" if junction.in_split else ""))
    if text:
        lines.append(text)
    for i, t in enumerate(lines):
        cv2.putText(img, t, (4, 14 + 14 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 3)
        cv2.putText(img, t, (4, 14 + 14 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1)
    return img
