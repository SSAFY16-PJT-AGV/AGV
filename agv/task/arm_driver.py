# -*- coding: utf-8 -*-
"""팔 인터페이스. 실행기(TaskExecutor)는 이 모양만 안다.

    start(cmd)       명령 시작. 바로 돌아온다.
    poll()           None(아직) 또는 {"ok": bool, "reason": 실패 이유 또는 None}
    servo_angles()   지금 카메라 자세에 쓰는 서보 각도 {"1": 도, "5": 도}(칸 판정의 카메라 -> 몸체 변환용)

명령(cmd["op"]):
  observe     카메라가 선반을 보게(1·5번을 관측 자세로). cmd["servo_angles"]
  drive_pose  주행 자세로 돌아감(1·5번을 라인 보는 저장 자세로). cmd["servo_angles"]
  pick        cmd["center_mm"](몸체 좌표 물건 가운데, z는 잡을 높이), cmd["yaw_deg"], cmd["slot"]
  place       cmd["center_mm"](놓을 자리 가운데, z는 선반 윗면), cmd["slot"]

집기·놓기의 실제 팔 동작(역기구학, 그리퍼)은 팔 담당 코드가 grip_fn으로 들어온다. 그 전에는
arm_unavailable로 실패한다.
"""
import threading


class ArmDriver(object):
    def start(self, cmd):
        raise NotImplementedError

    def poll(self):
        raise NotImplementedError

    def servo_angles(self):
        raise NotImplementedError


class NullArm(ArmDriver):
    """팔 없이(카메라만): 자세 명령은 바로 성공한 것으로, 집기·놓기는 arm_unavailable."""

    def __init__(self, servo_angles=None):
        self._angles = dict(servo_angles or {"1": 0.0, "5": 0.0})
        self._res = None

    def start(self, cmd):
        if cmd["op"] in ("observe", "drive_pose"):
            self._angles = dict(cmd.get("servo_angles") or self._angles)
            self._res = {"ok": True, "reason": None}
        else:
            self._res = {"ok": False, "reason": "arm_unavailable"}

    def poll(self):
        r, self._res = self._res, None
        return r

    def servo_angles(self):
        return dict(self._angles)


class ThreadedArm(ArmDriver):
    """막히는(blocking) 함수들을 스레드에서 돌린다. 실물용.

    pose_fn(servo_angles): 1·5번을 그 각도로(예: agv.arm.move). 끝나면 돌아옴.
    grip_fn(cmd) -> (ok, reason): 집기·놓기(팔 담당). 없으면 arm_unavailable.
    read_fn() -> {"1": 도, "5": 도}: 지금 각도를 읽음(없으면 마지막으로 보낸 각도).
    """

    def __init__(self, pose_fn, grip_fn=None, read_fn=None, servo_angles=None):
        self.pose_fn, self.grip_fn, self.read_fn = pose_fn, grip_fn, read_fn
        self._angles = dict(servo_angles or {"1": 0.0, "5": 0.0})
        self._res = None
        self._th = None
        self._lock = threading.Lock()

    def start(self, cmd):
        if self._th is not None and self._th.is_alive():
            raise RuntimeError("arm busy")
        self._res = None

        def work():
            try:
                if cmd["op"] in ("observe", "drive_pose"):
                    self.pose_fn(cmd["servo_angles"])
                    with self._lock:
                        self._angles = dict(cmd["servo_angles"])
                    res = {"ok": True, "reason": None}
                elif self.grip_fn is None:
                    res = {"ok": False, "reason": "arm_unavailable"}
                else:
                    ok, reason = self.grip_fn(cmd)
                    res = {"ok": bool(ok), "reason": None if ok else (reason or "arm_failed")}
            except Exception as e:      # 팔 오류는 실패로 알림(주행 루프를 죽이지 않음)
                res = {"ok": False, "reason": "arm_failed", "detail": repr(e)}
            with self._lock:
                self._res = res
        self._th = threading.Thread(target=work)
        self._th.daemon = True
        self._th.start()

    def poll(self):
        with self._lock:
            r, self._res = self._res, None
        return r

    def servo_angles(self):
        if self.read_fn is not None:
            try:
                a = self.read_fn()
                if a:
                    return dict(a)
            except Exception:
                pass
        with self._lock:
            return dict(self._angles)
