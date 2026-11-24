# -*- coding: utf-8 -*-
"""로봇별 설정(agv/robotcfg.py)과 현장 도구 저장 위치."""
from __future__ import print_function, unicode_literals

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from agv import robotcfg as R  # noqa: E402


def _write(path, d):
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False))


class _Sandbox(unittest.TestCase):
    """robotcfg의 경로를 임시 폴더로 바꿔 실제 agv/config를 건드리지 않는다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cdir = os.path.join(self.tmp, "config")
        self.saved = dict((k, getattr(R, k)) for k in ("CONFIG_DIR", "BASE_PATH", "ROBOTS_DIR", "ID_FILE"))
        R.CONFIG_DIR = self.cdir
        R.BASE_PATH = os.path.join(self.cdir, "explore.json")
        R.ROBOTS_DIR = os.path.join(self.cdir, "robots")
        R.ID_FILE = os.path.join(self.cdir, "robot_id")
        self.env = os.environ.pop(R.ENV, None)
        _write(R.BASE_PATH, {"robot": {"id": "agv1", "trim": 0.0},
                             "geom": {"ctrl_ahead_mm": 116, "patch_ahead_mm": 125}})
        self.defaults = {"robot": {"id": "agv1", "trim": 0.0, "speed_mm_s": 150},
                         "geom": {"ctrl_ahead_mm": 100}, "arm": {"servo_angles": None},
                         "patch": {"colors": {"red": [0]}, "length_mm": None}}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(R, k, v)
        if self.env is not None:
            os.environ[R.ENV] = self.env
        else:
            os.environ.pop(R.ENV, None)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestResolve(_Sandbox):
    def test_no_robot_reads_and_writes_common_file(self):
        res = R.resolve(defaults=self.defaults)
        self.assertIsNone(res.robot)
        self.assertEqual(res.write_path, R.BASE_PATH)
        self.assertEqual(res.cfg["geom"]["ctrl_ahead_mm"], 116)
        self.assertEqual(res.cfg["robot"]["speed_mm_s"], 150)   # 기본값도 남음
        self.assertEqual(res.calib_path, os.path.join(self.cdir, "camera_calib.json"))

    def test_robot_file_overrides_common(self):
        _write(R.robot_config_path("physical-1"), {"geom": {"ctrl_ahead_mm": 130}, "robot": {"trim": 0.05}})
        res = R.resolve(robot="physical-1", defaults=self.defaults)
        self.assertEqual(res.robot, "physical-1")
        self.assertEqual(res.cfg["robot"]["id"], "physical-1")
        self.assertEqual(res.cfg["geom"]["ctrl_ahead_mm"], 130)
        self.assertEqual(res.cfg["geom"]["patch_ahead_mm"], 125)   # 공통 값은 그대로
        self.assertAlmostEqual(res.cfg["robot"]["trim"], 0.05)
        self.assertEqual(res.write_path, R.robot_config_path("physical-1"))
        self.assertEqual(len(res.layers), 2)
        self.assertTrue(res.calib_path.endswith(os.path.join("robots", "physical-1", "camera_calib.json")))

    def test_robot_without_file_yet(self):
        res = R.resolve(robot="physical-2", defaults=self.defaults)
        self.assertEqual(res.cfg["geom"]["ctrl_ahead_mm"], 116)
        self.assertEqual(res.write_path, R.robot_config_path("physical-2"))
        self.assertFalse(os.path.exists(res.write_path))
        self.assertEqual(res.cfg["robot"]["id"], "physical-2")

    def test_id_order_flag_env_file(self):
        R.write_id_file("physical-1")
        self.assertEqual(R.robot_id(), ("physical-1", os.path.relpath(R.ID_FILE, os.path.dirname(R.HERE))))
        os.environ[R.ENV] = "physical-2"
        self.assertEqual(R.robot_id()[0], "physical-2")
        self.assertEqual(R.robot_id("physical-3")[0], "physical-3")

    def test_explicit_config_ignores_robot_files(self):
        R.write_id_file("physical-1")
        _write(R.robot_config_path("physical-1"), {"geom": {"ctrl_ahead_mm": 130}})
        other = os.path.join(self.tmp, "try.json")
        _write(other, {"geom": {"ctrl_ahead_mm": 90}})
        res = R.resolve(config=other, defaults=self.defaults)
        self.assertEqual(res.cfg["geom"]["ctrl_ahead_mm"], 90)
        self.assertEqual(res.write_path, other)
        self.assertIsNone(res.robot)

    def test_bad_ids(self):
        for bad in ("", "a/b", "a b", "+", "sim-1", "-x", "x" * 200):
            with self.assertRaises(ValueError):
                R.check_id(bad)
        for ok in ("physical-1", "physical_2", "agv1", "AGV.01"):
            self.assertEqual(R.check_id(ok), ok)


class TestWrite(_Sandbox):
    def test_update_creates_robot_file_then_backs_up(self):
        p = R.robot_config_path("physical-1")
        bak = R.update(p, lambda u: u.setdefault("geom", {}).update({"ctrl_ahead_mm": 140}), "physical-1")
        self.assertIsNone(bak)
        d = R.read_json(p)
        self.assertEqual(d["geom"]["ctrl_ahead_mm"], 140)
        self.assertIn("physical-1", d["_note"])
        bak = R.update(p, lambda u: u["geom"].update({"ctrl_ahead_mm": 150}), "physical-1")
        self.assertTrue(os.path.exists(bak))
        self.assertEqual(R.read_json(p)["geom"]["ctrl_ahead_mm"], 150)
        self.assertEqual(R.read_json(R.BASE_PATH)["geom"]["ctrl_ahead_mm"], 116)   # 공통 파일은 그대로

    def test_save_pose_goes_to_robot_file(self):
        from agv import arm as A
        p = R.robot_config_path("physical-2")
        bak, old = A.save_pose(p, {"1": 0.0, "2": -10.0, "3": 20.0, "5": 30.0}, robot="physical-2")
        self.assertIsNone(bak)
        self.assertIsNone(old)
        res = R.resolve(robot="physical-2", defaults=self.defaults)
        self.assertEqual(res.cfg["arm"]["servo_angles"]["5"], 30.0)
        self.assertNotIn("arm", R.read_json(R.BASE_PATH))

    def test_two_robots_do_not_share_values(self):
        R.update(R.robot_config_path("physical-1"), lambda u: u.update({"robot": {"trim": 0.1}}), "physical-1")
        R.update(R.robot_config_path("physical-2"), lambda u: u.update({"robot": {"trim": -0.1}}), "physical-2")
        a = R.resolve(robot="physical-1", defaults=self.defaults).cfg
        b = R.resolve(robot="physical-2", defaults=self.defaults).cfg
        self.assertAlmostEqual(a["robot"]["trim"], 0.1)
        self.assertAlmostEqual(b["robot"]["trim"], -0.1)


class TestMoveLocal(_Sandbox):
    def test_moves_field_values_and_restores_common(self):
        from agv.tools import robotcfg as T
        head = R.read_json(R.BASE_PATH)
        head_text = json.dumps(head, ensure_ascii=False, indent=2) + "\n"
        cur = json.loads(head_text)
        cur["geom"]["ctrl_ahead_mm"] = 133
        cur["arm"] = {"servo_angles": {"1": 0.0, "5": 25.0}}
        _write(R.BASE_PATH, cur)
        moved = T.move_local("physical-1", head=(head, head_text))
        self.assertEqual(moved["geom"], {"ctrl_ahead_mm": 133})
        self.assertIn("arm", moved)
        self.assertNotIn("robot", moved)                      # 바꾸지 않은 값은 안 옮김
        self.assertEqual(R.read_json(R.BASE_PATH), head)       # 공통 파일은 원래대로
        res = R.resolve(robot="physical-1", defaults=self.defaults)
        self.assertEqual(res.cfg["geom"]["ctrl_ahead_mm"], 133)
        self.assertEqual(res.cfg["geom"]["patch_ahead_mm"], 125)
        self.assertEqual(res.cfg["arm"]["servo_angles"]["5"], 25.0)

    def test_nothing_to_move(self):
        from agv.tools import robotcfg as T
        head = R.read_json(R.BASE_PATH)
        self.assertEqual(T.move_local("physical-1", head=(head, json.dumps(head))), {})
        self.assertFalse(os.path.exists(R.robot_config_path("physical-1")))

    def test_move_calib(self):
        from agv.tools import robotcfg as T
        _write(R.calib_path(None), {"model": "fisheye"})
        new = T.move_calib("physical-1")
        self.assertEqual(new, R.calib_path("physical-1"))
        self.assertFalse(os.path.exists(R.calib_path(None)))
        self.assertEqual(R.read_json(new)["model"], "fisheye")


class TestToolArgs(_Sandbox):
    def test_explore_args_pick_robot_and_write_target(self):
        import argparse
        from agv.explore import add_config_args, settings_from_args
        ap = argparse.ArgumentParser()
        add_config_args(ap)
        os.environ[R.ENV] = "physical-2"
        a = ap.parse_args([])
        res = settings_from_args(ap, a)
        self.assertEqual(res.robot, "physical-2")
        self.assertEqual(a.config, R.robot_config_path("physical-2"))
        a = ap.parse_args(["--robot-id", "physical-1"])       # 예전 serve 옵션 이름도 됨
        self.assertEqual(settings_from_args(ap, a).robot, "physical-1")


if __name__ == "__main__":
    unittest.main()
