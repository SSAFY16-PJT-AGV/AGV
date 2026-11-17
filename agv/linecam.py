# -*- coding: utf-8 -*-
"""카메라 한 프레임에서 라인·색 패치·갈림길을 찾고, 라인을 따라가는 조향값을 낸다.

전제: 카메라가 차체 앞 바닥을 비스듬히 내려다본다(화면 아래 = 가까움, 위 = 멂).
좌표는 화면 픽셀 기준이다. 버드아이뷰·캘리브레이션 없이 동작하도록 만들었다.

- 라인: HSV에서 어둡고(V 낮음) 채도 낮은(S 낮음) 픽셀. 색 패치 위에 인쇄된 검은 선도 잡힌다.
- 갈림길: 조향 줄(control row, 차체 바로 앞)에서 라인 조각 수가 바뀌는 순간을 본다.
    1개 -> 2개로 바뀔 때 두 조각이 붙어 있었으면 split(방금 갈라짐)
    2개 -> 1개로 바뀔 때 두 조각이 붙어 있었거나 빠르게 좁혀지던 중이면 merge(방금 합쳐짐)
    떨어진 채로 바뀌면 다른 가지가 화면 옆으로 들어오거나 나간 것이라 무시한다.
  짧은 분기(갈라짐과 합쳐짐이 한 화면에 다 보이는 경우)도 순서대로 두 이벤트가 나온다.
  1 -> 2가 될 때마다 두 조각을 위쪽 줄로 따라가 어느 쪽이 비스듬한지(slanted) 기록한다.
  X자 교차는 "merge 직후, merge 때 다른 선이 들어온 쪽의 반대편으로 비스듬한 선이 갈라짐"이라
  runner가 이 정보로 교차와 진짜 갈림길을 구분한다.
- 스퍼(T자): 조향 줄에서 라인 조각이 한쪽으로만 아주 넓어지면(옆으로 뻗은 선이 지나가는 중)
  그쪽에 스퍼가 있다고 본다(TeeTracker). 라인 추종은 넓은 조각의 내 선 쪽 가장자리를 쓴다.
- 급한 가지(V자): 모서리 곡선 위에서 급하게 꺾여 나가는 가지는 조향 줄에서 갈라지는 모습이
  보이지 않고, 옆에서 들어와 내 선과 만나는 선(합류와 같은 모양)으로 보인다. 매끄러운 합류는
  접선으로 붙어서 간격이 천천히 준다. 옆에서 가파르게 들어와 vee_max_frames 안에 붙었는데,
  붙은 뒤 내 선이 그 선 쪽으로 휘어 나가면 "vee"(그쪽에 급한 가지), 반대쪽으로 휘면 옆에서 붙는
  T자 합류라서 무시한다. "vee"가 나는 순간 그 만나는 점(J)이 조향 줄에 있다.
- 색 패치: 화면 아래쪽 영역에서 색 비율이 임계값을 넘으면 진입 이벤트.

Python 3.6 + numpy + OpenCV(Jetson 기본 이미지).
"""
import copy

import cv2
import numpy as np

DEFAULT_CONFIG = {
    "frame": {"width": 320, "height": 240, "flip": 0},
    "line": {
        "v_auto": True,         # 프레임마다 테이프·매트 밝기를 재서 그 사이에 문턱을 둔다. 조명이 바뀌어도 따라간다
                                # (실물: 어두운 방 매트 108·테이프 20, 밝은 곳 매트 210·테이프 90). false면 v_max·v_dark 고정값
        "v_split": 0.25,        # 문턱 = 테이프 밝기 + 이 비율 x (매트 - 테이프)
        "v_dark_split": 0.13,   # 채도와 상관없이 라인으로 볼 밝기도 같은 식
        "v_max": 80,            # (v_rel이 null일 때) 이보다 어두우면 라인 후보
        "s_max": 90,            # 이보다 채도가 높으면 색 패치로 보고 제외
        "v_dark": 55,           # (v_rel이 null일 때) 이보다 어두우면 채도와 상관없이 라인(어두운 픽셀은 S가 노이즈로 튐)
        "blur": 5,              # 가우시안 블러 커널(0이면 끔)
        "roi_top": 0.30,        # 스캔 영역(화면 높이 비율)
        "roi_bottom": 0.96,
        "rows": 14,             # 스캔 줄 수
        "control_row": 0.80,    # 조향에 쓰는 줄 위치
        "min_w_frac": 0.02,     # 라인 조각 최소/최대 폭(화면 폭 비율)
        "max_w_frac": 0.60,
        "gap_close_px": 4,      # 이보다 좁은 끊김은 이어 붙임
        "border_drop_frac": 0.6,  # 화면 왼/오른쪽 끝에 붙은 어두운 조각이 스캔 줄의 이 비율 이상에 있고 그 줄들에
                                  # 다른 라인 조각도 보이면 매트 밖 바닥으로 보고 뺀다(0이면 끔)
    },
    "junction": {
        "confirm_frames": 3,    # 조각 수 변화가 이만큼 이어져야 인정
        "join_k": 1.7,          # 두 조각 중심 간격 < join_k x 라인폭 이면 "붙어 있음"(합류 판정)
        "split_sure_k": 1.5,    # 1->2 순간 간격이 이보다 좁으면 무조건 split
        "split_max_k": 2.6,     # 이 사이면 간격이 벌어지는지(split) 좁아지는지(옆 선 진입)로 판단
        "split_trend_px": 1.0,  # 확인 프레임 동안 간격이 이만큼 이상 벌어져야 split
        "merge_conv_k": 3.2,    # 빠르게 좁혀지던 두 조각은 간격이 이 배수 안이면 합류로 본다(가파른 교차용)
        "slant_rows": 4,        # 1->2 순간 각 조각을 위로 따라가 기울기를 잴 줄 수
        "slant_min_px": 3.0,    # 줄당 x 변화 차이가 이보다 작으면 어느 쪽이 비스듬한지 모름
        "fork_frames": 2,       # 앞쪽 갈래 모양이 이만큼 이어지면 fork_ahead
        "fork_gap_k": 3.0,      # 갈래 모양의 위쪽 두 조각 간격 상한(라인폭 배수)
        "cross_window_mm": 300, # merge 후 이 거리 안에서 반대편으로 갈라지면 X자 교차
        "vee_max_frames": 5,    # 옆에서 들어온 선이 이 프레임 수 안에 붙으면 급한 가지(V자)
        "vee_cont_px": 4.0,     # 그리고 붙은 뒤 내 선이 그 선 쪽으로 이만큼(줄당 px) 휘어 나가야 V자
    },
    "tee": {
        "width_k": 3.0,         # 조향 줄 조각 폭이 라인폭의 이 배수보다 넓고
        "ext_k": 1.8,           # 한쪽으로 라인폭의 이 배수 이상 뻗고
        "other_k": 1.0,         # 반대쪽은 이 배수 이하일 때 스퍼
        "confirm_frames": 2,
        "rearm_frames": 4,
        "clear_rows": 3,        # 조향 줄에서 이만큼 위·아래 줄은 보통 폭 선 하나여야 함(스퍼 띠는 얇다)
    },
    "geom": {
        # 차체 회전 중심에서 각 판정 지점까지 바닥 거리(mm). 이벤트 위치(odo_mm)를 실제 지점으로 맞춘다.
        # ctrl_ahead_mm: camtool snap의 debug.png 파란 가로줄(조향 줄)이 바닥에서 차체 앞 몇 mm인지 잰 값
        # patch_ahead_mm: 패치 진입 이벤트가 날 때 패치 앞 가장자리까지 거리(대략 조향 줄과 비슷)
        "ctrl_ahead_mm": 116,
        "patch_ahead_mm": 125,
    },
    "speedcal": {
        # 패치가 화면 가로줄을 지나가는 동안(앞 가장자리~뒤 가장자리) 모터 명령 적분이 얼마였는지로
        # "명령 1당 실제 mm"를 다시 잰다. patch.length_mm가 있어야 동작한다. agv/speedcal.py 참고
        "rows": 10, "row_top": 0.32, "row_bottom": 0.95,   # 재는 가로줄(화면 높이 비율)
        "on": 0.10, "off": 0.05, "end_frames": 2,          # 패치가 보이기 시작/끝 판정(줄의 색 비율)
        "min_peak": 0.15,       # 줄마다 패치가 이 비율 이상 덮어야 그 줄을 씀
        "min_rows": 4,          # 쓸 수 있는 줄이 이만큼 있어야 한 번의 측정으로 침
        "max_spread": 0.08,     # 줄마다 잰 값의 사분위 폭 / 중앙값이 이보다 크면 버림
        "turn_max": 0.20,       # 지나는 동안 |좌-우|/(좌+우) 평균이 이보다 크면(곡선) 버림
        "alpha": 0.4,           # 새 측정을 섞는 비율(첫 측정은 그대로 씀)
        "max_jump": 0.35,       # 지금 값과 이 비율 넘게 다르면 이상치로 버림
        "max_frames": 300,
    },
    "patch": {
        "region_top": 0.55,     # 패치 판정 영역(화면 아래쪽)
        "length_mm": None,      # 패치의 진행 방향 길이(전부 같은 크기). 넣으면 지날 때마다 속도를 보정(speedcal)
        "s_min": 90, "v_min": 50,
        "on_frac": 0.20, "off_frac": 0.06,
        "on_frames": 3, "off_frames": 5,
        # OpenCV HSV의 H 범위(0~180). 실제 조명에서 tools/hsv_stats.py로 재서 고칠 것
        "colors": {
            "red": [[0, 6], [170, 180]],
            "orange": [[7, 18]],
            "yellow": [[19, 38]],
            "green": [[40, 90]],
            "blue": [[95, 124]],
            "purple": [[125, 169]],
        },
    },
    "follow": {
        "base_speed": 0.30,     # jetbot Robot.set_motors 단위(-1~1)
        "kp": 0.35, "kd": 0.04,
        "slow_k": 0.5,          # 오차가 크면 감속
        "max_cmd": 0.6,
        "lost_timeout_s": 1.0,  # 라인을 이만큼 못 보면 정지
        "jump_frac": 0.3,       # 화면 옆 끝에 붙은 조각이 마지막 라인 위치에서 화면 폭의 이 비율보다 멀면
                                # 라인이 아니라 매트 밖 바닥·그림자로 보고 무시
        "max_jump_frac": 0.5,   # 옆 끝에 안 붙은 조각도 이 비율보다 멀면 무시(먼 줄에 보이는 매트 밖 바닥)
        "edge_frac": 0.3,       # 라인을 놓치기 직전 위치가 화면 왼/오른쪽 이 비율 안이면 급커브로 화면 밖에
                                # 나간 것: 그쪽으로 제자리 회전하며 찾는다
        "search_timeout_s": 4.0,  # 그렇게 찾는 최대 시간(그 뒤 정지)
        "min_track": 0.0,       # 라인을 따라갈 때 안쪽 궤도 명령의 하한(0이면 끔). 0.2쯤 주면 양쪽이 같이 밀며 크게 돈다
        "track_jump_frac": 0.2,   # 바로 앞 프레임에 라인을 봤으면, 화면 폭의 이 비율보다 먼 조각으로는 안 넘어감
        "min_w_k": 0.3,           # 라인 폭(조향 줄)의 이 배보다 가는 조각은 라인이 아님(패치 가장자리의 가는 어두운 띠 등)
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
        self.raw = []           # rows와 같은 순서, 폭 필터 전 조각 [(x0, x1), ...] (스퍼 판정용)
        self.raw_all = []       # raw와 같되 매트 밖 바닥으로 뺀 조각도 남김(라인 추종용: 바닥에 붙은 선도 따라감)
        self.ctrl_idx = 0
        self.patch_fracs = {}
        self.line_mask = None
        self.sat_rows = []      # 속도 보정용: 정해진 가로줄마다 채도 높은(패치) 픽셀 비율
        self.dropped_sides = set()  # 매트 밖 바닥으로 보고 뺀 화면 쪽(left/right)
        self.mat_v = None           # 이 프레임의 매트 밝기 추정
        self.v_max = None           # 이 프레임에 쓴 라인 밝기 문턱
        self.v_dark = None
        self.line_v = None


def line_thresholds(v, lc):
    """(v_max, v_dark, mat_v, line_v). v_auto면 이 프레임의 스캔 영역 밝기에서 매트(70% 지점)와 테이프(3% 지점,
    테이프는 화면 폭의 1할쯤이라 가장 어두운 쪽)를 재서 그 사이에 둔다. 테이프가 안 보여 둘 차이가 작으면
    매트 밝기의 35%로 둔다(매트·패치를 라인으로 잡지 않게). 아니면 설정 고정값."""
    if not lc.get("v_auto"):
        return lc["v_max"], lc["v_dark"], None, None
    h = v.shape[0]
    sub = v[int(lc["roi_top"] * h):int(lc["roi_bottom"] * h):2, ::2]
    line_v, mat_v = [float(x) for x in np.percentile(sub, [3, 70])]
    if mat_v - line_v < 0.4 * mat_v:
        return 0.35 * mat_v, 0.2 * mat_v, mat_v, None
    k, kd = lc.get("v_split", 0.25), lc.get("v_dark_split", 0.13)
    return line_v + k * (mat_v - line_v), line_v + kd * (mat_v - line_v), mat_v, line_v


def _offmat_sides(per_row, w, frac):
    """매트 가장자리 밖 바닥이 화면 옆에 보이는지. 그 쪽 끝에 붙은 어두운 조각이 스캔 줄의 frac 이상에 있고,
    그 줄들의 절반 이상에 끝에 안 붙은 조각(진짜 라인)도 같이 보이면 그 쪽을 뺀다.
    라인이 급히 휘어 화면 옆으로 나갈 때는 다른 조각이 없으므로 빼지 않는다. 반환: {"left", "right"} 부분집합."""
    out = set()
    if not frac or not per_row:
        return out
    for side in ("left", "right"):
        touch = 0
        with_other = 0
        for runs in per_row:
            at = [r for r in runs if (r[0] <= 1 if side == "left" else r[1] >= w - 2)]
            if at:
                touch += 1
                if len(runs) > len(at):
                    with_other += 1
        if touch >= frac * len(per_row) and with_other >= 0.5 * touch:
            out.add(side)
    return out


def analyze(bgr, cfg):
    h, w = bgr.shape[:2]
    lc = cfg["line"]
    src = cv2.GaussianBlur(bgr, (lc["blur"], lc["blur"]), 0) if lc.get("blur") else bgr
    hsv = cv2.cvtColor(src, cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1], hsv[:, :, 2]
    v_max, v_dark, mat_v, line_v = line_thresholds(v, lc)
    mask = ((v < v_max) & ((s < lc["s_max"]) | (v < v_dark))).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    an = Analysis()
    an.w, an.h = w, h
    an.mat_v, an.v_max, an.v_dark, an.line_v = mat_v, v_max, v_dark, line_v
    an.line_mask = mask
    ys = np.linspace(lc["roi_top"] * h, lc["roi_bottom"] * h, lc["rows"]).astype(int)
    min_w, max_w = lc["min_w_frac"] * w, lc["max_w_frac"] * w
    per_row = []
    for y in ys:
        y0, y1 = max(0, y - 1), min(h, y + 2)
        band = mask[y0:y1].mean(axis=0) > 0.5
        per_row.append([(a, b) for a, b in _runs(band, lc["gap_close_px"]) if (b - a + 1) >= min_w])
    drop = _offmat_sides(per_row, w, lc.get("border_drop_frac", 0.0))
    an.dropped_sides = drop
    for y, allruns in zip(ys, per_row):
        if drop:
            allruns = [(a, b) for a, b in allruns
                       if not (("left" in drop and a <= 1) or ("right" in drop and b >= w - 2))]
        runs = [(a, b) for a, b in allruns if (b - a + 1) <= max_w]
        an.rows.append((int(y), [(a + b) / 2.0 for a, b in runs]))
        an.widths.append([float(b - a + 1) for a, b in runs])
        an.raw.append(allruns)
    an.raw_all = per_row
    an.ctrl_idx = int(np.argmin(np.abs(ys - lc["control_row"] * h)))

    pc = cfg["patch"]
    sc = cfg.get("speedcal")
    if sc:
        sat = (s >= pc["s_min"]) & (v >= pc["v_min"])
        for y in np.linspace(sc["row_top"] * h, sc["row_bottom"] * h, sc["rows"]).astype(int):
            an.sat_rows.append(float(sat[max(0, y - 1):min(h, y + 2)].mean()))
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
        self._gaps = []             # two 상태에서 최근 간격들(합류 추세 판단)
        self._pair_slopes = []      # two 상태에서 (내 선, 다른 선) 기울기(교차 판단)
        self._two_frames = 0        # 이번 two 상태가 이어진 프레임 수
        self._side_entry = False    # 이번 two 상태가 옆에서 들어온 선으로 시작했는지
        self._entry_gap = None
        self.last_onset = None      # 마지막 1->2 순간 정보 {ratio, trend, slanted}
        self.just_changed = False   # 이번 프레임에 조각 수 상태가 바뀜(confirm_frames 늦게 확정됨)
        self._fork_n = 0
        self.fork_ahead = False     # 앞에 갈래(split)가 보임(디버그 표시용)
        self.in_split = False       # 갈라진 직후 두 가지 사이를 지나는 중
        self.side = {"left": 0, "right": 0}

    @property
    def active(self):
        return self.state == "two" or self.fork_ahead

    def lw_or(self, an):
        return self.lw or 0.06 * an.w

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

    def _slope(self, an, x0):
        """조향 줄의 x0 조각을 위쪽(먼) 줄로 따라가 줄당 x 변화(px)를 잰다. 못 따라가면 None."""
        lw = self.lw_or(an)
        n = self.c["slant_rows"]
        x, k = x0, 0
        for i in range(an.ctrl_idx - 1, max(-1, an.ctrl_idx - 1 - n), -1):
            cand = an.rows[i][1]
            if not cand:
                break
            nx = min(cand, key=lambda v: abs(v - x))
            if abs(nx - x) > 3.0 * lw:
                break
            x, k = nx, k + 1
        return (x - x0) / k if k else None

    def _slanted_side(self, an, l, r):
        """조향 줄의 두 조각 중 화면에서 더 비스듬한 쪽(left/right)과 두 기울기."""
        slopes = [self._slope(an, l), self._slope(an, r)]
        if None in slopes:
            return None, slopes
        if abs(abs(slopes[0]) - abs(slopes[1])) < self.c["slant_min_px"]:
            return None, slopes
        return ("left" if abs(slopes[0]) > abs(slopes[1]) else "right"), slopes

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
        lw = self.lw_or(an)
        join = self.c["join_k"] * lw

        if self.state == "two" and obs == "two":
            l, r = self._pick_two(segs, an.w)
            ref = tracked_x if tracked_x is not None else an.w / 2.0
            mine_left = abs(l - ref) <= abs(r - ref)
            self.side["left" if mine_left else "right"] += 1
            sm, so = self._slope(an, l if mine_left else r), self._slope(an, r if mine_left else l)
            if sm is not None and so is not None:
                self._pair_slopes = (self._pair_slopes + [(sm, so)])[-4:]
        if obs is not None and obs != self.state:
            self._pend += 1
            if self._pend == 1:
                self._pend_gap = gap
                self._pend_gaps = []
                self._pend_segs = self._pick_two(segs, an.w) if obs == "two" else None
                self._pend_an = an
            if gap is not None:
                self._pend_gaps.append(gap)
            if self._pend >= self.c["confirm_frames"]:
                self.just_changed = True
                if obs == "two":
                    g0 = self._pend_gap
                    self._entry_gap = g0
                    trend = self._pend_gaps[-1] - self._pend_gaps[0] if self._pend_gaps else 0.0
                    l0, r0 = self._pend_segs
                    slanted, slopes = self._slanted_side(self._pend_an, l0, r0)
                    self.last_onset = {"ratio": round(g0 / lw, 2), "trend": round(trend, 1),
                                       "slanted": slanted,
                                       "slopes": [None if v is None else round(v, 1) for v in slopes]}
                    if g0 < self.c["split_sure_k"] * lw:
                        self.in_split = True
                    elif g0 < self.c["split_max_k"] * lw:
                        self.in_split = trend >= self.c["split_trend_px"]
                    else:
                        self.in_split = False
                    self.side = {"left": 0, "right": 0}
                    self._gaps = []
                    self._pair_slopes = []
                    self._two_frames = 0
                    self._side_entry = not self.in_split
                else:
                    side = None
                    if self.side["left"] != self.side["right"]:
                        side = "left" if self.side["left"] > self.side["right"] else "right"
                    if self.in_split:
                        events.append({"type": "split", "side": side})
                    converging = (len(self._gaps) >= 3 and self._gaps[-1] < self._gaps[0] - 0.3 * lw)
                    if self._last_gap is not None and (
                            self._last_gap < join
                            or (converging and self._last_gap < self.c["merge_conv_k"] * lw)):
                        ps = self._pair_slopes
                        slopes = None
                        if ps:
                            slopes = (sum(a for a, _ in ps) / len(ps), sum(b for _, b in ps) / len(ps))
                        events.append({"type": "merge", "side": side, "slopes": slopes})
                    elif (not self.in_split and self._side_entry and self._last_gap is not None
                          and self._two_frames <= self.c["vee_max_frames"]
                          and self._last_gap < self.c["merge_conv_k"] * lw
                          and (self._entry_gap is None or self._last_gap < self._entry_gap)):
                        # 옆에서 가파르게 들어와 짧게 붙음. 붙은 뒤 내 선이 그 선 쪽으로 휘어 나가면
                        # 급하게 꺾여 나가는 가지(V자), 반대쪽으로 휘면 옆에서 붙는 합류(T자)다.
                        other = None if side is None else ("right" if side == "left" else "left")
                        cont = self._slope(an, segs[0]) if segs else None
                        th = self.c["vee_cont_px"]
                        if other is not None and cont is not None and (
                                cont >= th if other == "right" else cont <= -th):
                            events.append({"type": "vee", "side": other, "frames": self._two_frames,
                                           "cont": round(cont, 1),
                                           "gaps": [round(self._entry_gap / lw, 2) if self._entry_gap else None,
                                                    round(self._last_gap / lw, 2)]})
                    self.in_split = False
                self.state = obs
                self._pend = 0
        elif obs is not None:
            self._pend = 0
            if obs == "two":
                self._two_frames += 1
                # 간격이 갑자기 크게 벌어지면(교차 위 스퍼 띠 조각 등) 다른 선을 잘못 잡은 것으로 보고 무시
                if not (self._gaps and gap > self._gaps[-1] + 0.8 * lw):
                    self._last_gap = gap
                    self._gaps = (self._gaps + [gap])[-5:]

        if self._fork(an):
            self._fork_n += 1
        else:
            self._fork_n = 0
        self.fork_ahead = self._fork_n >= self.c["fork_frames"]
        return events


def _line_x(run, ref, lw, wide_k=2.2):
    """조각 하나에서 라인 중심 x. 넓은 조각(스퍼·교차가 붙은 것)은 ref 쪽 가장자리에서 반 라인폭 안쪽."""
    a, b = run
    if lw is None or (b - a + 1) <= wide_k * lw:
        return (a + b) / 2.0
    if abs(a - ref) <= abs(b - ref):
        return a + lw / 2.0
    return b - lw / 2.0


class TeeTracker(object):
    """조향 줄에서 라인 조각이 한쪽으로만 넓어지는 순간(옆으로 뻗은 스퍼가 지나감)을 잡는다."""

    def __init__(self, cfg):
        self.c = cfg["tee"]
        self.armed = True
        self._n = 0
        self._side = None
        self._off = 0
        self.last = None

    def _clear(self, an, x, lw):
        """조향 줄 위·아래(clear_rows 떨어진 줄)에는 내 선만 보통 폭으로 있어야 스퍼 띠로 본다.
        합류하며 넓어진 선이나 교차는 여러 줄에 걸쳐 넓거나 두 갈래라 여기서 걸러진다."""
        k = self.c["clear_rows"]
        for i in (an.ctrl_idx - k, an.ctrl_idx + k):
            if i < 0 or i >= len(an.raw):
                continue
            runs = an.raw[i]
            if len(runs) != 1:
                return False
            a, b = runs[0]
            if (b - a + 1) > 2.0 * lw or not (a - lw <= x <= b + lw):
                return False
        return True

    def update(self, an, tracked_x, lw):
        """반환: None 또는 {"type": "tee", "side": left|right}"""
        if lw is None or tracked_x is None:
            return None
        runs = an.raw[an.ctrl_idx]
        hit = None
        if runs:
            a, b = min(runs, key=lambda r: 0 if r[0] <= tracked_x <= r[1] else min(abs(r[0] - tracked_x), abs(r[1] - tracked_x)))
            width = b - a + 1
            ext_l = (tracked_x - a) - lw / 2.0
            ext_r = (b - tracked_x) - lw / 2.0
            if width > self.c["width_k"] * lw and self._clear(an, tracked_x, lw):
                big, small = max(ext_l, ext_r), min(ext_l, ext_r)
                if big > self.c["ext_k"] * lw and small < self.c["other_k"] * lw:
                    hit = "left" if ext_l > ext_r else "right"
                    self.last = {"width_k": round(width / lw, 1), "ext_k": round(big / lw, 1)}
        if hit:
            self._off = 0
            if self.armed:
                self._n = self._n + 1 if hit == self._side else 1
                self._side = hit
                if self._n >= self.c["confirm_frames"]:
                    self.armed = False
                    return {"type": "tee", "side": hit}
        else:
            self._n = 0
            self._off += 1
            if self._off >= self.c["rearm_frames"]:
                self.armed = True
        return None


class Follower(object):
    """라인 위 목표점을 고르고 좌우 모터 명령을 만든다."""

    def __init__(self, cfg):
        self.c = cfg["follow"]
        self.last_x = None
        self.prev_err = 0.0
        self.rejected = 0       # 이번 프레임에 버린 조각 수(디버그용)
        self.missed = 999       # 라인을 못 본 연속 프레임 수

    def _ok(self, run, x, w):
        """마지막 라인 위치에서 갑자기 멀리 있는 조각은 받지 않는다(화면 옆 끝에 붙은 조각은 더 엄하게).
        급커브에서 라인이 화면 밖으로 나간 순간, 반대편 끝이나 먼 줄에 보이는 매트 밖 바닥(어두움)을
        라인으로 잡아 계속 그쪽으로 도는 것을 막는다."""
        a, b = run
        lim = self.c.get("jump_frac", 0.3) if (a <= 1 or b >= w - 2) else self.c.get("max_jump_frac", 0.5)
        if self.missed == 0:
            lim = min(lim, self.c.get("track_jump_frac", 0.2))
        return abs(x - self.last_x) <= lim * w

    def edge_side(self, w):
        """마지막으로 본 라인이 화면 옆 끝 근처였으면 그 쪽(left/right), 아니면 None."""
        if self.last_x is None:
            return None
        f = self.c.get("edge_frac", 0.3)
        if self.last_x < f * w:
            return "left"
        if self.last_x > (1.0 - f) * w:
            return "right"
        return None

    def target(self, an, choice=None, lw=None, near_only=False):
        """조향 목표 x(px). choice가 left/right면 갈림길에서 그쪽 가지를 택한다.
        lw(라인폭 px)를 주면 스퍼·교차로 넓어진 조각에서 내 선 쪽 가장자리를 쓴다.
        near_only: 조향 줄과 그보다 가까운 줄에서만 찾는다(라인을 찾아 회전하는 중. 먼 줄에는 매트 밖이 보일 수 있음)."""
        if self.last_x is None:
            self.last_x = an.w / 2.0
        ctrl_segs = an.rows[an.ctrl_idx][1]
        wmin = self.c.get("min_w_k", 0.3) * lw if lw else 0.0
        if wmin and an.widths:
            ctrl_segs = [c for c, wd in zip(ctrl_segs, an.widths[an.ctrl_idx]) if wd >= wmin]
        x = None
        if choice in ("left", "right") and len(ctrl_segs) >= 2:
            x = ctrl_segs[0] if choice == "left" else ctrl_segs[-1]
        if x is None:
            # 조향 줄 -> 그보다 가까운 줄 -> 먼 줄 순서로 찾는다
            order = [an.ctrl_idx] + list(range(an.ctrl_idx + 1, len(an.rows)))
            if not near_only:
                order += list(range(an.ctrl_idx - 1, -1, -1))
            self.rejected = 0
            runs_by_row = an.raw_all or an.raw
            for i in order:
                if runs_by_row:
                    cand = []
                    for r in runs_by_row[i]:
                        cx = _line_x(r, self.last_x, lw)
                        if (r[1] - r[0] + 1) >= wmin and self._ok(r, cx, an.w):
                            cand.append(cx)
                        else:
                            self.rejected += 1
                else:
                    cand = an.rows[i][1]
                if cand:
                    x = min(cand, key=lambda s: abs(s - self.last_x))
                    break
        if x is not None:
            self.last_x = float(x)
            self.missed = 0
        else:
            self.missed += 1
        return x

    def command(self, x, w, dt):
        if x is None:
            return 0.0, 0.0
        err = (x - w / 2.0) / (w / 2.0)
        if self.prev_err is None:       # 라인을 다시 잡은 첫 프레임: 미분항이 튀지 않게
            self.prev_err = err
        d = (err - self.prev_err) / max(dt, 1e-3)
        self.prev_err = err
        turn = self.c["kp"] * err + self.c["kd"] * d
        base = self.c["base_speed"] * max(0.4, 1.0 - self.c["slow_k"] * abs(err))
        m = self.c["max_cmd"]
        left, right = base + turn, base - turn
        lo = self.c.get("min_track", 0.0)
        if lo and min(left, right) < lo:
            # 안쪽 궤도를 거의 세우면 바깥 궤도 혼자 차체를 비틀어야 해서 실물에서 멈췄다.
            # 좌우 차이(회전 정도)는 그대로 두고 둘 다 올려 양쪽이 같이 밀며 돌게 한다
            up = lo - min(left, right)
            left, right = left + up, right + up
        left = max(-m, min(m, left))
        right = max(-m, min(m, right))
        return left, right


def draw_debug(bgr, an, follower=None, junction=None, patch=None, text=None, tee=None):
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
    if tee is not None and not tee.armed:
        lines.append("tee:" + str(tee._side))
    if text:
        lines.append(text)
    for i, t in enumerate(lines):
        cv2.putText(img, t, (4, 14 + 14 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 3)
        cv2.putText(img, t, (4, 14 + 14 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1)
    return img
