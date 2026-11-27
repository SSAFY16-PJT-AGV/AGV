# -*- coding: utf-8 -*-
"""가상 창고: 가상 매트(주행 시뮬레이터)에 선반 6개와 그 위 물건을 놓고, 선반 작업을 시험한다.

- 선반 자리: 매트 도안의 선반 패치(N01~N06) 스퍼 끝 너머. 로봇이 스퍼 끝에 제대로 주차했을 때
  (선 끝에서 park_back_mm 앞) 선반 앞 모서리가 차체 회전 중심 앞 shelf.front_x_mm에 오게 둔다.
  실제 주차 오차만큼 선반이 로봇에게 비뚤게 보인다.
- render(): 로봇의 실제 자세와 카메라 서보 각도로 선반 장면을 그린다(agv/perception/scene.py, 광선 추적).
- FakeArm: agv.task.ArmDriver 모양. 집기는 명령한 자리 가까이(grip_tol_mm)에 실제 물건이 있어야 성공,
  놓기는 명령한 자리에 물건을 놓는다(인식 오차가 그대로 놓는 자리 오차가 됨).

매트 좌표: mm, x 오른쪽, y 위, 각도는 반시계(rad). 몸체 좌표: x 앞, y 왼쪽(agv/perception과 같음).
"""
from __future__ import division

import math
import os
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agv.perception import scene  # noqa: E402
from agv.perception.camera import camera_to_body  # noqa: E402
from agv.task.arm_driver import ArmDriver  # noqa: E402


class VirtualWarehouse(object):
    def __init__(self, mat_d, pcfg, layout, cam, inventory=None, park_back_mm=20.0, seed=0, light=None):
        self.pcfg, self.layout, self.cam = pcfg, layout, cam
        self.rng = np.random.RandomState(seed)
        self.light = light or {"gain": 0.9, "noise": 3.0, "blur": 0.7}
        self.rays = cam.pixel_rays()
        self.marker_of = {}
        for name, o in pcfg["objects"].items():
            if not name.startswith("_"):
                self.marker_of[name] = int(o["ids"][0])
        self.shelves = {}                   # 선반 -> (앞 모서리 가운데 x, y, 선반 안쪽 방향 rad)
        for p in mat_d["paths"]:
            node = p.get("node")
            if not p.get("spur") or node not in layout.shelves:
                continue
            (px, py), (ex, ey) = p["points"][0], p["points"][-1]
            L = math.hypot(ex - px, ey - py)
            ux, uy = (ex - px) / L, (ey - py) / L
            d = layout.front_x - park_back_mm       # 선 끝에서 선반 앞 모서리까지
            self.shelves[node] = (ex + ux * d, ey + uy * d, math.atan2(uy, ux))
        self.inv = dict((s, {}) for s in self.shelves)
        for s, slots in (inventory or {}).items():
            for k, item in slots.items():
                self.put(s, int(k), item)

    # ---------- 창고 상태 ----------
    def put(self, shelf, slot, item, setback=None, dy=None, yaw=None):
        r = self.rng
        self.inv[shelf][slot] = {
            "item": item, "marker_id": self.marker_of[item],
            "setback": r.uniform(0, 8) if setback is None else setback,
            "dy": r.uniform(-4, 4) if dy is None else dy,
            "yaw": r.uniform(-5, 5) if yaw is None else yaw}

    def snapshot(self):
        """{선반: {칸: 물건}} 정답."""
        return dict((s, dict((k, v["item"]) for k, v in sorted(sl.items()))) for s, sl in sorted(self.inv.items()))

    # ---------- 기하 ----------
    def shelf_in_body(self, pose, shelf):
        """로봇 실제 자세(매트 x, y, th rad) -> 선반 자세(몸체 x, y, yaw 도)."""
        x, y, th = pose
        sx, sy, sa = self.shelves[shelf]
        dx, dy = sx - x, sy - y
        c, s = math.cos(-th), math.sin(-th)
        return (c * dx - s * dy, s * dx + c * dy, math.degrees((sa - th + math.pi) % (2 * math.pi) - math.pi))

    def facing(self, pose, max_mm=450.0, max_deg=35.0):
        """로봇이 마주 보는 선반(없으면 None)."""
        best = None
        for s in self.shelves:
            bx, by, yaw = self.shelf_in_body(pose, s)
            if 0 < bx < max_mm and abs(by) < 150 and abs(yaw) < max_deg:
                if best is None or bx < best[1]:
                    best = (s, bx)
        return best[0] if best else None

    def _scene(self, pose, shelf):
        contents = dict((k, {"marker_id": v["marker_id"], "setback": v["setback"], "dy": v["dy"], "yaw": v["yaw"]})
                        for k, v in self.inv[shelf].items())
        return scene.build_shelf_scene(self.pcfg, self.layout, self.shelf_in_body(pose, shelf), contents,
                                       shelf_id=shelf)

    def render(self, pose, servo_angles):
        """로봇 카메라 영상(회색조 uint8). 마주 보는 선반이 없으면 벽만."""
        R_bc, t_bc = camera_to_body(self.pcfg["robot_geom"], servo_angles)
        shelf = self.facing(pose)
        boxes = self._scene(pose, shelf)[0] if shelf else []
        raw = scene.render(self.cam, R_bc, t_bc, boxes, wall_x=self.layout.front_x + self.layout.depth + 250.0,
                           rays=self.rays)
        return scene.finish(raw, self.rng, **self.light)

    def pick(self, pose, cmd, tol_mm=15.0):
        """반환: (ok, 이유, 물건)."""
        shelf = self.facing(pose)
        if shelf is None:
            return False, "grip_missed", None
        _, truth = self._scene(pose, shelf)
        c = cmd["center_mm"]
        best = None
        for k, t in truth["slots"].items():
            if t["state"] == "empty":
                continue
            d = math.hypot(t["center_mm"][0] - c[0], t["center_mm"][1] - c[1])
            if best is None or d < best[0]:
                best = (d, k)
        if best is None or best[0] > tol_mm:
            return False, "grip_missed", None
        item = self.inv[shelf].pop(best[1])["item"]
        return True, None, item

    def place(self, pose, cmd, item):
        """명령한 자리에 물건을 놓는다. 칸 밖이면 떨어뜨린 것으로(창고에서 사라짐). 반환: (ok, 이유)."""
        shelf = self.facing(pose)
        if shelf is None:
            return True, None           # 팔은 놓았다고 안다(떨어뜨림). 다시 보기에서 걸린다
        bx, by, yaw = self.shelf_in_body(pose, shelf)
        a = math.radians(yaw)
        dx, dy = cmd["center_mm"][0] - bx, cmd["center_mm"][1] - by
        lx = math.cos(a) * dx + math.sin(a) * dy
        ly = -math.sin(a) * dx + math.cos(a) * dy
        L = self.layout
        k = int(round((L.n - 1) / 2.0 - ly / L.slot_w)) + 1
        size = 40.0
        setback = lx - size / 2.0
        if not (1 <= k <= L.n) or k in self.inv[shelf] or setback < -size / 2 or setback > L.depth - size:
            return True, None
        self.put(shelf, k, item, setback=max(0.0, setback), dy=ly - L.slot_y(k), yaw=0.0)
        return True, None


class FakeArm(ArmDriver):
    """가상 팔. 명령마다 정해진 시간이 지나면 끝난다. pose_fn() -> 로봇 실제 자세, clock() -> 시뮬레이션 시각."""

    DURATION = {"observe": 1.0, "drive_pose": 1.0, "pick": 4.0, "place": 4.0}

    def __init__(self, world, pose_fn, clock, servo_angles=None):
        self.world, self.pose_fn, self.clock = world, pose_fn, clock
        self._angles = dict(servo_angles or {"1": 0.0, "5": 30.0})
        self.holding = None
        self._cmd = None
        self._t_end = None
        self.log = []

    def start(self, cmd):
        self._cmd = cmd
        self._t_end = self.clock() + self.DURATION.get(cmd["op"], 1.0)

    def poll(self):
        if self._cmd is None or self.clock() < self._t_end:
            return None
        cmd, self._cmd = self._cmd, None
        op = cmd["op"]
        if op in ("observe", "drive_pose"):
            self._angles = dict(cmd["servo_angles"])
            res = {"ok": True, "reason": None}
        elif op == "pick":
            ok, reason, item = self.world.pick(self.pose_fn(), cmd)
            if ok:
                self.holding = item
            res = {"ok": ok, "reason": reason}
        else:
            if self.holding is None:
                res = {"ok": False, "reason": "arm_failed"}
            else:
                ok, reason = self.world.place(self.pose_fn(), cmd, self.holding)
                self.holding = None
                res = {"ok": ok, "reason": reason}
        self.log.append((op, res["ok"], res["reason"]))
        return res

    def servo_angles(self):
        return dict(self._angles)
