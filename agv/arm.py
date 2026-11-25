# -*- coding: utf-8 -*-
"""카메라 자세(팔 서보 각도) 읽기·맞추기·저장.

JETANK 카메라는 팔 끝이 아니라 팔 아래쪽 회전 받침에 달려 있다. 카메라가 바닥을 보는 방향은
1번(받침 좌우 회전, 팔과 카메라가 같이 돎)과 5번(카메라 상하) 두 서보로 정해진다.
2·3번 팔 관절과 4번 집게는 카메라를 움직이지 않는다.

    1번 받침 좌우(팔·카메라 같이) / 2번 팔 아래 관절 / 3번 팔 가운데 관절 / 5번 카메라 상하
    (4번은 집게라 저장 자세에 넣지 않음)

2·3번도 저장 자세에 넣는 것은 팔이 화면을 가리지 않는 자리에 두기 위해서다. 그래서 팔을 들어 올리는 등
2·3번만 바꿔도 카메라 시야는 그대로다(화면에 팔이나 든 물건이 들어오는지만 보면 된다).

사람이 한 번 자세를 정해 설정의 `arm.servo_angles`에 저장하면(`camtool arm`, 로봇 ID가 있으면 `config/robots/<ID>/explore.json`),
탐색·사진 찍기·색 보정 촬영이 시작할 때마다 그 자세로 맞춘다. 판정 지점 거리(`geom.ctrl_ahead_mm`),
렌즈 보정, 바닥 사진은 이 시야에서 잰 값이라서 1번이나 5번을 바꾸면 다시 재야 한다. 2·3번만 바꿨으면 그대로 쓴다.
다만 저장 자세와의 차이 경고(`apply_saved`, `calib_pose_warning`)는 지금 네 서보를 모두 비교한다.

각도는 Waveshare TTLServo.servoAngleCtrl(번호, 각도, 1, 속도)의 각도(도)다. 서보 원시 위치(0~1023,
가운데 512)와 `각도 = (위치 - 512) x 180 / 850`으로 바뀐다. 서보 라이브러리는 실제로 쓸 때만 불러온다.
Python 3.6 호환.
"""
from __future__ import print_function

import os
import time

ARM_IDS = ("1", "2", "3", "5")
NAMES = {"1": u"받침 좌우(팔·카메라)", "2": u"팔 아래 관절", "3": u"팔 가운데 관절", "5": u"카메라 상하"}
DEVICE = "/dev/ttyTHS1"
RAW_CENTER = 512
RAW_PER_DEG = 850.0 / 180.0
ADDR_TORQUE = 40
POSE_TOL_DEG = 3.0       # 저장한 자세와 이만큼 넘게 다르면 경고(서보가 걸렸거나 다른 자세)
# 카메라 시야(1번·5번)를 바꾸면 다시 재야 하는 것. 2·3번(팔 관절)만 바꿨으면 그대로 써도 된다
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
    _add_scsctrl_path()
    try:
        from SCSCtrl import TTLServo
    except ImportError as e:
        if "serial" in str(e):
            raise RuntimeError(u"pyserial이 없어요: pip3 install --user pyserial")
        raise RuntimeError(INSTALL_HINT)
    except SystemExit:
        raise RuntimeError(u"서보 포트를 못 열었어요(다른 프로그램, 예: 주피터 커널이 쓰는 중인지 확인)")
    _backend = TTLServo
    return _backend


# Waveshare JETANK 저장소의 SCSCtrl 폴더(설치 안 하고 받아만 둔 경우)를 찾는 곳. SCSCTRL_PATH로 지정 가능
SCSCTRL_DIRS = ["~/JETANK", "~/jetbot/JETANK", "~/Notebooks/JETANK", "/workspace/JETANK",
                "/workspace/jetbot/notebooks/JETANK"]
INSTALL_HINT = (u"SCSCtrl(Waveshare 서보 라이브러리)을 못 찾았어요. 한 번만 설치하세요:\n"
                u"    cd ~ && git clone https://github.com/waveshare/JETANK.git\n"
                u"    cd ~/JETANK && pip3 install --user .\n"
                u"  (설치 없이 ~/JETANK에 받아만 둬도 찾아 써요. 다른 곳이면 SCSCTRL_PATH=<JETANK 폴더>)")


def _add_scsctrl_path():
    """SCSCtrl이 설치돼 있지 않으면 받아 둔 JETANK 폴더를 import 경로에 더한다."""
    import importlib.util
    import sys
    if importlib.util.find_spec("SCSCtrl") is not None:
        return
    dirs = [os.environ.get("SCSCTRL_PATH", "")] + SCSCTRL_DIRS
    for d in dirs:
        d = os.path.expanduser(d) if d else ""
        if d and os.path.isfile(os.path.join(d, "SCSCtrl", "TTLServo.py")):
            sys.path.insert(0, d)
            return


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


def save_pose(config_path, pose, note=None, robot=None):
    """config 파일의 arm.servo_angles에 쓴다(원래 파일은 .bak-<시각>, 로봇 파일이 없으면 새로 만듦).
    반환: (백업 경로 또는 None, 이전 자세)."""
    from .robotcfg import update
    box = {}

    def put(user):
        arm = user.setdefault("arm", {})
        box["old"] = arm.get("servo_angles")
        arm["servo_angles"] = dict((k, None if v is None else round(float(v), 1)) for k, v in sorted(pose.items()))
        arm["saved_at"] = time.strftime("%Y-%m-%d %H:%M")
        if note:
            arm["saved_note"] = note
    bak = update(config_path, put, robot)
    return bak, box.get("old")


def calib_pose_warning(cfg, recorded, what):
    """보정값(렌즈 보정·바닥 사진)을 찍을 때의 자세와 지금 저장한 자세가 다르면 경고 문구."""
    now = saved_pose(cfg)
    if not recorded or not now:
        return None
    d = pose_diff(dict((str(k), v) for k, v in recorded.items()), now)
    if d is not None and d > POSE_TOL_DEG:
        return u"%s은 지금과 다른 카메라 자세(최대 %.1f도 차이)에서 찍었어요. 다시 찍어야 맞아요." % (what, d)
    return None
