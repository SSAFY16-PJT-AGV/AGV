# -*- coding: utf-8 -*-
"""탐색 주행 한 스텝: 프레임 -> 인식 -> 이벤트 기록·탐색 결정 -> 모터 명령.

실물(explore.py)과 시뮬레이터(sim/run_sim.py)가 같은 이 코드를 쓴다.

모드
    drive    라인을 따라 자율 주행하고 갈림길은 Explorer가 고른다.
    ask      자율 주행하되 갈림길에서 멈추고 사람에게 방향을 묻는다(분기 선택이 불안할 때).
    passive  모터를 쓰지 않는다. 손으로 밀거나 다른 프로그램으로 조종하면서 검출·기록만 한다.
             이동거리를 모르므로 odo_mm은 null(빌더가 시간 x 속도로 대신 계산).
"""
import collections

from .linecam import Follower, JunctionTracker, PatchTracker, analyze
from .topo import Explorer

MODES = ("drive", "ask", "passive")


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
        self.follow = Follower(cfg)
        self.odo = 0.0
        self.choice = None          # 지금 갈림길에서 명령한 쪽
        self.choice_t = 0.0
        self._odo_hist = collections.deque(maxlen=16)
        self.split_onset_odo = None
        self.t = 0.0
        self.lost_s = 0.0
        self.lost_logged = False
        self.finished = False
        self.finish_reason = None
        self.last_an = None
        self.last_cmd = (0.0, 0.0)
        self.log.log("start", self._odo(), robot=robot_id, mode=mode,
                     speed_mm_s=self.speed_mm_s, base_speed=cfg["follow"]["base_speed"],
                     map_hint=map_hint)

    def _odo(self):
        return None if self.mode == "passive" else self.odo

    def _finish(self, reason):
        if not self.finished:
            self.finished = True
            self.finish_reason = reason
            self.log.log("end", self._odo(), reason=reason, explorer=self.ex.status())

    def step(self, frame, dt):
        """프레임 하나 처리. 반환: (left, right) 모터 명령."""
        if self.finished:
            return 0.0, 0.0
        an = analyze(frame, self.cfg)
        self.last_an = an

        color = self.patch.update(an)
        if color:
            self.log.log("patch", self._odo(), color=color)
            self.ex.on_patch(color, self.odo)
            if self.ex.done and self.mode != "passive":
                self._finish("explore_" + str(self.ex.done_reason))
                return 0.0, 0.0

        self._odo_hist.append(self.odo)
        jevents = self.junc.update(an, self.follow.last_x)
        self.t += dt
        if self.junc.just_changed and self.junc.state == "two" and self.junc.in_split:
            # 확정은 confirm_frames 늦으므로 처음 갈라진 프레임의 거리를 쓴다
            k = self.cfg["junction"]["confirm_frames"]
            self.split_onset_odo = self._odo_hist[-k] if len(self._odo_hist) >= k else self.odo
        # 갈 쪽은 조향 줄에서 선이 막 갈라진 순간(in_split)에만 정한다.
        # 앞쪽 갈래 모양(fork_ahead)은 옆에서 들어오는 합류선과 화면상 구분이 안 돼서 쓰지 않는다.
        need_choice = self.junc.in_split
        if need_choice and self.choice is None and self.mode != "passive":
            suggestion = self.ex.choose_split(self.odo)
            if self.mode == "ask" and self.ask_fn is not None:
                self.choice = self.ask_fn(suggestion) or suggestion
            else:
                self.choice = suggestion
            self.choice_t = self.t
        for je in jevents:
            if je["type"] == "split":
                side = je["side"] or self.choice
                odo = self.split_onset_odo if self.split_onset_odo is not None else self.odo
                self.log.log("split", None if self.mode == "passive" else odo, choice=side,
                             cmd=self.choice, onset=self.junc.last_onset)
                self.split_onset_odo = None
                self.ex.on_split(side, self.odo)
                self.choice = None
            else:
                self.log.log("merge", self._odo(), from_side=je["side"])
                self.ex.on_merge(self.odo)
        # 갈래로 보였지만 실제로 갈라지지 않았으면(오검출) 선택을 버린다
        if (self.choice is not None and not need_choice and self.junc.state == "one"
                and self.t - self.choice_t > self.cfg["junction"].get("choice_timeout_s", 3.0)):
            self.log.log("note", self._odo(), msg="fork_without_split", cmd=self.choice)
            self.choice = None

        x = self.follow.target(an, self.choice)
        if x is None:
            self.lost_s += dt
            if self.lost_s >= self.cfg["follow"]["lost_timeout_s"] and not self.lost_logged:
                self.lost_logged = True
                self.log.log("gap", self._odo(), reason="line_lost")
                self.ex.on_gap()
                if self.mode != "passive":
                    self._finish("line_lost")
                    return 0.0, 0.0
        else:
            self.lost_s = 0.0
            self.lost_logged = False

        if self.mode == "passive":
            return 0.0, 0.0
        if x is None and self.lost_s > 0:
            left, right = self.last_cmd[0] * 0.8, self.last_cmd[1] * 0.8  # 잠깐 놓치면 관성 유지
        else:
            left, right = self.follow.command(x, an.w, dt)
        base = self.cfg["follow"]["base_speed"]
        self.odo += self.speed_mm_s * max(0.0, (left + right) / 2.0) / base * dt
        self.last_cmd = (left, right)
        return left, right

    def stop(self, reason="user_stop"):
        self._finish(reason)
