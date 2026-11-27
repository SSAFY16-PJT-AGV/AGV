"""A consumed Unreal world cannot restart as a fresh demo fleet."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from server.team_shelf_world_registry import TeamShelfWorldRegistry


class TeamShelfWorldRegistryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='team-world-registry-')
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'worlds.sqlite3'

    def test_same_world_is_rejected_after_reopen_including_case_change(self):
        TeamShelfWorldRegistry(self.path).claim('A' * 32)
        with self.assertRaises(ValueError):
            TeamShelfWorldRegistry(self.path).claim('a' * 32)
        TeamShelfWorldRegistry(self.path).claim('b' * 32)

    def test_concurrent_instances_allow_only_one_claim(self):
        one = TeamShelfWorldRegistry(self.path)
        two = TeamShelfWorldRegistry(self.path)
        def claim(registry):
            try:
                registry.claim('c' * 32)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(claim, (one, two))), [False, True])

    def test_invalid_existing_file_fails_closed(self):
        self.path.write_bytes(b'not a registry')
        with self.assertRaises((ValueError, OSError)):
            TeamShelfWorldRegistry(self.path)
