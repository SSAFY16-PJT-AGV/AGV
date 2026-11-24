# -*- coding: utf-8 -*-
"""주행 결과 폴더(events.jsonl)를 사람이 읽기 좋게 요약한다. 현장 시험의 통과 여부를 여기서 본다.

    python3 -m agv.tools.runlog data/runs/<시각>
    python3 -m agv.tools.runlog data/runs/<시각> --all     # 노트(note)까지 전부

출력: 이벤트 타임라인(시간·거리·핵심 값) → 종류별 개수 → 막다른 길·V자 요약 → 이동(goto) 요약 → 종료 이유.
"""
from __future__ import print_function

import argparse
import io
import json
import os
import sys

KEYS = {
    "patch": ("color",),
    "split": ("choice", "cmd"),
    "merge": ("from_side",),
    "cross": ("from_side",),
    "tee": ("side", "known_dead"),
    "dead_end": ("side", "spur_mm"),
    "gap": ("reason",),
    "end": ("reason",),
}


def load(path):
    if os.path.isdir(path):
        path = os.path.join(path, "events.jsonl")
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def fmt(e, t0):
    t = e.get("ts")
    odo = e.get("odo_mm")
    head = u"%7.1fs %8s  %-9s" % ((t - t0) if t is not None and t0 is not None else 0.0,
                                  u"-" if odo is None else u"%.0fmm" % odo, e["type"])
    if e["type"] == "note":
        rest = dict((k, v) for k, v in e.items() if k not in ("type", "ts", "odo_mm", "seq", "msg"))
        return head + u" " + e.get("msg", u"") + (u"  " + json.dumps(rest, ensure_ascii=False) if rest else u"")
    parts = []
    for k in KEYS.get(e["type"], ()):
        if e.get(k) is not None:
            parts.append(u"%s=%s" % (k, e[k]))
    if e.get("vee"):
        parts.append(u"V자(side=%s cont=%s)" % (e["vee"].get("side"), e["vee"].get("cont")))
    return head + u" " + u" ".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", help="결과 폴더 또는 events.jsonl")
    ap.add_argument("--all", action="store_true", help="모든 note 표시")
    a = ap.parse_args(argv)
    evs = load(a.run)
    t0 = next((e["ts"] for e in evs if e.get("ts") is not None), None)
    show_notes = ("speed_cal", "vee_seen", "vee_pivot", "vee_done", "recover_reverse", "recover_pivot", "recover_done",
                  "nav_target", "nav_choice", "nav_reached", "nav_reached_early", "nav_relocalized", "nav_unmatched",
                  "nav_off_route", "nav_gave_up", "nav_hold_before_split", "nav_unreachable", "stop_planned",
                  "hold", "resume", "nav_spur_armed", "nav_spur_enter", "nav_spur_retry", "nav_spur_missed",
                  "nav_spur_exited", "spur_enter", "spur_in_pivot", "spur_in_done", "spur_end_seen", "spur_parked",
                  "spur_deadline", "spur_out_reverse", "spur_out_pivot", "spur_out_done",
                  "nav_uturn", "uturn_planned", "uturn_pivot", "uturn_done", "nav_near_branch", "nav_spur_exit")
    count = {}
    for e in evs:
        count[e["type"]] = count.get(e["type"], 0) + 1
        if e["type"] == "start":
            continue
        if e["type"] == "note" and not a.all and e.get("msg") not in show_notes:
            continue
        print(fmt(e, t0))
    print(u"\n== 개수 ==")
    print(u"  " + u", ".join(u"%s %d" % kv for kv in sorted(count.items())))
    colors = []
    for e in evs:
        if e["type"] == "patch" and e.get("color") not in colors:
            colors.append(e.get("color"))
    print(u"  본 색: " + (u", ".join(colors) or u"없음"))
    des = [e for e in evs if e["type"] == "dead_end"]
    notes = [e.get("msg") for e in evs if e["type"] == "note"]
    if des:
        print(u"\n== 막다른 길 ==")
        for e in des:
            print(u"  side=%s spur_mm=%s" % (e.get("side"), e.get("spur_mm")))
        print(u"  되돌아 나오기 완료 %d / 시작 %d" % (notes.count("recover_done"), notes.count("recover_reverse")))
    vs = [e for e in evs if e["type"] == "note" and e.get("msg") == "vee_seen"]
    if vs:
        print(u"\n== V자(급한 가지) ==")
        for e in vs:
            print(u"  side=%s cont=%s frames=%s gaps=%s" % (e.get("side"), e.get("cont"), e.get("frames"), e.get("gaps")))
        print(u"  들어가기 완료 %d / 회전 시작 %d" % (notes.count("vee_done"), notes.count("vee_pivot")))
    sc = [e for e in evs if e["type"] == "note" and e.get("msg") == "speed_cal"]
    st = next((e for e in evs if e["type"] == "start"), {})
    if sc or st.get("speedcal"):
        print(u"\n== 속도 보정(패치) ==")
        print(u"  처음 짐작값 %s mm/s" % st.get("speed_mm_s"))
        for e in sc:
            print(u"  %-7s 잰 값 %6s  흩어짐 %5s  회전 %5s  -> %s%s" % (
                e.get("color"), e.get("k_obs"), e.get("spread"), e.get("turn"), e.get("k"),
                (u"  [버림: %s]" % e["reject"]) if e.get("reject") else u""))
        ok = [e for e in sc if not e.get("reject")]
        print(u"  받아들임 %d / %d%s" % (len(ok), len(sc), (u", 마지막 %s mm/s" % ok[-1]["k"]) if ok else u""))
    nav = [e for e in evs if e["type"] == "note" and str(e.get("msg", "")).startswith("nav_")]
    if nav:
        print(u"\n== 이동(goto) ==")
        tg = [e for e in nav if e["msg"] == "nav_target"]
        if tg:
            print(u"  목표 %s (끝 %s)%s" % (tg[0].get("target"), tg[0].get("final"),
                                        u", 경로 " + u"-".join(tg[0]["route"]) if tg[0].get("route") else u""))
        for e in nav:
            if e["msg"] == "nav_choice":
                rg = dict((k, (u"못 감" if v >= 1e5 else u"%dmm" % v)) for k, v in (e.get("regret") or {}).items())
                print(u"  갈림길 %-5s 손해 왼쪽 %s / 오른쪽 %s  해석 %s" % (
                    e.get("choice"), rg.get("left"), rg.get("right"), e.get("interps")))
            elif e["msg"] in ("nav_reached", "nav_reached_early"):
                print(u"  도착 판정 %s%s" % (e.get("node"), u" (패치 기록 보류 중 먼저)" if e["msg"].endswith("early") else u""))
        print(u"  다시 위치 잡음 %d, 맵과 안 맞은 이벤트 %d, 경로 벗어남 %d" % (
            notes.count("nav_relocalized"), notes.count("nav_unmatched"), notes.count("nav_off_route")))
        print(u"  대기 %d / 재개 %d" % (notes.count("hold"), notes.count("resume")))
        for e in evs:
            m = e.get("msg")
            if m == "spur_enter":
                print(u"  스퍼 진입 %s쪽 (%s로 봄)%s" % (e.get("side"), {"tee": u"옆 스퍼", "split": u"갈림길"}.get(
                    e.get("via"), e.get("via")), u", 갈림 지점보다 %smm 늦음" % e["late_mm"] if e.get("late_mm") else u""))
            elif m == "spur_end_seen":
                print(u"  스퍼 끝 봄: 잰 스퍼 길이 %smm (맵 %s)" % (
                    e.get("spur_mm"), u"%.0fmm" % e["map_spur_mm"] if e.get("map_spur_mm") else u"-"))
            elif m == "nav_uturn":
                print(u"  유턴: %s 가운데에서 %s쪽으로 돌기" % (e.get("node"), e.get("side")))
            elif m == "nav_near_branch":
                print(u"  회전 직후 바로 앞 %s(갈림길)에서 %s 가지를 고른 채로 출발" % (e.get("node"), e.get("want")))
            elif m == "nav_spur_exit":
                print(u"  스퍼에서 나가기: %s쪽으로 돌아 %s" % (e.get("side"), u"되돌아감" if e.get("back") else u"이어 감"))
            elif m == "nav_spur_retry":
                print(u"  스퍼를 못 봐서 한 바퀴 돌아 다시 (%s번째)" % e.get("tries"))
        print(u"  유턴: 지시 %d, 완료 %d / 시작 %d" % (
            notes.count("nav_uturn"), notes.count("uturn_done"), notes.count("uturn_pivot")))
        print(u"  스퍼: 주차 %d, 빠져나옴 %d / 후진 시작 %d, 못 봄 %d" % (
            notes.count("spur_parked"), notes.count("spur_out_done"), notes.count("spur_out_reverse"),
            notes.count("nav_spur_missed")))
    end = [e for e in evs if e["type"] == "end"]
    print(u"\n종료 이유: %s" % (end[-1].get("reason") if end else u"(end 없음 - 비정상 종료)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
