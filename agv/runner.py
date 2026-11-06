# -*- coding: utf-8 -*-
"""탐색 주행 한 스텝: 프레임 -> 인식 -> 이벤트 기록·탐색 결정 -> 모터 명령.

실물(explore.py)과 시뮬레이터(sim/run_sim.py)가 같은 이 코드를 쓴다.

모드
    drive    라인을 따라 자율 주행하고 갈림길은 Explorer가 고른다.
    ask      자율 주행하되 갈림길에서 멈추고 사람에게 방향을 묻는다(분기 선택이 불안할 때).
    passive  모터를 쓰지 않는다. 손으로 밀거나 다른 프로그램으로 조종하면서 검출·기록만 한다.
             이동거리를 모르므로 odo_mm은 null(빌더가 시간 x 속도로 대신 계산).

X자 교차 처리
    merge가 나면 바로 기록하지 않고 잠시 보류한다(pending). 보류 중 나온 패치·스퍼 이벤트도
    뒤에 줄 세워 둔다. cross_window_mm 안에 선이 다시 갈라지는데, 비스듬한 쪽이 merge 때 내가 있던
    쪽(= 다른 선이 빠져나갈 쪽)이면 X자 교차로 보고 "cross" 이벤트를 남긴 뒤 곧은 선을 따라간다.
    아니면 보류했던 merge를 그대로 기록하고 보통 갈림길로 처리한다.

이벤트 위치
    odo_mm은 판정한 바닥 지점 위치다(차체 위치 + geom.*_ahead_mm). 확정이 몇 프레임 늦게 나도
    처음 감지한 프레임의 위치를 쓴다.
"""
import collections

from .linecam import Follower, JunctionTracker, PatchTracker, TeeTracker, analyze
from .topo import Explorer

MODES = ("drive", "ask", "passive")
OPP = {"left": "right", "right": "left"}


class ExploreRunner(object):
    def __init__(self, cfg, logger, mode="drive", explorer=None, ask_fn=None,
                 speed_mm_s=150.0, robot_id="agv1", map_hint=None):
        assert mode in MODES
        self.cfg = cfg
        self.log = logger
        self.mode = mode
        self.ex = explorer or Explorer()
        self.ask_fn = ask_fn
        self.speed_mm_s = float(speed_mm_s)
        self.patch = PatchTracker(cfg)
        self.junc = JunctionTracker(cfg)
        self.tee = TeeTracker(cfg)
        self.follow = Follower(cfg)
        g = cfg.get("geom", {})
        self.ctrl_ahead = float(g.get("ctrl_ahead_mm", 0.0))
        self.patch_ahead = float(g.get("patch_ahead_mm", 0.0))
        self.odo = 0.0
        self.t = 0.0
        self.choice = None          # 지금 갈림길에서 명령한 쪽
        self.choice_t = 0.0
        self._hist = collections.deque(maxlen=16)   # (t, odo)
        self.split_onset = None     # (t, odo) 처음 갈라진 프레임
        self.pending = None         # 보류 중인 merge
        self.held = []              # 보류 중 나온 이벤트
        self.crossing = None        # 지금 지나는 X자 교차
        self.lost_s = 0.0
        self.lost_logged = False
        self.finished = False
        self.finish_reason = None
        self.last_an = None
        self.last_cmd = (0.0, 0.0)
        self.log.log("start", self._odo(), robot=robot_id, mode=mode,
                     speed_mm_s=self.speed_mm_s, base_speed=cfg["follow"]["base_speed"],
                     geom={"ctrl_ahead_mm": self.ctrl_ahead, "patch_ahead_mm": self.patch_ahead},
                     map_hint=map_hint)

    # ---------- 기록 ----------
    def _odo(self):
        return None if self.mode == "passive" else self.odo

    def _ago(self, k):
        """k프레임 전 (t, odo). 확정이 늦게 난 판정의 시작 시점."""
        if len(self._hist) >= k:
            return self._hist[-k]
        return (self.t, self.odo)

    def _ev(self, type_, when, ahead, **fields):
        t, odo = when
        return {"type": type_, "t": t + ahead / max(self.speed_mm_s, 1.0), "pos": odo + ahead,
                "fields": fields}

    def _dispatch(self, ev):
        """이벤트 하나를 기록하고 탐색 정책에 알린다."""
        if self.finished:
            return
        ts = self.log.clock() - (self.t - ev["t"])
        pos = None if self.mode == "passive" else ev["pos"]
        self.log.log(ev["type"], pos, ts=ts, **ev["fields"])
        p = ev["pos"]
        if ev["type"] == "patch":
            self.ex.on_patch(ev["fields"]["color"], p)
            if self.ex.done and self.mode != "passive":
                self._finish("explore_" + str(self.ex.done_reason))
        elif ev["type"] == "merge":
            self.ex.on_merge(p)
        elif ev["type"] == "split":
            self.ex.on_split(ev["fields"]["choice"], p)
        elif ev["type"] == "cross":
            self.ex.on_cross(p)
        elif ev["type"] == "tee":
            self.ex.on_tee(p)

    def _emit(self, ev):
        if self.pending is not None:
            self.held.append(ev)
        else:
            self._dispatch(ev)

    def _flush_pending(self):
        if self.pending is None:
            return
        pend, held = self.pending, self.held
        self.pending, self.held = None, []
        self._dispatch(pend["ev"])
        for ev in held:
            self._dispatch(ev)

    def _finish(self, reason):
        if not self.finished:
            self._flush_pending()
            self.finished = True
            self.finish_reason = reason
            self.log.log("end", self._odo(), reason=reason, explorer=self.ex.status())

    # ---------- 한 스텝 ----------
    def step(self, frame, dt):
        """프레임 하나 처리. 반환: (left, right) 모터 명령."""
        if self.finished:
            return 0.0, 0.0
        an = analyze(frame, self.cfg)
        self.last_an = an
        self._hist.append((self.t, self.odo))
        kc = self.cfg["junction"]["confirm_frames"]

        color = self.patch.update(an)
        if color:
            self._emit(self._ev("patch", self._ago(self.cfg["patch"]["on_frames"]), self.patch_ahead,
                                color=color))

        jevents = self.junc.update(an, self.follow.last_x)
        tee = self.tee.update(an, self.follow.last_x, self.junc.lw)
        if tee:
            self._emit(self._ev("tee", self._ago(self.cfg["tee"]["confirm_frames"]), self.ctrl_ahead,
                                side=tee["side"], measure=self.tee.last))

        # 1 -> 2: 교차인지 갈림길인지 판단
        if self.junc.just_changed and self.junc.state == "two":
            onset = self._ago(kc)
            ons = self.junc.last_onset or {}
            pend = self.pending
            if pend is not None and onset[1] - pend["odo"] <= self._window():
                if self._is_crossing(pend, ons):
                    # 다른 선이 들어온 쪽의 반대편으로 빠져나감 -> X자 교차. 곧은 선(반대쪽)을 따라간다
                    self.crossing = {"from_side": OPP[pend["side"]], "onset": ons}
                    self.junc.in_split = True
                    self.choice = OPP[pend["side"]]     # 선이 서로 자리를 바꾸므로 내 선은 반대편
                    self.choice_t = self.t
                    mid = ((pend["t"] + onset[0]) / 2.0, (pend["odo"] + onset[1]) / 2.0)
                    ev = self._ev("cross", mid, self.ctrl_ahead, from_side=self.crossing["from_side"],
                                  onset=ons)
                    held = self.held
                    self.pending, self.held = None, []
                    self._dispatch(ev)
                    for h in held:
                        self._dispatch(h)
            self._flush_pending()
            if self.junc.in_split and self.crossing is None:
                self.split_onset = onset
                if self.choice is None and self.mode != "passive":
                    suggestion = self.ex.choose_split(onset[1] + self.ctrl_ahead)
                    if self.mode == "ask" and self.ask_fn is not None:
                        self.choice = self.ask_fn(suggestion) or suggestion
                    else:
                        self.choice = suggestion
                    self.choice_t = self.t

        for je in jevents:
            if je["type"] == "split":
                if self.crossing is not None:
                    self.crossing = None        # 교차를 다 지남(이미 cross로 기록)
                else:
                    side = je["side"] or self.choice
                    when = self.split_onset or self._ago(kc)
                    self._emit(self._ev("split", when, self.ctrl_ahead, choice=side, cmd=self.choice,
                                        onset=self.junc.last_onset))
                self.split_onset = None
                self.choice = None
            else:
                self._flush_pending()
                when = self._ago(kc)
                self.pending = {"t": when[0], "odo": when[1], "side": je["side"],
                                "slopes": je.get("slopes"),
                                "ev": self._ev("merge", when, self.ctrl_ahead, from_side=je["side"])}
        if self.pending is not None and self.odo - self.pending["odo"] > self._window():
            self._flush_pending()
        if self.finished:
            return 0.0, 0.0

        # 갈래로 보였지만 실제로 갈라지지 않았으면(오검출) 선택을 버린다
        if (self.choice is not None and not self.junc.in_split and self.junc.state == "one"
                and self.t - self.choice_t > self.cfg["junction"].get("choice_timeout_s", 3.0)):
            self.log.log("note", self._odo(), msg="choice_without_split", cmd=self.choice)
            self.choice = None
            self.crossing = None

        self.t += dt
        x = self.follow.target(an, self.choice, self.junc.lw)
        if x is None:
            self.lost_s += dt
            if self.lost_s >= self.cfg["follow"]["lost_timeout_s"] and not self.lost_logged:
                self.lost_logged = True
                self._flush_pending()
                self.log.log("gap", self._odo(), reason="line_lost")
                self.ex.on_gap()
                if self.mode != "passive":
                    self._finish("line_lost")
                    return 0.0, 0.0
        else:
            self.lost_s = 0.0
            self.lost_logged = False

        if self.mode == "passive":
            # 실제 이동거리는 모르므로 기록하지 않지만(odo_mm null), 교차 판정 창 등 내부 비교용으로
            # 명목 속도만큼 늘린다.
            self.odo += self.speed_mm_s * dt
            return 0.0, 0.0
        if x is None and self.lost_s > 0:
            left, right = self.last_cmd[0] * 0.8, self.last_cmd[1] * 0.8  # 잠깐 놓치면 관성 유지
        else:
            left, right = self.follow.command(x, an.w, dt)
        base = self.cfg["follow"]["base_speed"]
        self.odo += self.speed_mm_s * max(0.0, (left + right) / 2.0) / base * dt
        self.last_cmd = (left, right)
        return left, right

    def _is_crossing(self, pend, ons):
        """보류 중인 merge 뒤 1->2가 X자 교차인지 판단한다.

        교차면 두 선이 자리를 바꾼다: 다른 선은 내가 있던 쪽으로 빠지고 기울기(방향)를 유지한다.
        둘 중 하나면 교차로 본다.
        (1) 내가 있던 쪽으로 빠지는 조각이 더 비스듬하다(곧은 선을 탄 채 다른 선이 가로지름)
        (2) 그쪽 조각 기울기가 merge 직전 다른 선 기울기와 이어진다(내 선이 휘어 있어 (1)이 안 맞을 때)
        둘 다 아니면(합류 뒤 같은 쪽으로 다시 갈라짐 - 보라 패치 뒤 렌즈 등) 보통 갈림길이다."""
        mine = pend.get("side")
        if mine not in OPP:
            return False
        if ons.get("slanted") == mine:
            return True
        sl = ons.get("slopes") or [None, None]
        new = {"left": sl[0], "right": sl[1]}
        ps = pend.get("slopes")
        if ps and new[mine] is not None:
            # 교차면 다른 선이 방향을 유지한 채 내가 있던 쪽으로 넘어온다: 그쪽 조각 기울기가
            # merge 전 다른 선 기울기와 비슷하고, 내 선 기울기보다 그쪽에 더 가까워야 한다.
            sm, so = ps
            d_other = abs(new[mine] - so)
            d_mine = abs(new[mine] - sm)
            ons["cross_fit"] = [round(d_other, 1), round(d_mine, 1)]
            return d_other < d_mine and d_other <= max(3.0, 0.5 * abs(so - sm))
        return False

    def _window(self):
        """교차 판정 거리 창(mm). passive에서는 명목 속도로 늘린 내부 거리로 비교한다."""
        return float(self.cfg["junction"].get("cross_window_mm", 300))

    def stop(self, reason="user_stop"):
        self._finish(reason)
