"""Apply the Unreal demo patch stack to an isolated team-source snapshot.

The test requires the ce23ec8 team commit in the local Git object store. It
extracts only Source/ into a fresh temporary directory outside this checkout;
neither the user's Unreal project nor its Git working tree is touched.
"""
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
TEAM_COMMIT = 'ce23ec88f8caceb90eaea73931801bb7d0ff99bd'
PATCHES = (
    ('unreal_local_auth.patch', ('--unidiff-zero',)),
    ('unreal_action_ack.patch', ()),
    ('unreal_action_position_guard.patch', ()),
    ('unreal_shelf_inventory.patch', ()),
    ('unreal_world_recovery.patch', ()),
    ('unreal_pickup_inventory.patch', ('--unidiff-zero',)),
    ('unreal_v3_scene_guard.patch', ()),
    ('unreal_v3_display_scale.patch', ()),
)


def source_digest(path):
    digest = hashlib.sha256()
    for file in sorted((path / 'Source').rglob('*')):
        if file.is_file():
            digest.update(file.relative_to(path).as_posix().encode('utf-8'))
            digest.update(file.read_bytes())
    return digest.hexdigest()


class UnrealV3PatchStackTest(unittest.TestCase):
    def test_stack_applies_to_latest_team_commit_in_an_isolated_copy(self):
        available = subprocess.run(['git', 'cat-file', '-e', TEAM_COMMIT + '^{commit}'], cwd=ROOT,
                                   capture_output=True)
        if available.returncode:
            self.skipTest('fetch feature/digital-twin to obtain team commit ' + TEAM_COMMIT)
        archive = subprocess.run(['git', 'archive', TEAM_COMMIT, 'Source'], cwd=ROOT,
                                 capture_output=True, check=True).stdout
        with tempfile.TemporaryDirectory(prefix='v3-unreal-patches-', dir=ROOT.parent) as folder:
            target = Path(folder)
            with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tar:
                for member in tar.getmembers():
                    path = Path(member.name)
                    if (path.is_absolute() or '..' in path.parts or not path.parts
                            or path.parts[0] != 'Source' or not (member.isdir() or member.isfile())):
                        self.fail('unexpected path or link in team source archive')
                tar.extractall(target, filter='data')
            self.assertTrue((target / 'Source/agvSimulation/AGVRobot.cpp').is_file())
            for name, flags in PATCHES:
                patch = ROOT / 'server/docs' / name
                self.assertTrue(patch.is_file(), 'missing patch: ' + name)
                with self.subTest(patch=name):
                    before = source_digest(target)
                    for mode in ('--check', None):
                        command = ['git', 'apply', *flags]
                        if mode:
                            command.append(mode)
                        result = subprocess.run(command + [str(patch)], cwd=target,
                                                capture_output=True, text=True,
                                                encoding='utf-8', errors='replace')
                        self.assertEqual(result.returncode, 0, name + ': ' + result.stderr)
                    self.assertNotEqual(source_digest(target), before,
                                        'patch reported success without changing source: ' + name)
                    reverse = subprocess.run(['git', 'apply', *flags, '--reverse', '--check', str(patch)],
                                             cwd=target, capture_output=True, text=True,
                                             encoding='utf-8', errors='replace')
                    self.assertEqual(reverse.returncode, 0, name + ': ' + reverse.stderr)
            robot = (target / 'Source/agvSimulation/AGVRobot.cpp').read_text(encoding='utf-8', errors='replace')
            loader = (target / 'Source/agvSimulation/WarehouseMapLoader.cpp').read_text(encoding='utf-8', errors='replace')
            self.assertIn('mat_ssafy_v3', robot)
            self.assertIn('UpdateV3DisplayScale', robot)
            self.assertIn('ParsedMapId', loader)


if __name__ == '__main__':
    unittest.main()
