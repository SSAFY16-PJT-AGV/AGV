# -*- coding: utf-8 -*-
"""완성된 맵 위에서 목표 노드로 가기: 이벤트로 맵 위 위치를 추적하고, 갈림길에서 갈 쪽을 고른다.

ExploreRunner가 탐색 정책(topo/explorer.py)과 똑같이 부른다(on_patch, choose_split, ...).
그래서 라인 추종·갈림길 판정·V자 들어가기·막다른 길 되돌아 나오기 코드를 그대로 쓴다.

위치 추적(가설)
    가설 하나 = "지금 엣지 E 위에 있고, E의 시작 노드를 odo s0에서 지났다".
    이벤트(patch·split·merge·cross·tee)가 오면 각 가설을 E 끝 노드와 맞춰 본다.
      - 맞으면 다음 엣지로 넘어간다(split은 실제로 따라간 쪽 엣지로).
      - 합류·교차·스퍼 갈림·갈림길 노드는 검출을 놓쳤을 수 있어서 비용을 내고 건너뛸 수 있다.
        중간 노드(via)는 비용 없이 건너뛴다. 패치는 건너뛰지 않는다(색으로 확실히 보임).
      - 합류·교차·스퍼(tee) 이벤트가 맵에 없는 것이면 비용을 내고 무시한다.
        패치에 붙은 스퍼·패치 위 교차는 맵에서 패치 속성이라 패치 가까이에서는 공짜다.
      - 이동거리가 엣지 길이와 많이 다르면 비용을 더한다.
    가장 싼 가설들(비용 차 SLACK 안)만 남긴다. 아무 가설과도 안 맞는 패치가 보이면 그 패치에서
    다시 시작한다(패치 색은 맵에 하나씩이라 바로 위치를 안다).

갈림길 고르기
    가설마다 "앞의 갈림길 노드 S"(갈림길 해석)나 "패치·스퍼 갈림의 스퍼"(스퍼 해석)를 찾는다.
    갈림길 해석은 각 쪽 엣지에서 목표까지 남은 거리로, 스퍼 해석은 스퍼 쪽에 벌점을 줘서 비교하고,
    해석들의 손해 합이 작은 쪽을 고른다. 모두 스퍼 해석이면 runner에 "아는 막다른 길"로 알려
    묻지 않고 반대쪽으로 지나가게 한다.

스퍼 끝(D_*) 목표
    부모 노드(패치·스퍼 갈림 T)까지 가는 길을 고른다. 부모 패치를 지나면 "무장"하고, 그 패치 근처에서
    스퍼가 보이면(tee) runner에 진입을 지시한다(pop_stop이 {type: enter_spur, via: tee, pos: J, side}).
    runner는 J에서 스퍼 쪽으로 제자리 회전해 스퍼를 탄다. 스퍼가 갈림길로 보이면(choose_split) 스퍼 쪽을
    고른다(via: split). 스퍼 갈림(T) 노드가 부모면 T를 본 순간 진입한다. 선 끝에서 runner가 spur_arrived()를
    부르면 도착으로 처리하고, runner는 조금 더 가서 주차한다. 다음 목표로 재개하면 runner가 후진해 J로
    돌아와 반대쪽으로 돌아 본선에 오르고 spur_exited()를 부른다. 스퍼 안에서 본 이벤트는 위치 추적에 안 쓴다.
    부모 패치를 지나 SPUR_WINDOW_MM 안에 스퍼를 못 보면, 자유 목표(set_goal)면 한 바퀴 돌아 SPUR_RETRIES번까지
    다시 시도하고, 정해진 경로(set_route)면 예약 밖으로 돌 수 없으니 그 자리에서 선다(nav_spur_missed).

반대 방향·유턴
    가설의 엣지는 주행 엣지(정방향 E, 반대 방향 E~r)라서 방향을 품는다. 노드가 어떻게 보이는지(갈림길이냐
    합류냐, 스퍼가 어느 쪽이냐)는 NavMap이 방향에 따라 알려 준다. 경로 비용에는 유턴(패치 가운데·스퍼 끝)이
    들어 있어서, 패치에 닿았을 때 "여기서 돌아가는 게 싸다"면 runner에 유턴을 지시한다
    ({type: uturn, pos: 패치 가운데, side: 스퍼 반대쪽}). runner가 돌고 나서 uturned(odo)를 부르면 가설을
    반대 방향 엣지로 바꾼다. 스퍼 끝에서 나갈 때는 spur_exit_side()로 들어온 방향을 이어 갈지(스퍼 반대쪽으로
    회전) 되돌아갈지(스퍼 쪽으로 회전) 정한다.

정지
    목표 노드에 맞는 이벤트가 오면 정지를 요청한다(pop_stop). 패치는 패치 가운데에 차체가 오도록
    stop_offset_mm만큼 더 간 뒤 멈춘다. 정해진 경로(set_route)의 해제 노드(released_until)가 끝이
    아니면 대기(hold)하고, extend()로 해제가 늘면 runner.resume()으로 이어 간다.
    목표가 갈림길이면 갈림길에 들어가기 전에 대기한다(choose_split이 None).

표준 라이브러리만 쓴다. Python 3.6 호환.
"""
import collections

from .navmap import FWD, REV

SKIP_COST = {"via": 0.0, "merge": 1.0, "cross": 1.0, "tee": 1.0, "split": 2.0}
SPURIOUS_COST = {"merge": 1.0, "cross": 1.0, "tee": 1.0}
NODE_OF_EVENT = {"patch": "patch", "split": "split", "merge": "merge", "cross": "cross", "tee": "tee"}
ABSORB_MM = 200.0          # 패치에서 이 거리 안의 스퍼·교차 이벤트는 패치의 일부
SLACK = 1.0                # 가장 싼 가설보다 이만큼 비싼 가설까지 남김
CHOICE_SLACK = 0.5         # 갈림길 고를 때 쓰는 해석의 비용 폭
MAX_HYPS = 40
MAX_SKIP = 3
SPUR_PENALTY = 2000.0      # 스퍼로 잘못 들어가면 되돌아 나오는 손해(mm 환산)
SPUR_WINDOW_MM = 260.0     # 부모 패치 앞 가장자리를 본 뒤 이 거리 안에서 스퍼를 찾음
SPUR_RETRIES = 2           # 스퍼를 못 봤을 때 한 바퀴 돌아 다시 시도하는 횟수(자유 목표만)
UNREACHABLE = 1e6
NEAR_SPLIT_MM = 200.0      # 유턴·스퍼 탈출 회전 직후 이 거리 안의 갈림길은 회전으로 가지를 고른다
OPP = {"left": "right", "right": "left"}

Hyp = collections.namedtuple("Hyp", "edge s0 cost last came")   # came: last 노드에 들어올 때 탄 엣지
Hyp.__new__.__defaults__ = (None,)


class Navigator(object):
    def __init__(self, navmap, goal=None, start=None, stop_offset_mm=80.0, hold_at_end=False,
                 log=None, default_side="left", start_dir=FWD):
        """start: 처음 보게 될 노드(그 노드 조금 앞에 로봇을 놓음). None이면 위치를 모르는 채로
        출발해 처음 보이는 패치에서 위치를 잡는다.
        start_dir: 탐색한 방향(FWD, 기본)으로 놓았는지 반대(REV)인지. None이면 둘 다 후보.
        hold_at_end: 최종 목표에 도착해도 끝내지 않고 대기한다(서버 주문을 이어 받을 때)."""
        self.m = navmap
        self.stop_offset_mm = float(stop_offset_mm)
        self.hold_at_end = hold_at_end
        self._log = log
        self.default_side = default_side
        self.target = None          # 지금 멈출 노드(해제 노드)
        self.final = None           # 경로의 마지막 노드
        self.allowed = None         # 정해진 경로의 엣지 집합(None이면 최단 경로 자유)
        self.route = None
        self._route_pos = 0         # 경로에서 마지막으로 지난(또는 서 있는) 노드의 순번
        self._target_i = None       # 해제 노드의 경로 순번(같은 노드가 경로에 두 번 나올 수 있음)
        self.at_node = None         # 대기 중인 노드
        self._stop = None
        self._early = None          # 미리 정지를 요청한 목표 패치(on_patch_early)
        self._dead_now = None
        self._choice_ctx = None
        self._pre_split = None
        self.spur = None            # 스퍼 진입 상태 {state: armed|entering|parked|exiting, parent, side, pos, hyps}
        self._spur_tries = 0
        self._uturn = None          # 지시한 유턴 {edge: 돈 뒤 탈 엣지, node, came}
        self.done = False           # 탐색 정책과 같은 이름(runner가 봄). 이동은 pop_stop으로 멈춘다
        self.done_reason = None
        self.n_obs = 0
        self.relocalized = 0
        self._patches_seen = 0      # 목표를 정한 뒤 지난 패치 수(못 찾고 맴도는지)
        if start is not None:
            if start not in self.m.nodes:
                raise ValueError(u"맵에 없는 시작 노드: %s" % start)
            self.hyps = [Hyp(e, None, 0.0, None) for e in self.m.edges_into(start, start_dir)]
            if not self.hyps:
                raise ValueError(u"시작 노드로 들어오는 길이 없어요: %s (%s)" % (
                    start, u"반대 방향" if start_dir == REV else u"탐색 방향"))
        else:
            self.hyps = self._unknown()
        if goal is not None:
            self.set_goal(goal)

    def _unknown(self):
        return [Hyp(e, None, 0.0, None) for e in self.m.main_edges()]

    def bind_logger(self, log):
        """runner가 부른다: 판단 메모(nav_*)를 이벤트 로그의 note로 남긴다."""
        self._log = log
        if self.target is not None:
            self._note("nav_target", target=self.target, final=self.final, route=self.route,
                       start=self.where())

    def _note(self, msg, **kw):
        if self._log is not None:
            self._log(msg, **kw)

    # ---------- 목표 ----------
    def set_goal(self, node):
        """최단 경로로 node까지 간다."""
        self._check_node(node)
        self.route, self.allowed = None, None
        self.target = self.final = node
        self._after_new_target()

    def set_route(self, nodes, release=None):
        """정해진 노드 목록을 따라간다(서버 주문의 nodes). release(released_until)까지만 가고 대기."""
        if not nodes:
            raise ValueError(u"빈 경로")
        for n in nodes:
            self._check_node(n)
        release = release or nodes[-1]
        if release not in nodes:
            raise ValueError(u"해제 노드가 경로에 없어요: %s" % release)
        self.allowed = self.m.route_edges(nodes)
        self.route = list(nodes)
        self._route_pos = 0
        self.final = nodes[-1]
        self.target = release
        self._target_i = self._route_index(release)
        self._after_new_target()

    def _route_index(self, node):
        """경로에서 node의 순번: 지금 위치 다음에 처음 나오는 곳(경로 첫 노드에 서 있으면 0)."""
        for i in range(max(1, self._route_pos), len(self.route)):
            if self.route[i] == node:
                return i
        return 0 if self.route and self.route[0] == node else None

    def _route_advance(self, node):
        """경로 다음 몇 노드 안에서 node를 지났으면 진행 순번을 올린다."""
        if self.route is None:
            return
        for i in range(self._route_pos + 1, min(len(self.route), self._route_pos + 5)):
            if self.route[i] == node:
                self._route_pos = i
                return

    def _route_reached(self, node):
        """경로 모드에서 node가 지금 멈출 해제 노드(진행 순번까지 맞음)인가."""
        if self.route is None:
            return node == self._approach(self.target)
        if node != self._approach(self.target):
            return False
        need = self._target_i if self._approach(self.target) == self.target else (self._target_i or 0) - 1
        return self._route_pos >= (need or 0)

    def extend(self, release):
        """해제 노드를 늘린다. 대기 중이었으면 runner.resume()을 불러야 다시 간다."""
        if self.route is None or release not in self.route:
            raise ValueError(u"지금 경로에 없는 해제 노드: %s" % release)
        i = self._route_index(release)
        if i is None or (self._target_i is not None and i < self._target_i):
            raise ValueError(u"해제 노드를 줄일 수는 없어요")
        self.target = release
        self._target_i = i
        self._after_new_target()

    def check_node(self, n):
        """목표로 쓸 수 있는 노드인지 확인한다(아니면 ValueError)."""
        self._check_node(n)

    def _check_node(self, n):
        if n not in self.m.nodes:
            raise ValueError(u"맵에 없는 노드: %s" % n)
        if self.m.kind(n) == "spur_end":
            par = self.m.spur_parent(n)
            if par is None or not self.m.spur_side_at(par):
                raise ValueError(u"스퍼 끝(%s)의 부모 노드나 스퍼 방향을 맵에서 못 찾았어요" % n)

    def _approach(self, node):
        """그 노드에 가려면 본선에서 도착해야 하는 노드(스퍼 끝이면 부모)."""
        if node is not None and self.m.kind(node) == "spur_end":
            return self.m.spur_parent(node)
        return node

    def _after_new_target(self):
        self._stop = None
        self._early = None
        self._patches_seen = 0
        if self.spur is not None and self.spur["state"] == "armed":
            self.spur = None
        self._spur_tries = 0
        if self.at_node is not None and self.at_node == self.target:
            # 이미 그 자리에 서 있음
            self._request_stop(None, self.target)
            return
        if self.at_node is not None and self.m.kind(self.at_node) == "patch" and self.spur is None:
            # 패치 가운데에 서 있다가 새 목표: 되돌아가는 게 싸면 그 자리에서 유턴
            self._maybe_uturn(self.at_node, None)
        R = self._R()
        if R is not None and self._confident() and not any(h.edge in R for h in self._best()):
            self._note("nav_unreachable", target=self.target, edges=[h.edge for h in self._best()])
        self._note("nav_target", target=self.target, final=self.final,
                   route=self.route, start=self.where())

    def _R(self):
        if self.target is None:
            return None
        return self.m.dist_to(self._approach(self.target), self.allowed)

    def pop_stop(self):
        """runner가 이벤트마다 부른다. None 또는 {pos, reason, hold, node}(pos None이면 지금 바로)."""
        s, self._stop = self._stop, None
        return s

    def _request_stop(self, pos, node, reason=None):
        hold = self.hold_at_end or node != self.final
        if reason is None:
            reason = "nav_hold" if hold else "nav_arrived"
        off = self.stop_offset_mm if (pos is not None and self.m.kind(node) == "patch") else 0.0
        self._stop = {"pos": None if pos is None else pos + off, "reason": reason, "hold": hold,
                      "node": node}
        self.at_node = node
        if node == self.final and not hold:
            self.target = None

    def resumed(self):
        """runner가 대기를 풀 때 부른다."""
        self.at_node = None
        if self.spur is not None and self.spur["state"] == "parked":
            self.spur["state"] = "exiting"

    # ---------- 유턴 ----------
    def _maybe_uturn(self, node, pos):
        """node(패치)에 막 닿았거나 서 있을 때, 목표로 가는 가장 싼 길이 되돌아가는 것이면 유턴을 지시한다."""
        if self.target is None or not self.m.uturn_ok(node) or self.m.kind(node) != "patch":
            return False
        if node == self._approach(self.target) and self._route_reached(node):
            return False            # 도착한 곳(스퍼 부모면 스퍼로 들어감)
        best = self._best()
        cames = set(h.came for h in best if h.last == node)
        if len(cames) != 1 or None in cames:
            return False
        came = cames.pop()
        nxt = self.m.best_next(came, self._approach(self.target), self.allowed)
        if nxt is None or not nxt[1]:
            return False
        sp = self.m.spur_side_at(node, self.m.dir(came))
        side = OPP[sp] if sp else self.default_side         # 스퍼 쪽으로 돌면 스퍼에 걸려 멈춤
        self._uturn = {"edge": nxt[0], "node": node, "came": came}
        self._stop = {"type": "uturn", "pos": None if pos is None else pos + self.stop_offset_mm,
                      "side": side, "node": node, "prefer": self._near_branch(nxt[0])}
        self._note("nav_uturn", node=node, side=side, then=nxt[0])
        return True

    def _near_branch(self, edge_after):
        """회전해서 edge_after를 탈 때 바로 앞(NEAR_SPLIT_MM 안)이 갈림길이면 탈 가지 쪽(left/right).
        회전이 끝나면 갈라지는 모습이 이미 화면에 있어 갈림길 판정을 못 하므로, runner가 회전 직후
        그쪽 가지를 고른 채로 따라가게 한다. 아니면 None."""
        e = self.m.edges[edge_after]
        n2, d = e["to"], e["dir"]
        if e["len"] > NEAR_SPLIT_MM or self.m.kind_seen(n2, d) != "split":
            return None
        br = self.m.branches_seen(n2, d)
        R = self._R()
        if R is None or len(br) < 2:
            return None
        want = min(br, key=lambda s_: self.m.edges[br[s_]]["len"] + R.get(br[s_], UNREACHABLE))
        if br[want] not in R:
            return None
        self._note("nav_near_branch", node=n2, want=want)
        return want

    def uturned(self, odo=None):
        """runner가 유턴을 마쳤을 때 부른다: 반대 방향 엣지로 이어 간다."""
        u, self._uturn = self._uturn, None
        if u is None:
            return
        self.hyps = [Hyp(u["edge"], odo, 0.0, u["node"], u["came"])]
        self._note("nav_uturned", where=self.where(odo))

    # ---------- 스퍼 끝 ----------
    def _arm_spur(self, parent, pos):
        best = self._best()
        d = self.m.dir(best[0].edge) if best else FWD
        side = self.m.spur_side_at(parent, d)
        self.spur = {"state": "armed", "parent": parent, "side": side, "pos": pos, "hyps": None,
                     "node": self.target}
        self._note("nav_spur_armed", parent=parent, side=side, node=self.target)
        if self.m.kind(parent) == "tee":
            self._enter_spur("tee", pos)           # 스퍼 갈림을 본 순간이 곧 갈림 지점
        else:
            # 패치 근처에서 스퍼를 못 보면 runner가 spur_missed()를 부른다
            self._stop = {"type": "spur_deadline", "pos": pos + SPUR_WINDOW_MM, "node": parent}

    def _enter_spur(self, via, pos):
        sp = self.spur
        sp["state"] = "entering"
        if sp["hyps"] is None:
            sp["hyps"] = list(self.hyps)
        sp["dir"] = self.m.dir(sp["hyps"][0].edge) if sp["hyps"] else FWD
        self._stop = {"type": "enter_spur", "via": via, "pos": pos, "side": sp["side"], "node": sp["node"]}
        self._note("nav_spur_enter", via=via, side=sp["side"], node=sp["node"])

    def _spur_busy(self):
        return self.spur is not None and self.spur["state"] in ("entering", "parked", "exiting")

    def on_tee_early(self, pos, side=None):
        """runner가 교차 판정으로 스퍼(tee) 기록을 보류할 때 부른다. 무장 중이면 바로 진입 지시."""
        sp = self.spur
        if sp is not None and sp["state"] == "armed" and (side is None or side == sp["side"]) \
                and pos - sp["pos"] < SPUR_WINDOW_MM:
            self._enter_spur("tee", pos)

    def spur_missed(self):
        """부모 패치를 지났는데 스퍼를 못 봄(runner가 부름)."""
        sp = self.spur
        if sp is None or sp["state"] != "armed":
            return
        self._spur_tries += 1
        self.spur = None
        if self.allowed is None and self._spur_tries <= SPUR_RETRIES:
            self._note("nav_spur_retry", node=sp["node"], tries=self._spur_tries)
            return                  # 목표는 그대로: 한 바퀴 돌아 부모 패치에서 다시 무장
        self._note("nav_spur_missed", node=sp["node"], tries=self._spur_tries)
        self._request_stop(None, sp["parent"], reason="nav_spur_missed")
        self._stop["hold"] = True
        self.at_node = None         # 패치는 이미 지나서 그 자리에 있는 게 아님

    def spur_arrived(self, side=None):
        """runner가 고른 쪽 선이 끝난 것을 봤을 때 부른다. 스퍼 끝 목표로 들어가는 중이면 도착 정보, 아니면 None
        (그러면 runner는 보통처럼 되돌아 나온다)."""
        sp = self.spur
        if sp is None or sp["state"] != "entering":
            return None
        sp["state"] = "parked"
        node = sp["node"]
        hold = self.hold_at_end or node != self.final
        self._note("nav_reached", node=node, final=node == self.final)
        self.at_node = node
        if node == self.final and not hold:
            self.target = None
        return {"hold": hold, "reason": "nav_hold" if hold else "nav_arrived", "node": node,
                "spur_mm": self.m.spur_length(node)}

    def spur_exit_side(self):
        """runner가 스퍼에서 후진해 갈림 지점에 왔을 때 돌 쪽을 묻는다. 들어온 방향을 이어 가면 스퍼 반대쪽,
        되돌아가면 스퍼 쪽으로 돈다(어느 쪽이든 90도)."""
        sp = self.spur
        if sp is None:
            return None
        cont = sp.get("hyps") or []
        par, d = sp["parent"], sp.get("dir", FWD)
        back = [Hyp(f, None, 0.0, par) for f in self.m.edges_out_of(par, REV if d == FWD else FWD)]
        sp["exit_hyps"] = cont
        side = OPP[sp["side"]]
        R = self._R()
        if R is not None and back:
            def cost(hs):
                vs = [self.m.edges[h.edge]["len"] + R[h.edge] for h in hs if h.edge in R]
                return min(vs) if vs else None
            c_cont, c_back = cost(cont), cost(back)
            if c_back is not None and (c_cont is None or c_back < c_cont):
                sp["exit_hyps"] = back
                side = sp["side"]
        prefer = self._near_branch(sp["exit_hyps"][0].edge) if len(sp["exit_hyps"]) == 1 else None
        self._note("nav_spur_exit", side=side, back=side == sp["side"], prefer=prefer)
        return side, prefer

    def spur_exited(self, odo=None):
        """runner가 스퍼에서 후진해 본선에 다시 올랐을 때 부른다."""
        sp, self.spur = self.spur, None
        hs = (sp or {}).get("exit_hyps") or (sp or {}).get("hyps")
        if hs:
            self.hyps = [h._replace(s0=odo) if h.s0 is None else h for h in hs]
        self._note("nav_spur_exited", where=self.where())

    # ---------- 가설 ----------
    def _best(self):
        if not self.hyps:
            return []
        m = min(h.cost for h in self.hyps)
        return [h for h in self.hyps if h.cost <= m + 1e-9]

    def _confident(self):
        return all(h.s0 is not None for h in self.hyps) and len(set(h.edge for h in self._best())) <= 2

    def _walk(self, h):
        """가설 h의 엣지 끝부터 건너뛸 수 있는 노드를 따라가며 (들어온 엣지, 노드, 누적 길이, 건너뛴 비용)."""
        stack = [(h.edge, 0.0, 0.0, 0)]
        seen = set()
        while stack:
            e, L, c, dep = stack.pop()
            if (e, dep) in seen:
                continue
            seen.add((e, dep))
            L2 = L + self.m.edges[e]["len"]
            node = self.m.edges[e]["to"]
            yield e, node, L2, c
            d = self.m.dir(e)
            k = self.m.kind_seen(node, d)
            if k in SKIP_COST:
                step = 0 if k == "via" else 1
                if dep + step > MAX_SKIP:
                    continue
                nexts = list(self.m.branches_seen(node, d).values()) if k == "split" else self.m.next_edges(e)
                for f in nexts:
                    if f in self.m.edges and not self.m.edges[f]["spur"]:
                        stack.append((f, L2, c + SKIP_COST[k], dep + step))

    @staticmethod
    def _dist_pen(d, L):
        if d is None:
            return 0.0
        tol = 150.0 + 0.3 * L
        err = abs(d - L)
        if err <= tol:
            return 0.0
        return 1.0 if err <= 2 * tol else 2.0

    def _absorbed(self, h, kind, odo):
        """스퍼(tee)·교차 이벤트가 패치 속성(패치 스퍼, 패치 위 교차)으로 설명되는가."""
        def fits(node):
            n = self.m.nodes[node]
            if n["kind"] != "patch":
                return False
            return bool(n["spur_side"]) if kind == "tee" else n["crossing"]
        d = None if h.s0 is None else odo - h.s0
        if h.last is not None and fits(h.last) and (d is None or d < ABSORB_MM):
            return True
        nxt = self.m.edges[h.edge]["to"]
        if fits(nxt) and (d is None or abs(self.m.edges[h.edge]["len"] - d) < ABSORB_MM):
            return True
        return False

    def _observe(self, kind, odo, color=None, side=None):
        self.n_obs += 1
        if self._spur_busy():
            sp = self.spur
            par = self.m.nodes[sp["parent"]]
            if not (sp["state"] == "entering" and kind == "patch" and par["kind"] == "patch"
                    and par["color"] == color):
                return              # 스퍼 안·드나들 때 본 것은 위치 추적에 안 씀
            # 교차 판정으로 늦게 온 부모 패치: 위치만 갱신하고 나올 때 쓸 가설로 둔다
            sp["hyps"] = None
        want = NODE_OF_EVENT[kind]
        cands = []
        for h in self.hyps:
            if kind in SPURIOUS_COST:
                c = 0.0 if self._absorbed(h, kind, odo) else SPURIOUS_COST[kind]
                cands.append(h._replace(cost=h.cost + c))
            d = None if h.s0 is None else odo - h.s0
            for e, node, L, sc in self._walk(h):
                n = self.m.nodes[node]
                if self.m.kind_seen(node, self.m.dir(e)) != want or (kind == "patch" and n["color"] != color):
                    continue
                if kind == "split":
                    br = self.m.branches_seen(node, self.m.dir(e))
                    succ = [br[side]] if side in br else []
                else:
                    succ = self.m.next_edges(e)
                pen = self._dist_pen(d, L)
                for f in succ:
                    cands.append(Hyp(f, odo, h.cost + sc + pen, node, e))
                if not succ and node == self.target:
                    # 목표가 막다른 노드(나가는 길 없음)여도 도착은 인정
                    cands.append(Hyp(e, odo, h.cost + sc + pen, node, e))
        if not cands:
            if kind == "patch" and color in self.m.by_color:
                p = self.m.by_color[color]
                self.hyps = [Hyp(f, odo, 0.0, p) for f in self.m.out[p]]    # 방향도 모름
                self.relocalized += 1
                self._note("nav_relocalized", node=p)
            else:
                self._note("nav_unmatched", event=kind, side=side, color=color)
                return
        else:
            m = min(h.cost for h in cands)
            best = {}
            for h in sorted(cands, key=lambda h: h.cost):
                if h.cost > m + SLACK:
                    break
                if h.edge not in best:
                    best[h.edge] = h._replace(cost=h.cost - m)
            self.hyps = sorted(best.values(), key=lambda h: h.cost)[:MAX_HYPS]
        if self._spur_busy():
            if self.spur["hyps"] is None:
                self.spur["hyps"] = list(self.hyps)
            return
        self._check_arrival(odo, kind)

    def on_patch_early(self, color, pos):
        """runner가 교차 판정 때문에 패치 기록을 보류할 때 부른다. 목표 패치면 정지 위치만 먼저 정한다.
        패치 색은 맵에 하나씩이라 색만으로 목표인지 안다. 스퍼 끝 목표면 그 부모 패치에서 무장한다."""
        par = self._approach(self.target)
        if par is not None and par != self.target and self.spur is None \
                and self.m.kind(par) == "patch" and self.m.nodes[par]["color"] == color \
                and (self.route is None or (self._target_i or 0) - 1 - self._route_pos <= 3):
            self._arm_spur(par, pos)
            return
        t = self.target
        if t is None or self._early == t or self.m.kind(t) != "patch" or self.m.nodes[t]["color"] != color:
            return
        if self.route is not None and (self._target_i or 0) - self._route_pos > 3:
            return                  # 경로상 아직 멀었음(같은 패치를 나중에 다시 지나는 경로)
        self._early = t
        self._note("nav_reached_early", node=t)
        self._request_stop(pos, t)

    def _check_arrival(self, odo, kind=None):
        if self.target is None or self._stop is not None:
            return
        if kind == "patch":
            self._patches_seen += 1
            if self._patches_seen > 3 * max(1, len(self.m.by_color)) + 2:
                # 목표를 못 찾고 맵을 몇 바퀴 돎: 그만 선다
                self._note("nav_gave_up", target=self.target, patches=self._patches_seen)
                self._request_stop(None, self.target, reason="nav_gave_up")
                self._stop["hold"] = False
                self.target = None
                return
        best = self._best()
        par = self._approach(self.target)
        just = best and all(h.s0 == odo for h in best) and len(set(h.last for h in best)) == 1
        if just:
            self._route_advance(best[0].last)
        if par != self.target:
            if self.spur is None and just and best[0].last == par and self._route_reached(par):
                self._arm_spur(par, odo)
                return
        elif just and best[0].last == self.target and self._route_reached(self.target):
            node = self.target
            if self._early == node:          # 이미 정지를 요청함
                self._early = None
                return
            self._note("nav_reached", node=node, final=node == self.final)
            self._request_stop(odo, node)
            return
        if kind == "patch" and just:
            if self._maybe_uturn(best[0].last, odo):
                return
        if self.allowed is not None and self._confident() and best:
            # 정해진 경로를 벗어남: 예약 안 된 길로 더 가지 않게 바로 선다
            if all(h.edge not in self.allowed for h in best):
                self._request_stop(None, best[0].last or self.target, reason="nav_off_route")
                self._stop["hold"] = True
                self._note("nav_off_route", edges=[h.edge for h in best])

    # ---------- runner가 부르는 것(탐색 정책과 같은 이름) ----------
    def on_patch(self, color, odo):
        self._observe("patch", odo, color=color)

    def on_merge(self, odo):
        self._observe("merge", odo)

    def on_cross(self, odo):
        self._observe("cross", odo)

    def on_tee(self, odo, side=None):
        sp = self.spur
        if sp is not None and sp["state"] == "armed":
            if (side is None or side == sp["side"]) and odo - sp["pos"] < SPUR_WINDOW_MM:
                self._enter_spur("tee", odo)
                return
        if sp is not None and sp["state"] == "entering" and sp["hyps"] is None:
            sp["hyps"] = list(self.hyps)
        self._observe("tee", odo, side=side)

    def on_split(self, choice, odo):
        if self._spur_busy():
            return                  # 스퍼로 들어간 갈림(via split)
        self._pre_split = list(self.hyps)
        self._choice_ctx = None
        self._observe("split", odo, side=choice)

    def on_dead_end(self, side, odo):
        """고른 갈림길이 사실은 스퍼였다: 갈림길 직전 가설로 돌아간다(되돌아 나와 원래 선을 탄다)."""
        if self._pre_split is not None:
            self.hyps = self._pre_split
            self._pre_split = None
        self._note("nav_dead_end", side=side)

    def on_gap(self):
        self.hyps = self._unknown()

    def known_dead(self):
        d, self._dead_now = self._dead_now, None
        return d

    def choose_split(self, odo):
        """갈림길(또는 V자)을 앞에서 봤을 때 갈 쪽. 목표 갈림길 앞에서 대기해야 하면 None."""
        sp = self.spur
        if sp is not None and sp["state"] == "armed" and odo - sp["pos"] < SPUR_WINDOW_MM:
            self._enter_spur("split", odo)          # 스퍼가 갈림길로 보임: 스퍼 쪽으로
            return sp["side"]
        if self._spur_busy():
            return self.default_side
        interps = []
        for h in self.hyps:
            d = None if h.s0 is None else odo - h.s0
            hd = self.m.dir(h.edge)
            if h.last is not None and self.m.spur_side_at(h.last, hd) and self.m.kind(h.last) == "patch" \
                    and (d is None or d < ABSORB_MM):
                interps.append((h.cost, "spur", self.m.spur_side_at(h.last, hd), None))
            for e, node, L, sc in self._walk(h):
                ed = self.m.dir(e)
                k = self.m.kind_seen(node, ed)
                pen = self._dist_pen(d, L)
                if k == "split":
                    interps.append((h.cost + sc + pen, "split", node, e))
                elif k == "tee" and self.m.spur_side_at(node, ed):
                    interps.append((h.cost + sc + pen, "spur", self.m.spur_side_at(node, ed), None))
                elif k == "patch" and self.m.spur_side_at(node, ed) and (d is None or abs(L - d) < ABSORB_MM):
                    interps.append((h.cost + sc, "spur", self.m.spur_side_at(node, ed), None))
        if not interps:
            self._note("nav_split_unknown")
            return self.default_side
        m = min(i[0] for i in interps)
        best = [i for i in interps if i[0] <= m + CHOICE_SLACK]
        if self.target is not None and all(i[1] == "split" and i[2] == self.target for i in best):
            self._note("nav_hold_before_split", node=self.target)
            self._request_stop(None, self.target)
            return None
        R = self._R()
        regret = {"left": 0.0, "right": 0.0}
        for _, typ, a, _e in best:
            if typ == "spur":
                v = {"left": 0.0, "right": 0.0}
                v[a] = SPUR_PENALTY
            else:
                br = self.m.branches_seen(a, self.m.dir(_e))
                v = {}
                for s in ("left", "right"):
                    f = br.get(s)
                    if f is None:
                        v[s] = UNREACHABLE
                    elif R is None:
                        v[s] = 0.0
                    else:
                        v[s] = self.m.edges[f]["len"] + R[f] if f in R else UNREACHABLE
            lo = min(v.values())
            for s in v:
                regret[s] += min(v[s] - lo, UNREACHABLE)
        if regret["left"] == regret["right"]:
            choice = self.default_side
            spur = [i[2] for i in best if i[1] == "spur"]
            if spur:
                choice = OPP[spur[0]]
        else:
            choice = min(regret, key=regret.get)
        spurs = set(i[2] for i in best if i[1] == "spur")
        if all(i[1] == "spur" for i in best) and len(spurs) == 1:
            self._dead_now = spurs.pop()
            choice = OPP[self._dead_now]
        self._choice_ctx = {"choice": choice, "interps": [(round(c, 1), t, a) for c, t, a, _ in best]}
        self._note("nav_choice", choice=choice, regret=dict((k, round(v)) for k, v in regret.items()),
                   interps=self._choice_ctx["interps"])
        return choice

    # ---------- 상태 ----------
    def where(self, odo=None):
        """지금 위치 추정: 마지막 노드, 엣지, 다음 노드, 엣지 위 거리, 후보 수."""
        best = self._best()
        if not best:
            return None
        h = best[0]
        e = self.m.edges[h.edge]
        out = {"last_node": h.last, "edge": h.edge, "next_node": e["to"],
               "dir": "rev" if e["dir"] == REV else "fwd",
               "candidates": len(set(b.edge for b in best)), "known": h.s0 is not None}
        if odo is not None and h.s0 is not None:
            out["along_mm"] = round(odo - h.s0, 1)
            out["edge_mm"] = round(e["len"], 1)
        return out

    def status(self):
        return {"target": self.target, "final": self.final, "where": self.where(),
                "spur": None if self.spur is None else self.spur["state"],
                "relocalized": self.relocalized, "observations": self.n_obs}
