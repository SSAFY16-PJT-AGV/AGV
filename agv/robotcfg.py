# -*- coding: utf-8 -*-
"""로봇별 설정 파일.

설정은 세 겹으로 읽는다(뒤가 앞을 덮어씀).

1. `linecam.DEFAULT_CONFIG` - 코드 기본값
2. `agv/config/explore.json` - 팀 공통 값(git에 올림)
3. `agv/config/robots/<로봇 ID>/explore.json` - 그 로봇에서 잰 값(카메라 자세, 색, 판정 지점 거리, trim 등)

현장 도구(`camtool arm`의 저장, `autocolor --write`, `calib --write`)는 3번 파일에 쓴다.
로봇마다 파일이 달라서 두 대가 같은 저장소를 받아도 서로 덮어쓰거나 `git pull`에서 부딪히지 않는다.
렌즈 보정값도 `agv/config/robots/<로봇 ID>/camera_calib.json`에 둔다.

로봇 ID를 정하는 순서: 명령의 `--robot` > 환경 변수 `AGV_ROBOT_ID` > `agv/config/robot_id` 파일(git에 안 올림).
젯슨마다 한 번 `python3 -m agv.tools.robotcfg init physical-1`을 하면 `robot_id` 파일이 생긴다.

로봇 ID가 없으면 예전처럼 공통 파일 하나만 읽고 거기에 쓴다. `--config <파일>`을 직접 주면
그 파일 하나만 읽고 거기에 쓴다(영상 재생 실험·테스트용, 로봇 파일은 겹치지 않음).
"""
from __future__ import print_function, unicode_literals

import copy
import io
import json
import os
import re
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(HERE, "config")
BASE_PATH = os.path.join(CONFIG_DIR, "explore.json")
ROBOTS_DIR = os.path.join(CONFIG_DIR, "robots")
ID_FILE = os.path.join(CONFIG_DIR, "robot_id")
ENV = "AGV_ROBOT_ID"

# 서버(field_robot_acl_review.py)와 같은 규칙. MQTT 토픽 한 칸에 들어가야 하므로 / + # 공백 금지.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def check_id(rid):
    """로봇 ID가 쓸 수 있는 모양인지. 틀리면 ValueError."""
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        raise ValueError(u"로봇 ID는 영문·숫자로 시작하고 영문·숫자·_·.·-만 쓸 수 있어요(128자 이하): %r" % (rid,))
    if rid.startswith("sim-"):
        raise ValueError(u"'sim-'으로 시작하는 ID는 서버가 가상 로봇으로 봐요. 실물은 physical-1처럼 지어 주세요: %r" % rid)
    return rid


def _read_id_file(path=None):
    path = path or ID_FILE
    try:
        with io.open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except IOError:
        pass
    return None


def robot_id(explicit=None, id_file=None):
    """(로봇 ID, 어디서 정했는지) 반환. 없으면 (None, None)."""
    if explicit:
        return check_id(explicit), "--robot"
    env = os.environ.get(ENV, "").strip()
    if env:
        return check_id(env), ENV
    f = _read_id_file(id_file)
    if f:
        return check_id(f), os.path.relpath(id_file or ID_FILE, os.path.dirname(HERE))
    return None, None


def robot_dir(rid):
    return os.path.join(ROBOTS_DIR, rid)


def robot_config_path(rid):
    return os.path.join(robot_dir(rid), "explore.json")


def calib_path(rid):
    """렌즈 보정값 파일. 로봇 ID가 없으면 예전 자리(config/camera_calib.json)."""
    if rid:
        return os.path.join(robot_dir(rid), "camera_calib.json")
    return os.path.join(CONFIG_DIR, "camera_calib.json")


def read_json(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


class Resolved(object):
    """읽은 설정과 그 출처.

    cfg: 합친 설정, write_path: 현장 도구가 쓸 파일, robot: 로봇 ID(없으면 None),
    layers: 읽은 파일 목록, source: 로봇 ID를 정한 곳.
    """

    def __init__(self, cfg, write_path, robot, layers, source):
        self.cfg = cfg
        self.write_path = write_path
        self.robot = robot
        self.layers = layers
        self.source = source

    @property
    def calib_path(self):
        return calib_path(self.robot)

    def describe(self):
        if self.robot:
            return u"로봇 %s(%s) - 설정 %s" % (self.robot, self.source, u" + ".join(_short(p) for p in self.layers))
        return u"로봇 ID 없음 - 설정 %s" % u" + ".join(_short(p) for p in self.layers)


def _short(p):
    try:
        return os.path.relpath(p, os.path.dirname(HERE))
    except ValueError:
        return p


def resolve(config=None, robot=None, defaults=None, base_path=None, id_file=None):
    """설정을 읽는다. config를 주면 그 파일 하나만, 아니면 공통 + 로봇 파일."""
    if defaults is None:
        from .linecam import DEFAULT_CONFIG as defaults
    if config:
        cfg = merge(defaults, read_json(config))
        rid = None
        if robot:
            rid = check_id(robot)
            cfg.setdefault("robot", {})["id"] = rid
        return Resolved(cfg, config, rid, [config], "--robot" if robot else None)
    base_path = base_path or BASE_PATH
    cfg = merge(defaults, read_json(base_path))
    layers = [base_path]
    rid, src = robot_id(robot, id_file)
    if not rid:
        return Resolved(cfg, base_path, None, layers, None)
    rp = robot_config_path(rid)
    if os.path.exists(rp):
        cfg = merge(cfg, read_json(rp))
        layers.append(rp)
    cfg.setdefault("robot", {})["id"] = rid
    return Resolved(cfg, rp, rid, layers, src)


NEW_NOTE = (u"로봇 %s에서 잰 값. agv/config/explore.json(공통)을 덮어쓴다. "
            u"camtool arm 저장, autocolor --write, calib --write가 이 파일을 고친다.")


def update(path, mutate, robot=None):
    """설정 파일을 고친다. mutate(user_dict)가 dict를 바꾼다. 원래 파일은 .bak-<시각>으로 남긴다.

    파일이 없으면(로봇 파일 첫 저장) 새로 만든다. 반환: 백업 경로(새 파일이면 None).
    """
    bak = None
    if os.path.exists(path):
        user = read_json(path)
        bak = path + time.strftime(".bak-%Y%m%d-%H%M%S")
        shutil.copy(path, bak)
    else:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        user = {"_note": NEW_NOTE % (robot or u"?")}
    mutate(user)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(user, ensure_ascii=False, indent=2) + u"\n")
    os.rename(tmp, path)
    return bak


def write_id_file(rid, path=None):
    path = path or ID_FILE
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(u"# 이 젯슨의 로봇 ID (git에 안 올림). agv/robotcfg.py 참고\n%s\n" % check_id(rid))
    return path


def diff(base, cur):
    """cur에서 base와 다른 값만 남긴 dict(로컬에서 고친 공통 파일을 로봇 파일로 옮길 때)."""
    out = {}
    for k, v in cur.items():
        if k not in base:
            out[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(base[k], dict):
            sub = diff(base[k], v)
            if sub:
                out[k] = sub
        elif v != base[k]:
            out[k] = copy.deepcopy(v)
    return out
