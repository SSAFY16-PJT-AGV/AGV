"""The physical-branch compatibility check only extracts safe snapshots."""
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path
import unittest

from server.tools.physical_branch_compat_check import extract_snapshot, parse_mosquitto_version, ensure_reviewed_refs


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        for name, content, link in entries:
            item = tarfile.TarInfo(name)
            if link:
                item.type = tarfile.SYMTYPE
                item.linkname = link
                tar.addfile(item)
            else:
                data = content.encode('utf-8')
                item.size = len(data)
                tar.addfile(item, io.BytesIO(data))
    return output.getvalue()


class PhysicalBranchCompatCheckTests(unittest.TestCase):
    def test_extracts_regular_files_under_own_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            extract_snapshot(archive([('server/a.py', 'safe', None)]), folder)
            self.assertEqual((folder / 'server/a.py').read_text(encoding='utf-8'), 'safe')

    def test_rejects_escaping_paths_and_links_before_extracting_any_file(self):
        for unsafe in ('../outside', '/absolute', 'server/../../outside', 'server/..\\outside'):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                with self.assertRaises(ValueError):
                    extract_snapshot(archive([('valid', 'safe', None), (unsafe, 'bad', None)]), folder)
                self.assertFalse((folder / 'valid').exists())
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with self.assertRaises(ValueError):
                extract_snapshot(archive([('alias', '', '../outside')]), folder)

    def test_requires_mosquitto_two_for_known_default_loopback(self):
        self.assertEqual(parse_mosquitto_version('mosquitto version 2.1.2\n'), (2, 1, 2))
        for value in ('mosquitto version 1.6.15', 'unknown', 'mosquitto version 0.0.0',
                      'mosquitto version 3.0.0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_mosquitto_version(value)

    def test_changed_or_missing_ref_is_not_reported_as_reviewed(self):
        with tempfile.TemporaryDirectory() as temp:
            subprocess.run(['git', 'init', '-q'], cwd=temp, check=True, capture_output=True)
            subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                            'commit', '-q', '--allow-empty', '-m', 'one'],
                           cwd=temp, check=True, capture_output=True)
            commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=temp, check=True,
                                    capture_output=True, text=True).stdout.strip()
            ensure_reviewed_refs(Path(temp), {'HEAD': commit})
            for refs in ({'HEAD': '0' * 40}, {'refs/remotes/origin/missing': commit}):
                with self.subTest(refs=refs), self.assertRaises(ValueError):
                    ensure_reviewed_refs(Path(temp), refs)


if __name__ == '__main__':
    unittest.main()
