"""Regression checks against a patched, disposable copy of the team robot."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
TEAM = os.getenv("AGV_FIELD_REPO")
MAT_DEPS = os.getenv("AGV_FIELD_MAT_DEPS")
PATCH = ROOT / "server/docs/field_nav_sequence_limited.patch"
CASES = ROOT / "server/tests/field_nav_sequence_cases.py"


@unittest.skipUnless(TEAM and MAT_DEPS, "set AGV_FIELD_REPO and AGV_FIELD_MAT_DEPS")
class TeamNavigatorSequenceRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="field-sequence-regression-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.target = Path(cls.temp.name)
        source = Path(TEAM).resolve()
        for name in ("agv", "maps", "sim", "mapping"):
            shutil.copytree(source / name, cls.target / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = os.environ.copy()
        env["GIT_CEILING_DIRECTORIES"] = str(cls.target.parent)
        applied = subprocess.run(["git", "apply", str(PATCH)], cwd=cls.target,
                                 env=env, capture_output=True, text=True, encoding="utf-8")
        if applied.returncode:
            raise RuntimeError(applied.stderr)
        shutil.copyfile(CASES, cls.target / "agv/tests/test_sequence_visit_patch.py")
        cls.env = os.environ.copy()
        cls.env["PYTHONUTF8"] = "1"
        cls.env["PYTHONPATH"] = os.pathsep.join([str(cls.target), str(Path(MAT_DEPS).resolve())])

    def run_case(self, code):
        result = subprocess.run([sys.executable, "-c", code], cwd=self.target,
                                env=self.env, capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)

    def test_missing_intermediate_observation_does_not_confirm_arrival(self):
        self.run_case('''
import json
from agv.tests.test_sequence_visit_patch import SequenceTests, order
nav, runner, link, ctl = SequenceTests().fresh()
ctl._handle(json.dumps(order(1, "C")).encode())
assert ctl.rejected is None
nav.on_patch("orange", 0)
nav.pop_stop()
# The B merge observation is missing; seeing C must not prove B was visited.
nav.on_patch("red", 200)
stop = nav.pop_stop()
assert nav._route_pos < 2, (nav._route_pos, stop)
assert nav.at_node != "C", (nav.at_node, stop)
assert stop is not None and stop["hold"] and stop["reason"] == "nav_off_route", stop
''')

    def test_disconnected_initial_order_never_resumes_robot(self):
        self.run_case('''
import json
from agv.tests.test_sequence_visit_patch import SequenceTests, order
nav, runner, link, ctl = SequenceTests().fresh()
link.send(order(1, "C"))  # Received while connected, before the control loop polls it.
link.connected = False
link.disconnected_at = 100
ctl.step(now=100)
assert runner.resumes == 0, (runner.resumes, runner.state, ctl.rejected)
assert runner.state == "hold", runner.state
assert ctl.book.current is None, ctl.book.current
''')


if __name__ == "__main__":
    unittest.main()
