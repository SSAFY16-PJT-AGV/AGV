# -*- coding: utf-8 -*-
"""노드 이벤트 로그(JSONL) 형식과 입출력.

로봇은 주행하면서 아래 이벤트를 한 줄에 하나씩 JSON으로 남긴다.
맵 빌더(build_map)는 이 로그만 보고 위상 맵을 만든다.

공통 필드
    ts      float  UNIX 시각(초)
    seq     int    로그 안 순번
    type    str    start | patch | split | merge | cross | tee | dead_end | gap | end | note
    odo_mm  float  주행 시작부터 누적 이동거리 추정(mm). 모터 명령 x 실측 속도로 적분.
                   수동으로 밀거나 조종해서 모터 명령을 모르면 null.

type별 필드
    start   robot, mode(drive|passive|ask), speed_mm_s, map_hint
    patch   color                     색 패치에 진입한 순간
    split   choice(left|right), cmd   갈라지는 곳을 지나 다른 가지가 화면에서 빠진 뒤 기록.
            onset                     odo_mm은 선이 처음 갈라진 순간 값이라 앞 이벤트보다
                                      작을 수 있다(빌더가 거리순 정렬). choice는 실제로
                                      따라간 쪽, cmd는 명령한 쪽(passive면 null),
                                      onset은 갈라질 때 간격/라인폭 비율과 간격 변화(px)
    merge   from_side(left|right)     합쳐지는 곳을 지난 순간. 내가 들어온 쪽
    cross   from_side(left|right)     X자 교차를 직진으로 지난 순간. 다른 선이 들어온 쪽
    tee     side(left|right)          옆으로 뻗은 막다른 선(스퍼)의 T자를 지난 순간.
            known_dead                이미 막다른 길로 아는 갈림길을 지났으면 true
    dead_end side, ref_seq, ref_kind  고른 쪽 선이 끝남(막다른 길). ref_seq는 그때 고른 갈림길(split)이나
             spur_mm                  교차(cross) 이벤트의 seq, spur_mm은 갈림길에서 선 끝까지 거리.
                                      이 뒤로 후진·회전해 갈림길로 돌아오며, 이후 odo_mm은 갈림길부터 이어진다
    gap     reason                    라인을 놓치는 등 경로 연속성이 끊긴 지점.
                                      빌더는 여기서 노드 순서를 끊는다.
    end     reason, explorer          종료 이유(explore_explored, max_time, user_stop ...)
    note    msg                       디버깅 메모. 빌더는 무시한다.

odo_mm·ts는 판정한 바닥 지점 기준이라 파일 순서와 다를 수 있다(빌더가 위치순으로 정렬).

Python 3.6 호환(Jetson Nano). 표준 라이브러리만 쓴다.
"""
import io
import json
import time

NODE_TYPES = ("patch", "split", "merge", "cross", "tee")
BREAK_TYPES = ("start", "gap", "end")


class EventLogger(object):
    """이벤트를 JSONL로 즉시 기록한다. 로봇이 중간에 꺼져도 앞부분은 남는다."""

    def __init__(self, path, echo=True, clock=time.time):
        self.path = path
        self.echo = echo
        self.clock = clock
        self.seq = 0
        self._f = io.open(path, "a", encoding="utf-8")

    def log(self, type_, odo_mm=None, ts=None, **fields):
        """ts를 주면 그 시각으로 기록한다(판정이 늦게 확정된 이벤트의 실제 시각)."""
        ev = {"ts": round(self.clock() if ts is None else ts, 3), "seq": self.seq, "type": type_,
              "odo_mm": None if odo_mm is None else round(float(odo_mm), 1)}
        ev.update(fields)
        self.seq += 1
        self._f.write(json.dumps(ev, ensure_ascii=False) + u"\n")
        self._f.flush()
        if self.echo:
            print("[event] " + json.dumps(ev, ensure_ascii=False))
        return ev

    def close(self):
        self._f.close()


def read_events(path):
    """JSONL 이벤트 로그를 읽는다. 깨진 줄(전원 차단 등)은 건너뛴다."""
    events = []
    with io.open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def split_runs(events):
    """start/gap/end에서 끊어 연속 구간 목록으로 나눈다.

    반환: [(구간 이벤트 목록, 구간 시작 이벤트의 속성 dict), ...]
    각 구간 안에서는 노드 이벤트의 순서가 실제 통과 순서라고 본다.
    """
    runs = []
    cur = []
    meta = {}
    for ev in events:
        t = ev.get("type")
        if t in BREAK_TYPES:
            if cur:
                runs.append((cur, meta))
            cur = []
            if t == "start":
                meta = ev
        elif t in NODE_TYPES:
            cur.append(ev)
    if cur:
        runs.append((cur, meta))
    return runs
