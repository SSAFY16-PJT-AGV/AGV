"""Apply the limited repeat-route patch only to a disposable team snapshot."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
TEAM = os.getenv('AGV_FIELD_REPO')
MAT_DEPS = os.getenv('AGV_FIELD_MAT_DEPS')
PATCH = ROOT / 'server/docs/field_nav_sequence_limited.patch'
CASES = ROOT / 'server/tests/field_nav_sequence_cases.py'


@unittest.skipUnless(TEAM and MAT_DEPS, 'set AGV_FIELD_REPO and AGV_FIELD_MAT_DEPS for the isolated team test')
class TeamNavigatorSequenceTest(unittest.TestCase):
    def test_limited_repeat_sequence_with_team_virtual_mat(self):
        source, deps = Path(TEAM).resolve(), Path(MAT_DEPS).resolve()
        for rel in ('agv/nav/navigator.py', 'agv/link/controller.py',
                    'agv/tests/test_link.py', 'maps/sim_ssafy_v2.json',
                    'sim/mat/mats/ssafy_v2.json'):
            self.assertTrue((source / rel).is_file(), 'missing team fixture: ' + rel)
        self.assertTrue((deps / 'cv2').exists(), 'OpenCV/NumPy test dependencies are missing')
        # TEMP must be writable; caller may set TEMP/TMP to the repository .venv.
        with tempfile.TemporaryDirectory(prefix='field-sequence-') as folder:
            target = Path(folder)
            for name in ('agv', 'maps', 'sim', 'mapping'):
                shutil.copytree(source / name, target / name,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            # When TEMP is inside the server checkout, prevent git from
            # discovering that outer repository and silently skipping paths.
            apply_env = os.environ.copy()
            apply_env['GIT_CEILING_DIRECTORIES'] = str(target.parent)
            for options in (['--check'], []):
                command = subprocess.run(['git', 'apply'] + options + [str(PATCH)], cwd=target,
                                         env=apply_env, capture_output=True, text=True,
                                         encoding='utf-8', errors='replace')
                self.assertEqual(command.returncode, 0, command.stderr)
            self.assertIn('_strict_repeat', (target / 'agv/nav/navigator.py').read_text(encoding='utf-8'))
            self.assertIn('event_unconfirmed', (target / 'agv/link/controller.py').read_text(encoding='utf-8'))
            self.assertIn('process_if_current', (target / 'agv/link/mqtt.py').read_text(encoding='utf-8'))
            self.assertIn('A pre-turn spur decision', (target / 'agv/runner.py').read_text(encoding='utf-8'))
            shutil.copyfile(CASES, target / 'agv/tests/test_sequence_visit_patch.py')
            env = os.environ.copy()
            env['PYTHONUTF8'] = '1'
            env['PYTHONPATH'] = os.pathsep.join([str(target), str(deps)])
            command = subprocess.run(
                [sys.executable, '-m', 'unittest',
                 'agv.tests.test_sequence_visit_patch.SequenceTests',
                 'agv.tests.test_sequence_visit_patch.SequenceMatTests.test_team_mat_revisit_completes_with_observed_split',
                 'agv.tests.test_sequence_visit_patch.SequenceMatTests.test_team_mat_single_release_revisits_each_junction',
                 'agv.tests.test_sequence_visit_patch.SequenceMatTests.test_team_mat_repeated_release_never_fakes_a_missed_junction', '-q'],
                cwd=target, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
            self.assertEqual(command.returncode, 0, command.stdout + '\n' + command.stderr)


if __name__ == '__main__':
    unittest.main()
