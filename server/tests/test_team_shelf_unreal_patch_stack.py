"""The four-product extension applies to an independently rebuilt v3 stack."""
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from server.tests.test_v3_unreal_patch_stack import PATCHES, ROOT, TEAM_COMMIT


class TeamShelfUnrealPatchStackTest(unittest.TestCase):
    def test_four_product_stack_applies_to_pinned_team_source(self):
        if subprocess.run(['git', 'cat-file', '-e', TEAM_COMMIT + '^{commit}'],
                          cwd=ROOT, capture_output=True).returncode:
            self.skipTest('team commit missing: ' + TEAM_COMMIT)
        archive = subprocess.run(['git', 'archive', TEAM_COMMIT, 'Source'], cwd=ROOT,
                                 capture_output=True, check=True).stdout
        patches = PATCHES + (('unreal_team_slot_milk_pilot.patch', ()),
                             ('unreal_team_shelf_four_product.patch', ()))
        with tempfile.TemporaryDirectory(prefix='team-shelf-stack-', dir=ROOT.parent) as folder:
            target = Path(folder)
            with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tar:
                for member in tar.getmembers():
                    path = Path(member.name)
                    if (path.is_absolute() or '..' in path.parts or not path.parts
                            or path.parts[0] != 'Source' or not (member.isdir() or member.isfile())):
                        self.fail('unexpected path in team source archive')
                tar.extractall(target, filter='data')
            for name, flags in patches:
                for mode in ('--check', None):
                    command = ['git', 'apply', *flags]
                    if mode:
                        command.append(mode)
                    result = subprocess.run(command + [str(ROOT / 'server/docs' / name)],
                                            cwd=target, capture_output=True)
                    self.assertEqual(result.returncode, 0, name + ': ' +
                                     result.stderr.decode('utf-8', errors='replace'))
            robot = (target / 'Source/agvSimulation/AGVRobot.cpp').read_bytes()
            socket = (target / 'Source/agvSimulation/AGVWebSocketClient.cpp').read_bytes()
            self.assertIn(b'ApplyTeamSlotPick(int32 SlotId, const FString& Product)', robot)
            self.assertIn(b'team.shelf.snapshot.request.v1', socket)


if __name__ == '__main__':
    unittest.main()
