# -*- coding: utf-8 -*-
"""JETANK 하드웨어 어댑터: 카메라, 모터, 팔(카메라 자세).

jetbot·SCSCtrl은 Jetson에만 있으므로 쓸 때만 import한다(노트북·테스트에서는 불러오지 않음).
Python 3.6 호환.
"""
import time

import cv2


# ---------- 카메라 ----------
def gst_pipeline(c):
    """nvarguscamerasrc 파이프라인. 노출·화이트밸런스 잠금(aelock/awblock)은 JetPack 4.4 이상."""
    src = "nvarguscamerasrc sensor-id=%d" % c.get("sensor_id", 0)
    if c.get("lock_exposure", True):
        src += " aelock=true awblock=true"
    if c.get("extra"):
        src += " " + c["extra"]
    return (
        "%s ! video/x-raw(memory:NVMM), width=%d, height=%d, framerate=%d/1 ! "
        "nvvidconv flip-method=%d ! video/x-raw, width=%d, height=%d, format=BGRx ! "
        "videoconvert ! video/x-raw, format=BGR ! appsink drop=true max-buffers=1 sync=false"
        % (src, c.get("capture_width", 1280), c.get("capture_height", 720), c.get("fps", 30),
           c.get("flip", 0), c["width"], c["height"]))


class GstCamera(object):
    def __init__(self, c):
        self.pipeline = gst_pipeline(c)
        self.cap = cv2.VideoCapture(self.pipeline, cv2.CAP_GSTREAMER)
        if not self.cap.isOpened():
            raise RuntimeError(
                "카메라를 열지 못했어요. 주피터 노트북 등 다른 프로그램이 카메라를 쓰고 있지 않은지, "
                "aelock을 지원하지 않는 JetPack이면 camera.lock_exposure를 false로 바꿔 보세요.\n"
                + self.pipeline)
        for _ in range(10):         # 자동 노출이 잡힐 때까지 몇 장 버림
            self.cap.read()

    def read(self):
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self):
        self.cap.release()


class JetbotCamera(object):
    """jetbot.Camera(트레이틀릿 기반). 노출 잠금은 안 되지만 기본 이미지에서 바로 동작."""

    def __init__(self, c):
        from jetbot import Camera
        self.cam = Camera.instance(width=c["width"], height=c["height"])
        time.sleep(1.0)

    def read(self):
        return self.cam.value.copy()

    def close(self):
        self.cam.stop()


class VideoCamera(object):
    """녹화 영상 재생(오프라인 검출 튜닝용)."""

    def __init__(self, path, c):
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError("영상을 열지 못했어요: " + path)
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 20.0
        self.size = (c["width"], c["height"])

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            return None
        if (frame.shape[1], frame.shape[0]) != self.size:
            frame = cv2.resize(frame, self.size)
        return frame

    def close(self):
        self.cap.release()


def open_camera(spec, c):
    if spec == "gst":
        return GstCamera(c)
    if spec == "jetbot":
        return JetbotCamera(c)
    if spec.startswith("video:"):
        return VideoCamera(spec[len("video:"):], c)
    raise ValueError("알 수 없는 카메라: " + spec)


# ---------- 모터 ----------
class NullMotors(object):
    def set(self, left, right):
        pass

    def stop(self):
        pass


class JetbotMotors(object):
    def __init__(self, invert_left=False, invert_right=False, trim=0.0):
        from jetbot import Robot
        self.robot = Robot()
        self.sl = -1.0 if invert_left else 1.0
        self.sr = -1.0 if invert_right else 1.0
        self.trim = trim            # 직진 시 한쪽으로 쏠리면 보정(+면 왼쪽을 더 세게)

    def set(self, left, right):
        self.robot.set_motors(self.sl * (left + self.trim), self.sr * (right - self.trim))

    def stop(self):
        self.robot.stop()


# ---------- 팔(카메라 자세) ----------
def set_arm_pose(servo_angles, speed=150, settle_s=1.5):
    """{"1": 0, "5": 25} 처럼 서보 번호별 각도. 1번=카메라 좌우(PAN), 5번=카메라 상하(TILT)."""
    from SCSCtrl import TTLServo
    for sid, ang in sorted(servo_angles.items()):
        TTLServo.servoAngleCtrl(int(sid), int(ang), 1, speed)
        time.sleep(0.05)
    time.sleep(settle_s)
