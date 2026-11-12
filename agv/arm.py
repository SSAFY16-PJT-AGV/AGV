# -*- coding: utf-8 -*-
"""카메라 자세(팔 서보 각도) 읽기·맞추기·저장.

JETANK 카메라는 팔 끝에 달려 있어서 카메라가 바닥을 보는 각도와 높이가 서보 네 개로 정해진다.

    1번 팔 좌우(카메라 좌우) / 2번 팔 아래 관절 / 3번 팔 가운데 관절 / 5번 카메라 상하
    (4번은 집게라 카메라와 상관없음)

사람이 한 번 자세를 정해 `config/explore.json`의 `arm.servo_angles`에 저장하면(`camtool arm`),
탐색·사진 찍기·색 보정 촬영이 시작할 때마다 그 자세로 맞춘다. 판정 지점 거리(`geom.ctrl_ahead_mm`),
렌즈 보정, 바닥 사진은 이 자세에서 잰 값이라서 자세를 바꾸면 다시 재야 한다.

각도는 Waveshare TTLServo.servoAngleCtrl(번호, 각도, 1, 속도)의 각도(도)다. 서보 원시 위치(0~1023,
가운데 512)와 `각도 = (위치 - 512) x 180 / 850`으로 바뀐다. 서보 라이브러리는 실제로 쓸 때만 불러온다.
Python 3.6 호환.
"""
from __future__ import print_function

import io
import json
import os
import shutil
import time

ARM_IDS = ("1", "2", "3", "5")
NAMES = {"1": u"팔 좌우", "2": u"팔 아래 관절", "3": u"팔 가운데 관절", "5": u"카메라 상하"}
DEVICE = "/dev/ttyTHS1"
RAW_CENTER = 512
RAW_PER_DEG = 850.0 / 180.0
ADDR_TORQUE = 40
POSE_TOL_DEG = 3.0       # 저장한 자세와 이만큼 넘게 다르면 경고(서보가 걸렸거나 다른 자세)
# 자세를 바꾸면 다시 재야 하는 것
REDO = [u"판정 지점 거리 geom.ctrl_ahead_mm (camtool snap의 debug.png 파란 줄을 자로 잼)",
        u"렌즈 보정 (camtool snap --count 25 -> tools.calib)",
        u"바닥 좌표계 사진 (camtool ground)",
        u"패치 색은 보통 그대로 써도 되지만, 검출이 흔들리면 autocolor 다시"]

_backend = None          # 시험용 가짜 서보를 넣을 수 있음


def _ttl():
    """Waveshare SCSCtrl.TTLServo 모듈. 포트를 못 열면 RuntimeError(라이브러리는 실패하면 키 입력을
    기다렸다가 끝내 버리므로 미리 확인한다)."""
    global _backend
    if _backend is not None:
        return _backend
    if not os.path.exists(DEVICE):
        raise RuntimeError(u"서보 포트 %s가 없어요(Jetson이 아니거나 시리얼이 꺼져 있음)" % DEVICE)
    if not os.access(DEVICE, os.R_OK | os.W_OK):
        raise RuntimeError(u"서보 포트 %s에 접근 권한이 없어요(sudo chmod 666 %s 또는 dialout 그룹)" % (DEVICE, DEVICE))
    try:
        from SCSCtrl import TTLServo
    except ImportError:
        raise RuntimeError(u"SCSCtrl(Waveshare 서보 라이브러리)이 없어요. JETANK 이미지의 설치 스크립트를 확인하세요")
    except SystemExit:
        raise RuntimeError(u"서보 포트를 못 열었어요(다른 프로그램, 예: 주피터 커널이 쓰는 중인지 확인)")
    _backend = TTLServo
    return _backend


def raw_to_deg(raw):
    return round((raw - RAW_CENTER) / RAW_PER_DEG, 1)


def read_pose(ids=ARM_IDS):
    """지금 서보 각도 {"1": 도, ...}. 읽기에 실패한 서보는 None."""
    t = _ttl()
    out = {}
    for sid in ids:
        raw = None
        for _ in range(3):
            try:
                raw = int(t.infoSingleGet(int(sid)))
            except Exception:       # 통신 오류(라이브러리가 예외 종류를 정하지 않음)
                raw = None
            if raw:                 # 0은 통신 실패로 봄
                break
            time.sleep(0.05)
        out[sid] = raw_to_deg(raw) if raw else None
    return out


def move(pose, speed=150, settle_s=1.5):
    """{"5": 25.0, ...}로 움직인다. None인 서보는 건너뜀."""
    t = _ttl()
    for sid, ang in sorted(pose.items()):
        if ang is None:
            continue
        t.servoAngleCtrl(int(sid), float(ang), 1, int(speed))
        time.sleep(0.03)
    if settle_s:
        time.sleep(settle_s)


def torque(on, ids=ARM_IDS):
    """서보 힘을 켜고 끈다. 끄면 손으로 팔을 움직일 수 있다(팔이 처지니 손으로 받칠 것)."""
    t = _ttl()
    for sid in ids:
        t.packetHandler.write1ByteTxRx(t.portHandler, int(sid), ADDR_TORQUE, 1 if on else 0)
        time.sleep(0.02)


def hold_here(ids=ARM_IDS):
    """손으로 맞춘 자리를 읽고, 그 자리를 목표로 준 뒤 힘을 켠다(켤 때 예전 목표로 튀지 않게)."""
    pose = read_pose(ids)
    move(pose, speed=100, settle_s=0.0)
    torque(True, ids)
    return pose


def saved_pose(cfg):
    p = (cfg.get("arm") or {}).get("servo_angles")
    if not p:
        return None
    return dict((str(k), (None if v is None else float(v))) for k, v in p.items())


def pose_diff(a, b):
    """두 자세에서 같이 있는 서보의 최대 각도 차이. 비교할 게 없으면 None."""
    ds = [abs(a[k] - b[k]) for k in a if k in b and a[k] is not None and b[k] is not None]
    return max(ds) if ds else None


def fmt(pose):
    if not pose:
        return u"(없음)"
    return u", ".join(u"%s번 %s" % (k, u"?" if pose[k] is None else u"%+.1f도" % pose[k]) for k in sorted(pose))


def apply_saved(cfg, say=print, settle_s=1.5):
    """저장한 카메라 자세로 맞추고 다시 읽어 확인한다. 실패해도 예외를 내지 않는다.
    반환: {"saved", "pose"(읽은 값), "diff", "ok", "msg"}."""
    want = saved_pose(cfg)
    res = {"saved": want, "pose": None, "diff": None, "ok": False, "msg": None}
    if not want:
        res["msg"] = u"저장한 카메라 자세가 없어요. 지금 팔 자세 그대로 써요(정하려면: python3 -m agv.tools.camtool arm --view)"
        say(u"[주의] " + res["msg"])
        return res
    try:
        move(want, settle_s=settle_s)
        got = read_pose(list(want.keys()))
    except RuntimeError as e:
        res["msg"] = u"카메라 자세를 못 맞췄어요: %s. 지금 팔 자세 그대로 써요" % e
        say(u"[주의] " + res["msg"])
        return res
    res["pose"] = got
    res["diff"] = pose_diff(want, got)
    res["ok"] = res["diff"] is not None and res["diff"] <= POSE_TOL_DEG
    if res["ok"]:
        say(u"카메라 자세를 저장한 값으로 맞췄어요(%s)" % fmt(got))
    else:
        res["msg"] = (u"카메라 자세가 저장한 값과 %s도 달라요(저장 %s / 지금 %s). 팔이 걸렸는지 보세요"
                      % (u"?" if res["diff"] is None else u"%.1f" % res["diff"], fmt(want), fmt(got)))
        say(u"[주의] " + res["msg"])
    return res


def save_pose(config_path, pose, note=None):
    """config 파일의 arm.servo_angles에 쓴다(원래 파일은 .bak-<시각>). 반환: (백업 경로, 이전 자세)."""
    with io.open(config_path, encoding="utf-8") as f:
        user = json.load(f)
    bak = config_path + time.strftime(".bak-%Y%m%d-%H%M%S")
    shutil.copy(config_path, bak)
    arm = user.setdefault("arm", {})
    old = arm.get("servo_angles")
    arm["servo_angles"] = dict((k, None if v is None else round(float(v), 1)) for k, v in sorted(pose.items()))
    arm["saved_at"] = time.strftime("%Y-%m-%d %H:%M")
    if note:
        arm["saved_note"] = note
    with io.open(config_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(user, ensure_ascii=False, indent=2) + u"\n")
    return bak, old


def calib_pose_warning(cfg, recorded, what):
    """보정값(렌즈 보정·바닥 사진)을 찍을 때의 자세와 지금 저장한 자세가 다르면 경고 문구."""
    now = saved_pose(cfg)
    if not recorded or not now:
        return None
    d = pose_diff(dict((str(k), v) for k, v in recorded.items()), now)
    if d is not None and d > POSE_TOL_DEG:
        return u"%s은 지금과 다른 카메라 자세(최대 %.1f도 차이)에서 찍었어요. 다시 찍어야 맞아요." % (what, d)
    return None
