"""Optional team-source test for the conservative repeated-route patch.

This exercises a fresh temporary copy, never the user's robot checkout or
hardware. Set AGV_FIELD_REPO to the field-run source snapshot to enable it.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'server/docs/field_nav_repeat_guard.patch'
TEAM = os.getenv('AGV_FIELD_REPO')

SCRIPT = r'''
import copy
import json
from agv.link import FakeLink, LinkController
from agv.nav import NavMap, Navigator

data = {'map_id': 'guard-test', 'version': 0, 'nodes': [
    {'id': 'X'}, {'id': 'A'}, {'id': 'B'},
    {'id': 'C', 'kind': 'patch', 'uturn': True}, {'id': 'D'}],
    'edges': [
        {'id': 'E0', 'from': 'X', 'to': 'A', 'length_mm': 10, 'bidirectional': True},
        {'id': 'E1', 'from': 'A', 'to': 'B', 'length_mm': 10, 'bidirectional': True},
        {'id': 'E2', 'from': 'B', 'to': 'C', 'length_mm': 10, 'bidirectional': True},
        {'id': 'E3', 'from': 'B', 'to': 'D', 'length_mm': 10, 'bidirectional': True}]}
nav = Navigator(NavMap(data), start='A')
nav.set_route(['A', 'B', 'D'], release='B')
prior = (list(nav.route), set(nav.allowed), nav.target, nav.final,
         nav._route_pos, nav._target_i)
try:
    nav.set_route(['A', 'B', 'C', 'B', 'D'], release='D')
except ValueError as exc:
    assert 'repeated' in str(exc), str(exc)
else:
    raise AssertionError('repeated route unexpectedly accepted')
assert (nav.route, nav.allowed, nav.target, nav.final,
        nav._route_pos, nav._target_i) == prior

class Runner:
    state = 'hold'
    hold_reason = 'wait_order'
    finished = False
    odo = 0
    resumes = 0
    def hold(self, reason):
        self.state, self.hold_reason = 'hold', reason
    def resume(self):
        self.resumes += 1
        self.state = 'running'
    def policy_changed(self):
        raise AssertionError('rejected order changed policy')

nav = Navigator(NavMap(data), start='A')
link, runner = FakeLink('sim-r1'), Runner()
ctl = LinkController(link, nav, data, start_node='A')
ctl.attach(runner)
order = {'order_id':'T1', 'seq':1, 'map_id':'guard-test@v0',
         'nodes':['A','B','C','B','D'], 'released_until':'D',
         'action_at_end':{'type':'none'}}
ctl._handle(json.dumps(order).encode('utf-8'))
assert ctl.rejected and 'repeated' in ctl.rejected['reason'], ctl.rejected
assert ctl.book.current is None and runner.resumes == 0 and nav.route is None
assert runner.state == 'hold' and runner.hold_reason == 'wait_order'
assert not link.events
order['nodes'] = ['A', 'B', 'D']
ctl._handle(json.dumps(order).encode('utf-8'))
assert ctl.rejected is None and ctl.book.current is not None and runner.resumes == 1
'''


@unittest.skipUnless(TEAM, 'set AGV_FIELD_REPO to team field-run checkout')
class TeamNavigatorRepeatGuardTests(unittest.TestCase):
    def test_patch_rejects_repeat_without_starting_and_accepts_unique_route(self):
        source = Path(TEAM).resolve() / 'agv'
        self.assertTrue((source / 'nav/navigator.py').is_file())
        # Place outside this Git checkout: `git apply` invoked from a nested
        # ignored directory can silently filter all paths and apply nothing.
        with tempfile.TemporaryDirectory(prefix='field-nav-guard-', dir=ROOT.parent) as folder:
            target = Path(folder)
            shutil.copytree(source, target / 'agv', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            for options in (['--check'], []):
                result = subprocess.run(['git', 'apply'] + options + [str(PATCH)], cwd=target,
                                        capture_output=True, text=True, encoding='utf-8', errors='replace')
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('repeated route nodes are not supported',
                          (target / 'agv/nav/navigator.py').read_text(encoding='utf-8'))
            env = os.environ.copy()
            env['PYTHONPATH'] = str(target)
            env['PYTHONUTF8'] = '1'
            result = subprocess.run([sys.executable, '-c', SCRIPT], cwd=target, env=env,
                                    capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)


if __name__ == '__main__':
    unittest.main()
