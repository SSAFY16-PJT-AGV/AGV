# -*- coding: utf-8 -*-
"""ArUco 마커 검출(직접 구현). 로봇 Python(3.6 / OpenCV 4.1)에는 cv2.aruco가 없어서 기본 모듈로 만든다.

순서(docs/perception/shelf-perception.md "처리 파이프라인" 3~5):
1. 후보 찾기: adaptiveThreshold(반전) -> findContours -> approxPolyDP로 볼록 사각형만 남긴다.
2. 비트 읽기: 후보를 warpPerspective로 정사각형으로 펴고 Otsu로 이진화한 뒤, 칸마다 가운데 부분의
   흑백 다수결로 읽는다. 바깥 테두리 칸이 (거의) 모두 검은색이 아니면 버린다.
3. ID 맞추기: 안쪽 비트를 사전 코드와 네 방향 회전으로 비교해 해밍 거리가 가장 작은 것을 고른다.
   허용값을 넘으면 버린다. 맞은 회전으로 꼭짓점 순서를 OpenCV aruco와 같게(마커 기준 왼쪽 위부터
   시계 방향) 맞춘다.

사전은 표준 DICT_4X4_50을 그대로 쓴다(마커는 기존 생성기로 뽑는다). 코드는 OpenCV 5.0
generateImageMarker로 만든 마커의 안쪽 4x4 칸(흰색=1)을 행 우선으로 읽은 값이다.

Python 3.6 / OpenCV 4.1 호환.
"""
from __future__ import division, print_function

import cv2
import numpy as np

# DICT_4X4_50: 안쪽 4x4 비트(흰색=1, 행 우선, 첫 비트가 최상위)
DICT_4X4_50 = [46386, 3994, 13101, 39238, 21662, 31181, 40494, 50418, 65242, 53078, 63889, 4519, 3767,
               10767, 9393, 9790, 18021, 26112, 27742, 30383, 34443, 45099, 52437, 56706, 65095, 38001,
               44260, 42324, 8483, 13423, 17429, 22450, 40655, 61643, 2222, 2345, 6261, 1279, 3574, 7258,
               5912, 10792, 12940, 14514, 9448, 12011, 11583, 19300, 20526, 20499]

DEFAULT_PARAMS = {
    "thresh_windows": [13, 33, 63],   # adaptiveThreshold 창 크기(px). 여러 개를 돌려 후보를 모은다
    "thresh_c": 7,                    # adaptiveThreshold 상수
    "min_side_px": 10.0,              # 이보다 작은 사각형은 버림(원본 해상도 기준)
    "approx_rate": 0.03,              # approxPolyDP 허용 오차 = 둘레 x 이 값
    "min_corner_gap_rate": 0.05,      # 꼭짓점끼리 이 비율(둘레 대비)보다 가까우면 버림
    "cell_px": 10,                    # 펼 때 칸 하나 크기
    "cell_margin": 0.15,              # 칸 가장자리에서 이 비율은 빼고 읽음
    "max_border_err": 3,              # 테두리 칸 20개 중 흰색으로 읽혀도 되는 수
    "max_bit_err": 1,                 # 사전 코드와의 해밍 거리 허용값(DICT_4X4_50 교정 가능 비트 = 1)
    "min_otsu_std": 5.0,              # 펼친 영상 밝기 표준편차가 이보다 작으면 한 색으로 봄
    "min_quiet_white": 0.6,           # 테두리 바깥 여백 띠 중 흰색이어야 하는 비율(가짜 사각형 거르기)
    "subpix": True,                   # cornerSubPix로 꼭짓점 다듬기
}


def _code_to_bits(code, n=4):
    return np.array([(code >> (n * n - 1 - i)) & 1 for i in range(n * n)], np.uint8).reshape(n, n)


def _bits_to_code(bits):
    v = 0
    for b in bits.flatten():
        v = (v << 1) | int(b)
    return v


class Dictionary(object):
    """마커 사전. 각 ID의 네 방향 회전 코드를 미리 만들어 둔다."""

    def __init__(self, codes, marker_size=4):
        self.n = marker_size
        self.codes = list(codes)
        # rot[k][id]: 정방향 비트를 np.rot90(k)번 돌린 코드
        self.rot = []
        for k in range(4):
            self.rot.append(np.array([_bits_to_code(np.rot90(_code_to_bits(c, self.n), k)) for c in self.codes],
                                     np.int64))

    def match(self, bits):
        """읽은 안쪽 비트 -> (id, 해밍 거리, k). k: 읽은 비트가 정방향을 np.rot90(k)번 돌린 모양."""
        obs = _bits_to_code(bits)
        best = (None, 99, 0)
        for k in range(4):
            x = np.bitwise_xor(self.rot[k], obs)
            d = np.array([bin(int(v)).count("1") for v in x])
            i = int(np.argmin(d))
            if d[i] < best[1]:
                best = (i, int(d[i]), k)
        return best


DICT = Dictionary(DICT_4X4_50)


class Marker(object):
    """검출된 마커. corners: (4,2) float32, 마커 기준 왼쪽 위 -> 오른쪽 위 -> 오른쪽 아래 -> 왼쪽 아래."""

    def __init__(self, mid, corners, hamming):
        self.id = int(mid)
        self.corners = np.asarray(corners, np.float32).reshape(4, 2)
        self.hamming = hamming

    @property
    def center(self):
        return self.corners.mean(axis=0)

    @property
    def side_px(self):
        c = self.corners
        return float(np.mean([np.linalg.norm(c[i] - c[(i + 1) % 4]) for i in range(4)]))

    def __repr__(self):
        return "Marker(id=%d, center=(%.1f, %.1f), side=%.1fpx)" % (self.id, self.center[0], self.center[1],
                                                                  self.side_px)


def _to_gray(img):
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def _find_contours(binary):
    out = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    return out[-2]          # OpenCV 3.x는 (img, contours, hier), 4.x는 (contours, hier)


def _candidates(gray, p):
    """볼록 사각형 후보들 (N,4,2). 영상 좌표에서 시계 방향(왼쪽 위 -> 오른쪽 위 ...)으로 정렬."""
    h, w = gray.shape[:2]
    max_perim = 4.0 * max(w, h)
    found = []
    for win in p["thresh_windows"]:
        win = int(win) | 1
        if win < 3:
            continue
        th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, win,
                                   p["thresh_c"])
        for c in _find_contours(th):
            if len(c) < 4 * p["min_side_px"] * 0.8:
                continue
            perim = cv2.arcLength(c, True)
            if perim < 4 * p["min_side_px"] or perim > max_perim:
                continue
            q = cv2.approxPolyDP(c, perim * p["approx_rate"], True)
            if len(q) != 4 or not cv2.isContourConvex(q):
                continue
            q = q.reshape(4, 2).astype(np.float32)
            sides = [np.linalg.norm(q[i] - q[(i + 1) % 4]) for i in range(4)]
            if min(sides) < p["min_side_px"] or min(sides) < perim * p["min_corner_gap_rate"]:
                continue
            # 화면 가장자리에 붙은 것은 잘린 마커라 버림
            if q[:, 0].min() < 1 or q[:, 1].min() < 1 or q[:, 0].max() > w - 2 or q[:, 1].max() > h - 2:
                continue
            area2 = sum(q[i, 0] * q[(i + 1) % 4, 1] - q[(i + 1) % 4, 0] * q[i, 1] for i in range(4))
            if area2 < 0:                # 영상 좌표(y 아래)에서 시계 방향이 되게
                q = q[[0, 3, 2, 1]]
            found.append(q)
    # 창 크기마다 같은 사각형이 또 나오므로 겹치는 것은 하나만 남김(큰 것)
    found.sort(key=lambda q: -cv2.arcLength(q.reshape(-1, 1, 2), True))
    keep = []
    for q in found:
        side = cv2.arcLength(q.reshape(-1, 1, 2), True) / 4.0
        dup = False
        for k in keep:
            # 꼭짓점 순서가 다를 수 있으므로 가장 가까운 짝끼리 비교
            d = min(np.mean(np.linalg.norm(np.roll(k, s, axis=0) - q, axis=1)) for s in range(4))
            if d < 0.05 * side:
                dup = True
                break
        if not dup:
            keep.append(q)
    return keep


def _read_bits(gray, quad, p, n=4):
    """사각형을 펴서 (n+2)x(n+2) 칸을 읽는다. 흰색=1. 한 색뿐이면 (None, 0).

    바깥 한 칸 폭(흰 여백 자리)도 같이 펴서, 그중 흰색으로 읽힌 비율을 같이 돌려준다. 진짜 마커는
    테두리 바깥에 흰 여백이 있어서 이 비율이 높고, 배경 무늬로 생긴 가짜 사각형은 낮다.
    """
    cells = n + 2
    cp = int(p["cell_px"])
    size = cells * cp
    full = size + 2 * cp
    dst = np.array([[cp, cp], [cp + size - 1, cp], [cp + size - 1, cp + size - 1], [cp, cp + size - 1]],
                   np.float32)
    M = cv2.getPerspectiveTransform(quad.astype(np.float32), dst)
    big = cv2.warpPerspective(gray, M, (full, full), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    warped = big[cp:cp + size, cp:cp + size]
    if float(warped.std()) < p["min_otsu_std"]:
        return None, 0.0
    thr, bw = cv2.threshold(warped, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    m = int(round(cp * p["cell_margin"]))
    out = np.zeros((cells, cells), np.uint8)
    for r in range(cells):
        for c in range(cells):
            patch = bw[r * cp + m:(r + 1) * cp - m, c * cp + m:(c + 1) * cp - m]
            out[r, c] = 1 if patch.mean() > 127 else 0
    # 바깥 여백 띠(테두리에서 반 칸 떨어진 곳부터 한 칸 안쪽까지)
    ring = np.ones((full, full), bool)
    h = cp // 2
    ring[h:full - h, h:full - h] = False
    ring[:2, :] = ring[-2:, :] = False
    ring[:, :2] = ring[:, -2:] = False
    quiet = float((big[ring] > thr).mean())
    return out, quiet


def detect(img, dictionary=DICT, params=None):
    """영상에서 마커를 찾는다. 반환: [Marker]. img는 BGR 또는 회색조(uint8)."""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    gray = _to_gray(img)
    n = dictionary.n
    found = []
    for quad in _candidates(gray, p):
        cells, quiet = _read_bits(gray, quad, p, n)
        if cells is None or quiet < p["min_quiet_white"]:
            continue
        border = np.concatenate([cells[0], cells[-1], cells[1:-1, 0], cells[1:-1, -1]])
        if int(border.sum()) > p["max_border_err"]:
            continue
        mid, ham, k = dictionary.match(cells[1:-1, 1:-1])
        if mid is None or ham > p["max_bit_err"]:
            continue
        # 읽은 비트 = 정방향을 rot90(k)번(반시계) 돌린 모양 -> 정방향 왼쪽 위 꼭짓점은 지금 k번째 꼭짓점
        corners = np.roll(quad, k, axis=0)
        found.append(Marker(mid, corners, ham))
    # 같은 마커가 두 번 잡히거나, 마커 안쪽(테두리 안 경계)이 다른 ID로 잘못 읽히면 큰 것만 남김
    found.sort(key=lambda m: -m.side_px)
    out = []
    for m in found:
        inside = False
        for o in out:
            poly = o.corners.reshape(-1, 1, 2).astype(np.float32)
            cx, cy = float(m.center[0]), float(m.center[1])
            if cv2.pointPolygonTest(poly, (cx, cy), False) >= 0:
                inside = True
                break
        if not inside:
            out.append(m)
    if p["subpix"] and out:
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01)
        for m in out:
            win = int(max(2, min(5, m.side_px / 12.0)))
            c = m.corners.reshape(-1, 1, 2).copy()
            cv2.cornerSubPix(gray, c, (win, win), (-1, -1), crit)
            m.corners = c.reshape(4, 2)
    return out


def marker_object_points(size_mm):
    """마커 좌표계의 꼭짓점(OpenCV aruco와 같음): x 오른쪽, y 위, z 마커 밖(카메라 쪽)."""
    s = size_mm / 2.0
    return np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], np.float64)


def estimate_pose(marker, size_mm, cam):
    """마커 자세 후보들(카메라 좌표). 반환: [(R(3x3), t(3,), 재투영 오차 px)], 오차 작은 순.

    평면 마커는 기울기가 두 가지로 풀릴 수 있어서(뒤집힘 모호성) 가능하면 두 해를 다 돌려준다.
    고르는 것은 쓰는 쪽(선반 판정)에서 '세로 면' 같은 조건으로 한다.
    """
    obj = marker_object_points(size_mm)
    nrm = cam.undistort(marker.corners).reshape(-1, 1, 2)
    K = np.eye(3)
    sols = []
    generic = getattr(cv2, "solvePnPGeneric", None)
    ippe = getattr(cv2, "SOLVEPNP_IPPE_SQUARE", None)
    if generic is not None and ippe is not None:
        try:
            res = generic(obj, nrm, K, None, flags=ippe)
            for rv, tv in zip(res[1], res[2]):
                sols.append((rv, tv))
        except cv2.error:
            sols = []
    if not sols:
        ok, rv, tv = cv2.solvePnP(obj, nrm, K, None, flags=cv2.SOLVEPNP_ITERATIVE)
        if ok:
            sols.append((rv, tv))
    out = []
    for rv, tv in sols:
        R, _ = cv2.Rodrigues(rv)
        t = np.asarray(tv, np.float64).reshape(3)
        if t[2] <= 0:
            continue
        Xc = obj.dot(R.T) + t
        proj = cam.project(Xc)
        err = float(np.mean(np.linalg.norm(proj - marker.corners, axis=1)))
        out.append((R, t, err))
    out.sort(key=lambda s: s[2])
    return out


def draw(img, markers, color=(0, 200, 0)):
    """디버그용: 마커 테두리와 ID를 그린 사본."""
    out = img.copy() if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    for m in markers:
        pts = m.corners.astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(out, [pts], True, color, 2)
        c0 = tuple(int(v) for v in m.corners[0])
        cv2.circle(out, c0, 4, (0, 0, 255), -1)
        cx, cy = (int(v) for v in m.center)
        cv2.putText(out, str(m.id), (cx - 8, cy + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return out
