# -*- coding: utf-8 -*-
"""색 패치로 주행 속도(모터 명령 1당 실제 mm)를 계속 다시 잰다.

배터리가 닳거나 바닥이 바뀌면 같은 모터 명령에도 속도가 달라진다. 손으로 잰 속도 하나로
이동거리를 적분하면 맵 엣지 길이가 통째로 틀어진다. 대신 크기를 아는 색 패치를 자로 쓴다.

원리
    화면의 한 가로줄은 바닥의 한 선에 해당한다. 패치 앞 가장자리가 그 선을 지나는 순간부터
    뒤 가장자리가 지나는 순간까지 로봇은 정확히 패치 길이(L)만큼 움직인다. 이 사이에 적분한
    모터 명령(u, "기본 속도로 달린 초")으로 L을 나누면 기본 속도의 실제 mm/s가 나온다.
    렌즈 왜곡이나 카메라 각도와 상관이 없다(같은 줄에서 앞뒤 가장자리를 잼).

    - 줄 여러 개(speedcal.rows)에서 따로 재고 중앙값을 쓴다. 줄마다 패치 색 비율이 반을 넘는
      순간을 프레임 사이에서 보간해 1프레임보다 정밀하게 잡는다.
    - 지나는 동안 크게 돌았거나(곡선), 줄마다 값이 흩어지면 버린다.
    - 첫 측정은 그대로 쓰고, 그 뒤로는 조금씩 섞는다(배터리처럼 천천히 바뀌는 것을 따라감).

패치 사이 거리는 여전히 "모터 명령 x 이 값"으로 적분한다. 곡선에서 궤도가 미끄러지는 것은
이 방법으로 못 잡는다.
"""
import numpy as np


class SpeedCal(object):
    def __init__(self, cfg, k0):
        self.c = cfg["speedcal"]
        self.L = cfg["patch"].get("length_mm")
        self.enabled = bool(self.L)
        self.k = float(k0)          # 기본 속도(follow.base_speed)로 달릴 때 실제 mm/s
        self.k0 = float(k0)
        self.n = 0                  # 받아들인 측정 수
        self.last = None            # 마지막 측정 결과(보고용)
        self._pre = []
        self._ep = None
        self._quiet = 0

    def reset(self):
        """후진·회전 등으로 지금 지나던 패치 측정을 버린다."""
        self._ep = None
        self._pre = []
        self._quiet = 0

    def update(self, an, u, cmd):
        """프레임 하나. u: 지금까지 적분한 명령(기본 속도 초), cmd: 직전 (좌, 우) 명령.
        측정이 끝나면 결과 dict를 돌려준다(받아들였든 버렸든). 아니면 None."""
        if not self.enabled or not an.sat_rows:
            return None
        f = np.array(an.sat_rows, float)
        rec = (u, f, cmd)
        c = self.c
        if self._ep is None:
            self._pre.append(rec)
            del self._pre[:-3]
            if f.max() >= c["on"]:
                self._ep = list(self._pre)
                self._quiet = 0
            return None
        self._ep.append(rec)
        if len(self._ep) > c["max_frames"]:
            self.reset()
            return None
        if f.max() < c["off"]:
            self._quiet += 1
            if self._quiet >= c["end_frames"]:
                ep, self._ep, self._pre = self._ep, None, [rec]
                return self._finish(ep)
        else:
            self._quiet = 0
        return None

    def _finish(self, ep):
        c = self.c
        U = np.array([r[0] for r in ep])
        F = np.array([r[1] for r in ep])
        ks = []
        for j in range(F.shape[1]):
            f = F[:, j]
            peak = f.max()
            if peak < c["min_peak"]:
                continue
            th = 0.5 * peak
            if f[0] >= th or f[-1] >= th:
                continue          # 앞이나 뒤 가장자리를 못 봄
            above = np.nonzero(f >= th)[0]
            i0, i1 = above[0], above[-1]
            u_in = U[i0 - 1] + (th - f[i0 - 1]) / (f[i0] - f[i0 - 1]) * (U[i0] - U[i0 - 1])
            u_out = U[i1] + (f[i1] - th) / (f[i1] - f[i1 + 1]) * (U[i1 + 1] - U[i1])
            du = u_out - u_in
            if du > 1e-3:
                ks.append(self.L / du)
        res = {"rows": len(ks)}
        if len(ks) < c["min_rows"]:
            return None           # 패치를 제대로 못 봄(가장자리를 스침 등): 조용히 넘어감
        ks = np.array(ks)
        med = float(np.median(ks))
        spread = float((np.percentile(ks, 75) - np.percentile(ks, 25)) / med)
        lr = np.array([r[2] for r in ep], float)
        tot = np.abs(lr.sum(axis=1))
        turn = float(np.mean(np.abs(lr[:, 0] - lr[:, 1]) / np.maximum(tot, 1e-3)))
        res.update(k_obs=round(med, 1), spread=round(spread, 3), turn=round(turn, 3))
        if spread > c["max_spread"]:
            res["reject"] = "spread"
        elif turn > c["turn_max"]:
            res["reject"] = "turn"
        elif self.n > 0 and abs(med / self.k - 1.0) > c["max_jump"]:
            res["reject"] = "jump"
        elif self.n == 0 and not (0.4 * self.k0 <= med <= 2.5 * self.k0):
            res["reject"] = "range"
        else:
            self.k = med if self.n == 0 else self.k + c["alpha"] * (med - self.k)
            self.n += 1
        res["k"] = round(self.k, 1)
        res["n"] = self.n
        self.last = res
        return res
