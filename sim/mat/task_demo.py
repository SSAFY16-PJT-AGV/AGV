# -*- coding: utf-8 -*-
"""선반 작업 시뮬레이션: 서버 주문 -> 선반 앞으로 이동 -> 칸 보기 -> 집기/놓기 -> 선반 상태·결과 보고.

주행은 가상 매트(run_sim, 실물과 같은 runner·Navigator·LinkController), 선반 장면은 광선 추적
(agv/perception/scene.py), 팔은 가상 팔(warehouse.FakeArm)이다. 실물과 다른 것은 카메라 영상과 팔뿐이다.

    python3 sim/mat/task_demo.py                       # 기본 시나리오(아래 DEFAULT_JOBS), 브로커 없이
    python3 sim/mat/task_demo.py --host 127.0.0.1      # 실제 MQTT 브로커로(주문을 이 스크립트가 보냄)
    python3 sim/mat/task_demo.py --out data/task_demo --video

기본 시나리오(새 매트 ssafy_v3, 로봇은 N01 앞에서 출발):
  1. N01(정렬 안 된 물체) 스퍼 끝 D01로 가서 칸 2의 휴지를 집는다
  2. 안쪽 왼쪽 세로선으로 N03(휴지 선반) D03에 가서 칸 2에 놓는다
  3. N02(우유 선반) D02에 가서 보기만 한다
  4. 틀린 작업 하나: 1번에서 비운 N01 칸 2에서 휴지를 또 집으라고 함 -> slot_empty로 실패
"""
from __future__ import print_function

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sim", "mat"))

import run_sim  # noqa: E402
from mat import Mat  # noqa: E402
from warehouse import FakeArm, VirtualWarehouse  # noqa: E402
from agv.link import FakeLink, LinkController  # noqa: E402
from agv.nav import NavMap, Navigator  # noqa: E402
from agv.perception.shelf import ShelfLayout, camera_from_config, load_config  # noqa: E402
from agv.task import TaskExecutor  # noqa: E402

MAT = os.path.join(ROOT, "sim", "mat", "mats", "ssafy_v3.json")
MAP = os.path.join(ROOT, "maps", "mat_ssafy_v3.json")
CFG = {"patch": {"length_mm": 160.0, "length_mm_by_color": {"pink": 149.0, "skyblue": 149.0}}}
START = [300, 868, 0]
DRIVE_ANGLES = {"1": 0.0, "5": 30.0}

DEFAULT_INVENTORY = {"N01": {1: "milk", 2: "tissue", 3: "egg", 4: "juice"},
                     "N02": {3: "milk"}, "N03": {1: "tissue"}}
DEFAULT_JOBS = [
    (["N01", "D01"], {"type": "load", "shelf": "N01", "slot": 2, "item": "tissue"}),
    (["D01", "N01", "J02", "J07", "N03", "D03"], {"type": "unload", "shelf": "N03", "slot": 2, "item": "tissue"}),
    (["D03", "N03", "N02", "D02"], {"type": "observe", "shelf": "N02"}),
    (["D02", "N02", "N07", "N01", "D01"], {"type": "load", "shelf": "N01", "slot": 2, "item": "tissue"}),
]


def make_order(oid, nodes, action, map_ref, seq=1, release=None):
    return {"order_id": oid, "seq": seq, "map_id": map_ref, "nodes": nodes,
            "released_until": release or nodes[-1], "action_at_end": action}


def make_task_sim(st, inventory=None, seed=2, log=None, geom_err=None):
    """가상 창고·가상 팔·실행기. st["pose"](실제 자세), st["t"](시뮬레이션 시각)를 on_step에서 채운다.
    반환: (world, arm, executor)."""
    pcfg = load_config()
    layout = ShelfLayout(pcfg)
    cam = camera_from_config(pcfg)
    world = VirtualWarehouse(Mat(MAT).d, pcfg, layout, cam,
                             inventory=DEFAULT_INVENTORY if inventory is None else inventory, seed=seed)
    arm = FakeArm(world, lambda: st["pose"], lambda: st["t"], servo_angles=DRIVE_ANGLES)
    ecfg = pcfg
    if geom_err:
        import copy
        ecfg = copy.deepcopy(pcfg)
        for k, d in geom_err.items():
            ecfg["robot_geom"][k] = float(ecfg["robot_geom"].get(k, 0.0)) + d
    ex = TaskExecutor(ecfg, cam, layout, arm, pcfg["observe_pose"]["servo_angles"], DRIVE_ANGLES,
                      grab=lambda: world.render(st["pose"], arm.servo_angles()), log=log)
    return world, arm, ex


def run(jobs=None, inventory=None, seed=2, out=None, video=False, link=None, max_time_s=400.0, notes=None,
        quiet=True, geom_err=None):
    """jobs: [(경로, action_at_end)]. geom_err: 판정 쪽이 잘못 아는 robot_geom 값 차이(예: {"camera_z_mm": 8}),
    카메라 보정 오차 흉내. 반환: dict(link, world, arm, gt, notes, ctl)."""
    jobs = DEFAULT_JOBS if jobs is None else jobs
    with io.open(MAP, encoding="utf-8") as f:
        raw = json.load(f)
    ref = "%s@v%s" % (raw["map_id"], raw["version"])
    st = {"t": 0.0, "runner": None, "sent": 0, "pose": None, "done": False}
    notes = [] if notes is None else notes

    def log(m, **k):
        notes.append((round(st["t"], 2), m, k))
        if not quiet:
            print(u"[%6.1f] %s %s" % (st["t"], m, json.dumps(k, ensure_ascii=False)))
    world, arm, ex = make_task_sim(st, inventory, seed, log, geom_err)
    nav = Navigator(NavMap(raw), start=jobs[0][0][0], stop_offset_mm=80, hold_at_end=True)
    link = link or FakeLink("sim")
    ctl = LinkController(link, nav, raw, start_node=jobs[0][0][0], log=log, tasks=ex, clock=lambda: st["t"])

    def on_step(runner, t):
        st["t"], st["pose"] = t, runner.sim_truth
        if st["runner"] is None:
            st["runner"] = runner
            ctl.attach(runner)
        s = ctl.step(now=t)
        w = st.get("await")
        if w is not None:
            cur = ctl.book.current
            if cur is not None and cur["order_id"] == w:
                st["await"] = None
            elif ctl.rejected and ctl.rejected.get("order_id") == w:
                runner.stop("order_rejected")
            elif getattr(link, "real", False):
                time.sleep(0.002)           # 실제 브로커: 주문이 돌아올 때까지 시뮬레이션 시간을 천천히
        elif s["status"] == "IDLE":
            if st["sent"] < len(jobs):
                nodes, act = jobs[st["sent"]]
                st["sent"] += 1
                st["await"] = "J%d" % st["sent"]
                link.send(make_order(st["await"], nodes, act, ref))
            elif not st["done"]:
                st["done"] = True
                runner.stop("jobs_done")
        if s["status"] == "ERROR":
            runner.stop("error")
    tmp = out or tempfile.mkdtemp()
    try:
        ev, gt = run_sim.run(MAT, tmp, seed=seed, max_time_s=max_time_s, policy=nav, on_step=on_step,
                             start_pose=START, cfg_override=CFG, video=video)
    finally:
        if out is None:
            shutil.rmtree(tmp, ignore_errors=True)
    return {"link": link, "world": world, "arm": arm, "gt": gt, "notes": notes, "ctl": ctl, "jobs_sent": st["sent"]}


def summary(r):
    link, world = r["link"], r["world"]
    lines = [u"종료: %s, 보낸 작업 %d" % (r["gt"]["finish_reason"], r["jobs_sent"])]
    for m in getattr(link, "tasks", []):
        lines.append(u"  결과 %s %s %s 칸%s(%s): %s%s, 들고 있음 %s" % (
            m["order_id"], m["action"], m["shelf"], m["slot"], m["item"], m["result"],
            u" (%s)" % m["reason"] if m["reason"] else u"", m["carrying"]))
    for m in getattr(link, "shelves", []):
        lines.append(u"  선반 %s %s %s: %s" % (m["order_id"], m["shelf"], m["phase"],
                                             " ".join("%d:%s" % (s["slot"], s["item"]) for s in m["slots"])))
    lines.append(u"  실제 창고: %s" % json.dumps(world.snapshot(), ensure_ascii=False))
    return u"\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=2)
    ap.add_argument("--out", default=None)
    ap.add_argument("--video", action="store_true")
    ap.add_argument("--host", default=None, help="실제 MQTT 브로커(없으면 FakeLink)")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    link = None
    if a.host:
        from agv.link import MqttLink
        link = RecordingMqtt("sim-r1", a.host, a.port).start()
        link.wait_connected(5.0)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    r = run(seed=a.seed, out=a.out, video=a.video, link=link, quiet=not a.verbose)
    print(summary(r))
    if link is not None:
        link.stop()
    return 0 if r["gt"]["finish_reason"] == "jobs_done" else 1


def RecordingMqtt(robot_id, host, port):
    """실제 브로커용: MqttLink에 send()(주문 넣기)와 보낸 메시지 기록을 붙인다."""
    from agv.link import MqttLink

    class _L(MqttLink):
        real = True

        def __init__(self):
            MqttLink.__init__(self, robot_id, host, port)
            self.tasks, self.shelves, self.events, self.states = [], [], [], []

        def send(self, order):
            self.c.publish(self.topic("order"), json.dumps(order), qos=1)

        def publish_task(self, msg):
            self.tasks.append(msg)
            return MqttLink.publish_task(self, msg)

        def publish_shelf(self, msg):
            self.shelves.append(msg)
            return MqttLink.publish_shelf(self, msg)
    return _L()


if __name__ == "__main__":
    sys.exit(main())
