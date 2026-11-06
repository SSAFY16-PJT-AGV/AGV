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

막다른 길(스퍼) 되돌아 나오기 (drive/ask 모드)
    갈림길·교차에서 고른 쪽으로 간 지 recover.window_mm 안에 선이 끝나면(조향 줄부터 위쪽 줄에
    선이 하나도 없고 아래쪽 줄에만 있음, 또는 선을 완전히 놓침) 막다른 길로 본다.
      1. 멈추고 "dead_end"를 기록한다(스퍼 길이 = 선 끝 - 갈림길 위치).
      2. 화면의 선 기울기로 방향을 잡으며 후진해 차체 중심을 갈림길 지점까지 되돌린다.
      3. 스퍼 반대쪽으로 제자리 회전하며 선이 화면 가운데에 오면 멈추고 다시 따라간다.
    탐색 정책은 그 갈림길을 막다른 길로 기억해, 다음에 만나면 다른 쪽으로 가고 "tee"로 기록한다.
    맵 빌더는 dead_end가 가리키는 갈림길(ref_seq)을 스퍼로 바꾼다.

급한 가지(V자) 들어가기 (drive/ask 모드)
    모서리 곡선 위에서 급하게 꺾여 나가는 가지는 조향 줄에서 갈라지는 모습이 보이지 않고,
    옆에서 들어와 내 선과 한 점(J)에서 만나는 선으로 보인다(합류와 같은 모양). JunctionTracker가
    "vee"(옆에서 들어와 빨리 붙고, 붙은 뒤 내 선이 그쪽으로 휨)를 내면 보통 갈림길처럼 탐색 정책이
    고른다. 그 가지를 고르면 차체 중심이 J에 올 때까지 따라간 뒤 그쪽으로 제자리 회전해 그 선을
    탄다. 안 고르면 J를 지날 때까지 갈림길 판정을 쉰다. 이 가지가 사실은 막다른 길이면 위의
    되돌아 나오기가 그대로 처리한다.

이벤트 위치
    odo_mm은 판정한 바닥 지점 위치다(차체 위치 + geom.*_ahead_mm). 확정이 몇 프레임 늦게 나도
    처음 감지한 프레임의 위치를 쓴다.
"""
import collections

from .linecam import Follower, JunctionTracker, PatchTracker, TeeTracker, analyze
from .topo import Explorer

MODES = ("drive", "ask", "passive")
OPP = {"left": "right", "right": "left"}
RECOVER_DEFAULT = {
    "window_mm": 600,        # 갈림길에서 고른 뒤 이 거리 안에서 선이 끝나면 막다른 길
    "end_frames": 3,         # 선 끝 모양이 이만큼 이어져야 인정
    "reverse_speed": 0.18,   # 후진 모터 명령
    "k_slope": 0.04,         # 후진 중 선 기울기(줄당 px)로 방향 잡기
    "k_offset": 0.10,        # 후진 중 선 가로 위치 보정(작게)
    "back_extra_mm": 10,     # 갈림길 지점보다 이만큼 더 물러난다
    "pivot_speed": 0.22,     # 제자리 회전 모터 명령
    "pivot_center": 0.15,    # 선이 화면 가운데에서 이 비율 안이면 회전 끝
    "pivot_timeout_s": 8.0,
    "suppress_mm": 40,       # 다시 따라가기 시작한 뒤 이 거리 동안 갈림길 판정 쉼
}


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
        self.v_plan = None          # 급한 가지로 들어갈 계획 {stop, side}
        self.vee_mute_until = -1e9  # 급한 가지를 판정한 뒤 J를 지날 때까지 갈림길 판정 쉼
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
        self.decision = None        # 마지막으로 고른 갈림길 {pos, side, kind, seq}
        self.split_as_tee = None    # 이번 갈림길이 이미 아는 막다른 길이면 그쪽(스퍼) 방향
        self.state = "follow"       # follow | reverse | pivot
        self.rc = dict(RECOVER_DEFAULT)
        self.rc.update(cfg.get("recover", {}))
        self._end_n = 0
        self._rev = None
        self.suppress_until = -1e9  # 되돌아 나온 직후 같은 갈림길을 다시 잡지 않게
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
        logged = self.log.log(ev["type"], pos, ts=ts, **ev["fields"])
        if ev.get("decision") is not None:
            ev["decision"]["seq"] = logged["seq"]
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
        elif ev["type"] == "dead_end":
            if ev["fields"]["ref_kind"] == "split":
                self.ex.on_dead_end(ev["fields"]["side"], p)

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
        if self.state == "reverse":
            return self._step_reverse(an, dt)
        if self.state == "pivot":
            return self._step_pivot(an, dt)
        if self.odo < self.suppress_until:
            # 막 되돌아 나와 다시 따라가기 시작함: 방금 지난 갈림길·스퍼를 다시 잡지 않는다
            color = self.patch.update(an)
            if color:
                self._emit(self._ev("patch", self._ago(self.cfg["patch"]["on_frames"]), self.patch_ahead,
                                    color=color))
            self.junc.update(an, self.follow.last_x)
            self.junc.in_split = False
            self.junc.state = "one"
            self.junc._pend = 0
            self.junc._gaps = []
            self.junc._last_gap = None
            return self._drive(an, dt)

        color = self.patch.update(an)
        if color:
            self._emit(self._ev("patch", self._ago(self.cfg["patch"]["on_frames"]), self.patch_ahead,
                                color=color))

        jevents = self.junc.update(an, self.follow.last_x)
        tee = self.tee.update(an, self.follow.last_x, self.junc.lw)
        if tee:
            self._emit(self._ev("tee", self._ago(self.cfg["tee"]["confirm_frames"]), self.ctrl_ahead,
                                side=tee["side"], measure=self.tee.last))

        muted = self.odo < self.vee_mute_until
        if muted:
            jevents = []
            self.junc.in_split = False

        # 1 -> 2: 교차인지 갈림길인지 판단
        if not muted and self.junc.just_changed and self.junc.state == "two":
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
                    # 교차 위에 스퍼가 겹쳐 있으면 엉뚱한 선으로 갈 수 있으므로 되돌아 나올 기준으로 삼는다
                    self.decision = {"pos": onset[1] + self.ctrl_ahead, "side": self.choice,
                                     "kind": "cross", "seq": None}
                    ev["decision"] = self.decision
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
                    dead = self.ex.known_dead()
                    if dead:
                        self.split_as_tee = dead          # 아는 막다른 길: 묻지 않고 반대쪽
                        self.choice = suggestion
                    elif self.mode == "ask" and self.ask_fn is not None:
                        self.choice = self.ask_fn(suggestion) or suggestion
                    else:
                        self.choice = suggestion
                    self.choice_t = self.t
                    if not dead:
                        self.decision = {"pos": onset[1] + self.ctrl_ahead, "side": self.choice,
                                         "kind": "split", "seq": None}

        for je in jevents:
            if je["type"] == "vee":
                if (self.mode != "passive" and self.choice is None and self.v_plan is None
                        and self.crossing is None and je["side"] is not None):
                    self._flush_pending()
                    self._on_vee(je)
                continue
            if je["type"] == "split":
                if self.crossing is not None:
                    self.crossing = None        # 교차를 다 지남(이미 cross로 기록)
                else:
                    self._emit_split(je["side"])
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

        if self.mode != "passive" and self._dead_end_seen(an, dt):
            self._start_recover()
            return 0.0, 0.0
        if self.v_plan is not None and self.odo >= self.v_plan["stop"]:
            self.state = "pivot"
            self._piv = {"t": 0.0, "away": False, "n": 0, "side": self.v_plan["side"], "kind": "vee"}
            self.log.log("note", self._odo(), msg="vee_pivot", dir=self.v_plan["side"])
            self.v_plan = None
            return 0.0, 0.0
        return self._drive(an, dt)

    def _on_vee(self, v):
        """급한 가지를 찾음: 탐색 정책이 고르게 하고, 그 가지면 J에서 회전할 계획을 세운다."""
        side = v["side"]
        v = dict(v, j_mm=self.ctrl_ahead)     # 붙은 순간 만나는 점(J)이 조향 줄 위에 있었다
        when = self._ago(self.cfg["junction"]["confirm_frames"])
        j_pos = when[1] + v["j_mm"]
        suggestion = self.ex.choose_split(j_pos)
        dead = self.ex.known_dead()
        if dead:
            choice = suggestion
        elif self.mode == "ask" and self.ask_fn is not None:
            choice = self.ask_fn(suggestion) or suggestion
        else:
            choice = suggestion
        self.vee_mute_until = j_pos + 120.0
        if dead:
            self._emit(self._ev("tee", when, v["j_mm"], side=dead, known_dead=True, vee=v))
            return
        ev = self._ev("split", when, v["j_mm"], choice=choice, cmd=choice, vee=v)
        if choice == side:
            self.v_plan = {"stop": j_pos, "side": side}
            self.decision = {"pos": j_pos, "side": choice, "kind": "split", "seq": None}
            ev["decision"] = self.decision
        self._emit(ev)

    def _drive(self, an, dt):
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
        self._integrate(left, right, dt)
        return left, right

    def _integrate(self, left, right, dt):
        """모터 명령으로 이동거리를 적분한다(후진은 뺀다)."""
        base = self.cfg["follow"]["base_speed"]
        self.odo += self.speed_mm_s * ((left + right) / 2.0) / base * dt
        self.last_cmd = (left, right)

    # ---------- 갈림길 기록 ----------
    def _emit_split(self, seen_side):
        kc = self.cfg["junction"]["confirm_frames"]
        side = seen_side or self.choice
        when = self.split_onset or self._ago(kc)
        if self.split_as_tee:
            # 아는 막다른 길을 지나감: 갈림길이 아니라 스퍼로 기록
            ev = self._ev("tee", when, self.ctrl_ahead, side=self.split_as_tee, known_dead=True)
            self.split_as_tee = None
        else:
            ev = self._ev("split", when, self.ctrl_ahead, choice=side, cmd=self.choice,
                          onset=self.junc.last_onset)
            if self.decision is not None and self.decision["kind"] == "split" and self.decision["seq"] is None:
                self.decision["side"] = side
                ev["decision"] = self.decision
        self._emit(ev)

    # ---------- 막다른 길 ----------
    def _dead_end_seen(self, an, dt):
        """고른 갈림길에서 window_mm 안인데 선이 끝나 보이면 True."""
        d = self.decision
        if d is None or (self.odo + self.ctrl_ahead) - d["pos"] > self.rc["window_mm"]:
            self._end_n = 0
            return False
        rows = an.raw
        far_empty = all(len(rows[i]) == 0 for i in range(an.ctrl_idx + 1))
        near = [r for i in range(an.ctrl_idx + 1, len(rows)) for r in rows[i]]
        ended = far_empty and len(near) > 0
        lost = all(len(r) == 0 for r in rows) and self.lost_s + dt >= 0.3
        if ended or lost:
            self._end_n += 1
        else:
            self._end_n = 0
        return self._end_n >= self.rc["end_frames"] or lost

    def _start_recover(self):
        d = self.decision
        # 아직 기록 안 된 갈림길이 있으면 먼저 기록(가지가 화면에서 빠지기 전에 선이 끝난 경우)
        if self.junc.state == "two" and self.junc.in_split and self.crossing is None:
            self._emit_split(None)
        self.junc.in_split = False
        self.junc.state = "one"
        self.choice = None
        self.crossing = None
        self.split_onset = None
        self._flush_pending()
        end_pos = self.odo + self.ctrl_ahead
        ev = self._ev("dead_end", (self.t, self.odo), self.ctrl_ahead, side=d["side"],
                      ref_seq=d.get("seq"), ref_kind=d["kind"],
                      spur_mm=round(end_pos - d["pos"], 1))
        self._dispatch(ev)
        self.decision = None
        self._end_n = 0
        self.state = "reverse"
        self._rev = {"target": d["pos"] - self.rc["back_extra_mm"], "side": d["side"], "t": 0.0,
                     "limit_s": 3.0 + 2.5 * (end_pos - d["pos"] + 200) / max(self.speed_mm_s, 1.0)}
        self.follow.prev_err = 0.0
        self.log.log("note", self._odo(), msg="recover_reverse", target=round(self._rev["target"], 1))

    def _line_at(self, an, i, ref):
        lw = self.junc.lw
        from .linecam import _line_x
        cand = [_line_x(r, ref, lw) for r in an.raw[i]]
        return min(cand, key=lambda v: abs(v - ref)) if cand else None

    def _step_reverse(self, an, dt):
        """선 기울기로 방향을 잡으며 후진한다. 앞을 보며 뒤로 가므로 위치 오차보다 방향을 맞춘다."""
        r = self._rev
        r["t"] += dt
        self.t += dt
        ref = self.follow.last_x if self.follow.last_x is not None else an.w / 2.0
        x0 = self._line_at(an, an.ctrl_idx, ref)
        turn = 0.0
        if x0 is not None:
            self.follow.last_x = x0
            k = 3
            x1 = self._line_at(an, max(0, an.ctrl_idx - k), x0)
            e = (x0 - an.w / 2.0) / (an.w / 2.0)
            slope = (x1 - x0) / float(k) if x1 is not None else 0.0
            turn = -self.rc["k_slope"] * slope + self.rc["k_offset"] * e
        v = self.rc["reverse_speed"]
        left, right = -v + turn, -v - turn
        self._integrate(left, right, dt)
        if self.odo <= r["target"]:
            self.state = "pivot"
            self._piv = {"t": 0.0, "away": False, "n": 0, "side": OPP[r["side"]]}
            self._piv["kind"] = "recover"
            self.log.log("note", self._odo(), msg="recover_pivot", dir=self._piv["side"])
            return 0.0, 0.0
        if r["t"] > r["limit_s"]:
            self._finish("recover_failed_reverse")
            return 0.0, 0.0
        return left, right

    def _step_pivot(self, an, dt):
        """스퍼 반대쪽으로 제자리 회전: 스퍼 선이 가운데를 벗어난 뒤, 다른 선이 가운데에 오면 끝."""
        p = self._piv
        p["t"] += dt
        self.t += dt
        segs = [_c for _c in an.rows[an.ctrl_idx][1]]
        c = an.w / 2.0
        best = min(segs, key=lambda v: abs(v - c)) if segs else None
        e = None if best is None else abs(best - c) / c
        if not p["away"]:
            if p["t"] > 0.3 and (e is None or e > 0.35):
                p["away"] = True
        elif e is not None and e < self.rc["pivot_center"]:
            p["n"] += 1
            if p["n"] >= 2:
                self._rejoin(an, best)
                return 0.0, 0.0
        else:
            p["n"] = 0
        if p["t"] > self.rc["pivot_timeout_s"]:
            self._finish("recover_failed_pivot")
            return 0.0, 0.0
        w = self.rc["pivot_speed"]
        if p["side"] == "right":
            return w, -w
        return -w, w

    def _rejoin(self, an, x):
        self.state = "follow"
        self.follow.last_x = float(x)
        self.follow.prev_err = 0.0
        self.junc.state = "one"
        self.junc.in_split = False
        self.junc._pend = 0
        self.junc._gaps = []
        self.junc._pair_slopes = []
        self.tee.armed = False
        self.tee._off = 0
        fr = an.patch_fracs
        best = max(fr, key=fr.get) if fr else None
        if best is not None and fr[best] >= self.cfg["patch"]["off_frac"]:
            self.patch.active = best
        self.suppress_until = self.odo + self.rc["suppress_mm"]
        self.lost_s = 0.0
        self.log.log("note", self._odo(), msg=self._piv.get("kind", "recover") + "_done")

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
