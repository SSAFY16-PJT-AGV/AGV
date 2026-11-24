# -*- coding: utf-8 -*-
"""로봇 ID와 로봇별 설정 파일 관리.

    # 이 젯슨의 로봇 ID 정하기(agv/config/robot_id에 적음, git에 안 올림)
    python3 -m agv.tools.robotcfg init physical-1

    # 예전처럼 공통 파일(agv/config/explore.json)에 현장 값을 저장해 둔 젯슨이면, 그 값을 로봇 파일로 옮김
    python3 -m agv.tools.robotcfg init physical-1 --move-local

    # 지금 어떤 로봇 ID·설정 파일로 도는지, 현장 값이 들어 있는지 보기
    python3 -m agv.tools.robotcfg show

로봇 파일은 agv/config/robots/<ID>/explore.json(현장 값)과 camera_calib.json(렌즈 보정값)이다.
자세한 규칙은 agv/robotcfg.py 맨 위.
"""
from __future__ import print_function, unicode_literals

import argparse
import io
import json
import os
import shutil
import subprocess
import sys

from .. import robotcfg as R

ROOT = os.path.dirname(R.HERE)


def _git_head_base(base_path):
    """공통 설정 파일의 git HEAD 내용(dict). git이 없거나 실패하면 None."""
    rel = os.path.relpath(base_path, ROOT).replace(os.sep, "/")
    try:
        out = subprocess.check_output(["git", "-C", ROOT, "show", "HEAD:" + rel], stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError):
        return None, None
    text = out.decode("utf-8")
    return json.loads(text), text


def move_local(rid, base_path=None, head=None):
    """공통 파일에서 git HEAD와 다른 값을 로봇 파일로 옮기고 공통 파일은 HEAD 내용으로 되돌린다.
    head=(dict, text)를 주면 git 대신 그것을 씀(테스트용). 반환: 옮긴 값 dict."""
    base_path = base_path or R.BASE_PATH
    head_d, head_text = head if head is not None else _git_head_base(base_path)
    if head_d is None:
        raise RuntimeError(u"git에서 %s의 원래 내용을 못 읽었어요. 저장소 안에서 실행하세요." % base_path)
    cur = R.read_json(base_path)
    moved = R.diff(head_d, cur)
    moved.pop("_note", None)
    if not moved:
        return {}
    dst = R.robot_config_path(rid)

    def put(user):
        for k, v in R.merge(user, moved).items():
            user[k] = v
    R.update(dst, put, rid)
    bak = base_path + ".before-robotcfg"
    shutil.copy(base_path, bak)
    with io.open(base_path, "w", encoding="utf-8") as f:
        f.write(head_text)
    return moved


def move_calib(rid):
    """예전 자리의 렌즈 보정값(config/camera_calib.json)을 로봇 폴더로 옮김. 반환: 옮겼으면 새 경로."""
    old = R.calib_path(None)
    new = R.calib_path(rid)
    if not os.path.exists(old) or os.path.exists(new):
        return None
    if not os.path.isdir(os.path.dirname(new)):
        os.makedirs(os.path.dirname(new))
    shutil.move(old, new)
    return new


def _fmt_keys(d, prefix=""):
    out = []
    for k, v in sorted(d.items()):
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and k != "colors" and k != "servo_angles":
            out += _fmt_keys(v, prefix + k + ".")
        else:
            out.append(prefix + k)
    return out


def cmd_show(a):
    try:
        res = R.resolve(None, a.robot)
    except ValueError as e:
        print(e)
        return 1
    cfg = res.cfg
    print(res.describe())
    print(u"현장 도구가 쓸 파일: %s" % os.path.relpath(res.write_path, ROOT))
    print(u"렌즈 보정값: %s%s" % (os.path.relpath(res.calib_path, ROOT),
                               u"" if os.path.exists(res.calib_path) else u" (없음)"))
    if res.robot and len(res.layers) > 1:
        print(u"로봇 파일에 든 값: " + u", ".join(_fmt_keys(R.read_json(res.layers[-1]))))
    arm = (cfg.get("arm") or {}).get("servo_angles")
    print(u"  카메라 자세   %s" % (arm if arm else u"아직 안 정함 (camtool arm)"))
    print(u"  색 보정       %s" % ((cfg.get("patch") or {}).get("autocolor_at") or u"아직 안 함 (autocolor --write)"))
    print(u"  패치 길이     %s" % ((cfg.get("patch") or {}).get("length_mm") or u"없음 (patch.length_mm)"))
    print(u"  판정 지점     %s mm" % (cfg.get("geom") or {}).get("ctrl_ahead_mm"))
    rob = cfg.get("robot") or {}
    print(u"  모터          trim %s, 반전 L/R %s/%s" % (rob.get("trim"), rob.get("invert_left"), rob.get("invert_right")))
    if not res.robot:
        print(u"\n로봇 ID가 없어서 공통 파일에 써요. 두 대를 쓰면 `python3 -m agv.tools.robotcfg init physical-1`")
    return 0


def cmd_init(a):
    try:
        rid = R.check_id(a.robot_id)
    except ValueError as e:
        print(e)
        return 1
    old, _ = R.robot_id(None)
    path = R.write_id_file(rid)
    print(u"이 젯슨의 로봇 ID: %s (%s)%s" % (rid, os.path.relpath(path, ROOT),
                                      u", 예전 ID %s" % old if old and old != rid else u""))
    if os.environ.get(R.ENV, "").strip() and os.environ[R.ENV].strip() != rid:
        print(u"[주의] 환경 변수 %s=%s가 이 파일보다 먼저예요." % (R.ENV, os.environ[R.ENV]))
    if a.move_local:
        try:
            moved = move_local(rid)
        except RuntimeError as e:
            print(e)
            return 1
        if moved:
            print(u"공통 파일에서 옮긴 값: " + u", ".join(_fmt_keys(moved)))
            print(u"-> %s (공통 파일은 git 원래 내용으로 되돌리고, 바꾸기 전 파일은 explore.json.before-robotcfg)"
                  % os.path.relpath(R.robot_config_path(rid), ROOT))
        else:
            print(u"공통 파일에 현장에서 바꾼 값이 없어요.")
        c = move_calib(rid)
        if c:
            print(u"렌즈 보정값을 옮겼어요: %s" % os.path.relpath(c, ROOT))
    print()
    return cmd_show(argparse.Namespace(robot=rid))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("show", help="지금 로봇 ID와 설정 파일, 현장 값")
    s.add_argument("--robot", default=None)
    i = sub.add_parser("init", help="이 젯슨의 로봇 ID 정하기")
    i.add_argument("robot_id", help="예: physical-1")
    i.add_argument("--move-local", action="store_true",
                   help="공통 파일(agv/config/explore.json)에 저장해 둔 현장 값과 렌즈 보정값을 로봇 폴더로 옮김")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        return cmd_show(a)
    if a.cmd == "init":
        return cmd_init(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
