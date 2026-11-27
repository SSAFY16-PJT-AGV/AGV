# -*- coding: utf-8 -*-
"""선반 작업 실행기. 로봇이 선반 앞(스퍼 끝)에 서 있는 동안 주행 루프가 프레임마다 step()을 부른다.

단계(phase):
  observe_pose  1·5번을 관측 자세로(팔 명령 observe)
  look_before   칸 보기 n_looks장(프레임마다 한 장). 필요한 칸이 안 보이면(비뚤게 주차) 1번을 그 칸 쪽으로
                돌려 한 번 더 본다(pan_look). 합친 관측으로 할 일을 정함
  act           팔 집기·놓기(pick/place)
  look_after    다시 보기(동작 확인, 바뀐 선반 상태)
  drive_pose    주행 자세로(라인 보는 1·5번)
끝나면 step()이 TaskOutcome을 한 번 돌려준다. 실패해도 주행 자세로는 돌아간다.

막히지 않는다(팔은 start/poll). 프레임은 step(frame=)으로 받거나, grab()을 주면 그걸 부른다(시뮬레이션).
"""
from .decide import combine_views, decide, merge_views, pan_toward, verify, view_from_perception
from .model import TaskOutcome

PHASES = ("observe_pose", "look_before", "act", "look_after", "drive_pose")


def scaled_camera(cam, size):
    """카메라 모델을 프레임 크기에 맞춘다(설정은 1280x720, 실제 프레임이 다를 수 있음)."""
    from ..perception.camera import CameraModel
    if tuple(size) == tuple(cam.size):
        return cam
    sx, sy = float(size[0]) / cam.size[0], float(size[1]) / cam.size[1]
    K = cam.K.copy()
    K[0, :] *= sx
    K[1, :] *= sy
    return CameraModel(cam.model, K, cam.D, size)


class TaskExecutor(object):
    def __init__(self, pcfg, cam, layout, arm, observe_angles, drive_angles=None, grab=None, n_looks=3,
                 max_look_frames=12, arm_timeout_s=30.0, settle_s=0.3, log=None, perceive=None, max_pans=2):
        self.pcfg, self.cam, self.layout, self.arm = pcfg, cam, layout, arm
        self.observe_angles = dict(observe_angles)
        self.drive_angles = dict(drive_angles) if drive_angles else None
        self.grab = grab
        self.n_looks, self.max_look_frames = int(n_looks), int(max_look_frames)
        self.arm_timeout_s, self.settle_s = float(arm_timeout_s), float(settle_s)
        self._log = log
        if perceive is None:
            from ..perception.shelf import perceive_shelf as perceive
        self._perceive = perceive
        self.carrying = None            # 들고 있는 물건 종류(작업 사이에 이어짐)
        self.task = None
        self.phase = None
        self._t0 = None
        self._views = []
        self._frames = 0
        self._before = self._after = None
        self._plan = None
        self._fail = None
        self._arm_sent = False
        self._placed = None
        self.max_pans = int(max_pans)
        self._pans = 0
        self._angles_now = dict(self.observe_angles)
        g = pcfg.get("robot_geom", {})
        self._s1_x = float(g.get("s1_axis_x_mm", 0.0))
        self._pan_range = (pcfg.get("arm") or {}).get("pan_range_deg", [-80.0, 80.0])

    @property
    def busy(self):
        return self.task is not None

    def _note(self, msg, **kw):
        if self._log is not None:
            self._log(msg, **kw)

    def start(self, task, now):
        if self.busy:
            raise RuntimeError("task already running")
        self.task = task
        self._before = self._after = self._plan = self._fail = self._placed = None
        self._pans = 0
        self._angles_now = dict(self.observe_angles)
        self._merged_before = self._merged_after = False
        self._note("task_start", action=task.action, shelf=task.shelf, slot=task.slot, item=task.item,
                   carrying=self.carrying)
        self._enter("observe_pose", now)

    def _enter(self, phase, now):
        self.phase = phase
        self._t0 = now
        self._views, self._frames = [], 0
        self._arm_sent = False
        self._note("task_phase", phase=phase)

    def _arm(self, cmd, now):
        """팔 명령을 한 번 보내고 결과를 기다린다. 반환: None(진행 중) 또는 결과 dict."""
        if not self._arm_sent:
            self.arm.start(cmd)
            self._arm_sent = True
            return None
        r = self.arm.poll()
        if r is None and now - self._t0 > self.arm_timeout_s:
            return {"ok": False, "reason": "timeout"}
        return r

    def _look(self, now, frame):
        """한 장 보고, 다 모았으면 합친 ShelfView를 돌려준다."""
        if now - self._t0 < self.settle_s:
            return None
        img = self.grab() if self.grab is not None else frame
        if img is None:
            return None
        self._frames += 1
        cam = scaled_camera(self.cam, (img.shape[1], img.shape[0]))
        res = self._perceive(img, self.pcfg, cam, self.arm.servo_angles(), self.layout,
                             expect_shelf=self.task.shelf)
        v = view_from_perception(res, self.task.shelf, self.layout)
        self._views.append(v)
        enough = sum(1 for x in self._views if x.seen) >= self.n_looks
        if enough or self._frames >= self.max_look_frames:
            return merge_views(self._views)
        return None

    def step(self, now, frame=None):
        """반환: 끝났으면 TaskOutcome, 아니면 None."""
        if not self.busy:
            return None
        t = self.task
        if self.phase == "observe_pose":
            r = self._arm({"op": "observe", "servo_angles": self.observe_angles}, now)
            if r is not None:
                if not r["ok"]:
                    self._fail = r.get("reason") or "arm_failed"
                    self._enter("drive_pose", now)
                else:
                    self._enter("look_before", now)
        elif self.phase == "pan":
            r = self._arm({"op": "observe", "servo_angles": self._angles_now}, now)
            if r is not None:
                self._enter("look_before" if self._after_phase == "before" else "look_after", now)
        elif self.phase in ("look_before", "look_after") and self._need_pan(now, frame):
            pass
        elif self.phase == "look_before":
            v = self._before
            if v is not None:
                what, arg = decide(t, v, self.carrying, self.layout.n)
                self._note("task_decide", decision=what, detail=arg if what == "fail" else None,
                           slots=dict((k, s.state) for k, s in v.slots.items()))
                if what == "fail":
                    self._fail = arg
                    self._enter("drive_pose", now)
                elif what == "observe":
                    self._enter("drive_pose", now)
                else:
                    self._plan = (what, arg)
                    self._enter("act", now)
        elif self.phase == "act":
            what, s = self._plan
            cmd = {"op": what, "slot": t.slot, "center_mm": list(s.center_mm), "yaw_deg": s.yaw_deg,
                   "shelf_yaw_deg": self._before.pose["yaw_deg"] if self._before.pose else None}
            r = self._arm(cmd, now)
            if r is not None:
                if r["ok"]:
                    if what == "pick":
                        self.carrying = s.state
                    else:
                        self._placed = self.carrying
                        self.carrying = None
                else:
                    self._fail = r.get("reason") or "arm_failed"
                self._note("task_arm", op=what, ok=r["ok"], reason=r.get("reason"), carrying=self.carrying)
                self._enter("look_after", now)
        elif self.phase == "look_after":
            v = self._after
            if v is not None:
                if self._fail is None:
                    item = self.carrying if t.action == "load" else self._placed
                    bad = verify(t, v, item)
                    if bad:
                        self._fail = bad
                self._enter("drive_pose", now)
        elif self.phase == "drive_pose":
            if self.drive_angles is None:
                return self._finish()
            r = self._arm({"op": "drive_pose", "servo_angles": self.drive_angles}, now)
            if r is not None:
                if not r["ok"] and self._fail is None:
                    self._fail = r.get("reason") or "arm_failed"
                return self._finish()
        return None

    def _wanted(self, view):
        """이 작업에 필요한데 판단 못 한 칸들."""
        if self.task.action == "observe":
            return [k for k, s in view.slots.items() if s.state == "unknown"]
        k = self.task.slot
        return [k] if k in view.slots and view.slots[k].state == "unknown" else []

    def _need_pan(self, now, frame):
        """보기 단계: 한 묶음을 다 보면 앞서 본 것과 합치고, 필요한 칸이 안 보이면 1번을 돌려 다시 보러 간다.
        반환: True면 이번 스텝은 여기서 끝(아직 보는 중이거나 돌리러 감)."""
        which = "before" if self.phase == "look_before" else "after"
        if getattr(self, "_merged_" + which, False):
            return False
        v = self._look(now, frame)
        if v is None:
            return True
        prev = self._before if which == "before" else self._after
        v = combine_views(prev, v) if prev is not None else v
        if which == "before":
            self._before = v
        else:
            self._after = v
        want = self._wanted(v)
        if want and v.seen and self._pans < self.max_pans:
            pan = pan_toward(v, want, self._s1_x, self._pan_range)
            if pan is not None and abs(pan - float(self._angles_now.get("1", 0.0))) > 3.0:
                self._pans += 1
                self._angles_now = dict(self._angles_now, **{"1": pan})
                self._note("task_pan", look=which, pan_deg=pan, slots=want)
                self._after_phase = which
                self._enter("pan", now)
                return True
        setattr(self, "_merged_" + which, True)
        return False

    def _finish(self):
        out = TaskOutcome(self.task, self._fail is None, self._fail, self.carrying, self._before, self._after)
        self._note("task_done", ok=out.ok, reason=out.reason, carrying=out.carrying)
        self.task = None
        self.phase = None
        return out

    def slot_ids(self, shelf):
        """선반 칸 1..n의 전체 칸 번호(= 칸 마커 ID, 서버 선반 모델의 slot_id)."""
        return list(self.layout.shelves.get(shelf, []))

    def slot_of(self, slot_id):
        """전체 칸 번호 -> (선반, 칸) 또는 None."""
        return self.layout.slot_marker.get(slot_id)

    def info(self):
        """상태 메시지용: 진행 중인 작업과 단계."""
        if not self.busy:
            return None
        return {"action": self.task.action, "shelf": self.task.shelf, "slot": self.task.slot, "phase": self.phase}
