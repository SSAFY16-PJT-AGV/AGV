# -*- coding: utf-8 -*-
"""카메라 자세(팔 서보) 저장·적용 테스트. 실제 서보 대신 가짜 TTLServo를 쓴다."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from agv import arm as A  # noqa: E402


class FakeTTL(object):
    """Waveshare SCSCtrl.TTLServo 흉내: 원시 위치 0~1023(가운데 512), 850 = 180도."""

    def __init__(self, stuck=()):
        self.raw = {1: 512, 2: 600, 3: 400, 4: 512, 5: 630}
        self.torque = {}
        self.stuck = set(stuck)
        fake = self

        class PH(object):
            def write1ByteTxRx(self, port, sid, addr, val):
                assert addr == A.ADDR_TORQUE
                fake.torque[sid] = val
                return 0, 0
        self.packetHandler = PH()
        self.portHandler = object()

    def servoAngleCtrl(self, sid, ang, direction, speed):
        if sid not in self.stuck:
            self.raw[sid] = 512 + int((850.0 / 180) * ang * direction)

    def infoSingleGet(self, sid):
        return self.raw[sid]


class TestArm(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTTL()
        A._backend = self.fake
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.tmp, "explore.json")
        with io.open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"robot": {"id": "x"}, "arm": {"servo_angles": None}}))

    def tearDown(self):
        A._backend = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self):
        from agv.explore import load_config
        return load_config(self.cfg_path)

    def test_read_move_roundtrip(self):
        A.move({"5": 25.0, "2": -10.0}, settle_s=0)
        p = A.read_pose()
        self.assertAlmostEqual(p["5"], 25.0, delta=0.3)
        self.assertAlmostEqual(p["2"], -10.0, delta=0.3)
        self.assertEqual(sorted(p), ["1", "2", "3", "5"])      # 4번(집게)은 카메라와 무관

    def test_unsaved_pose_leaves_arm_alone(self):
        before = dict(self.fake.raw)
        r = A.apply_saved(self._cfg(), say=lambda *a: None, settle_s=0)
        self.assertFalse(r["ok"])
        self.assertIn("camtool arm", r["msg"])
        self.assertEqual(self.fake.raw, before)

    def test_save_then_apply(self):
        A.move({"5": 30.0}, settle_s=0)
        bak, old = A.save_pose(self.cfg_path, A.read_pose())
        self.assertTrue(os.path.exists(bak))
        self.assertIsNone(old)
        cfg = self._cfg()
        self.assertEqual(cfg["robot"]["id"], "x")                # 다른 설정은 그대로
        self.assertAlmostEqual(A.saved_pose(cfg)["5"], 30.0, delta=0.3)
        A.move({"5": -20.0, "1": 15.0}, settle_s=0)              # 누가 팔을 움직임
        r = A.apply_saved(cfg, say=lambda *a: None, settle_s=0)
        self.assertTrue(r["ok"], r)
        self.assertAlmostEqual(A.read_pose()["5"], 30.0, delta=0.3)
        self.assertAlmostEqual(A.read_pose()["1"], 0.0, delta=0.3)

    def test_stuck_servo_warns(self):
        A.save_pose(self.cfg_path, {"1": 0.0, "2": 20.0, "3": 0.0, "5": 30.0})
        self.fake.raw[2] = 512          # 0도에 걸려서 20도로 못 감
        self.fake.stuck.add(2)
        r = A.apply_saved(self._cfg(), say=lambda *a: None, settle_s=0)
        self.assertFalse(r["ok"])
        self.assertGreater(r["diff"], A.POSE_TOL_DEG)

    def test_calib_pose_warning(self):
        A.save_pose(self.cfg_path, {"1": 0.0, "2": 20.0, "3": 0.0, "5": 30.0})
        cfg = self._cfg()
        self.assertIsNone(A.calib_pose_warning(cfg, {"1": 0, "2": 20, "3": 0, "5": 31}, u"렌즈 보정값"))
        self.assertIn(u"다시 찍어야", A.calib_pose_warning(cfg, {"5": 40}, u"렌즈 보정값"))

    def test_camtool_arm_keys_and_save(self):
        """camtool arm: 5번을 골라 2도씩 세 번 올리고 p로 저장, q로 끝(카메라 없이)."""
        from agv.tools import camtool
        start = A.read_pose()["5"]
        r, w = os.pipe()
        os.write(w, b"5+++\np\nq\n")
        os.close(w)
        old_stdin = sys.stdin
        sys.stdin = os.fdopen(r)
        try:
            rc = camtool.main(["--config", self.cfg_path, "arm", "--camera", "none", "--out", self.tmp])
        finally:
            sys.stdin.close()
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        saved = A.saved_pose(self._cfg())
        self.assertAlmostEqual(saved["5"], start + 6.0, delta=0.5)
        self.assertEqual(sorted(saved), ["1", "2", "3", "5"])

    def test_free_mode_holds_where_moved_by_hand(self):
        from agv.tools import camtool
        r, w = os.pipe()
        os.write(w, b"f\n")
        os.close(w)
        old_stdin = sys.stdin
        sys.stdin = os.fdopen(r)

        # 힘을 뺀 뒤(첫 f) 손으로 5번을 움직였다고 치고 f -> p -> q
        orig_torque = A.torque

        def torque(on, ids=A.ARM_IDS):
            orig_torque(on, ids)
            if not on:
                self.fake.raw[5] = 512 + int(850.0 / 180 * 42)
                os.write(w2, b"f\np\nq\n")
                os.close(w2)
        r2, w2 = os.pipe()
        A.torque = torque
        try:
            sys.stdin = _Chain(sys.stdin, os.fdopen(r2))
            rc = camtool.main(["--config", self.cfg_path, "arm", "--camera", "none", "--keep", "--out", self.tmp])
        finally:
            A.torque = orig_torque
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.torque.get(5), 1)              # 끝나면 힘이 다시 켜져 있음
        self.assertAlmostEqual(A.saved_pose(self._cfg())["5"], 42.0, delta=0.5)


class _Chain(object):
    """앞 파일을 다 읽으면 뒤 파일로 넘어가는 stdin(select용 fileno도 바꿔 줌)."""

    def __init__(self, a, b):
        self.files = [a, b]

    def isatty(self):
        return False

    def fileno(self):
        return self.files[0].fileno()

    def readline(self):
        while self.files:
            line = self.files[0].readline()
            if line:
                return line
            self.files.pop(0).close()
        return ""


if __name__ == "__main__":
    unittest.main()
