"""Only the original state owner may reconnect to a persisted UE world."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from server.team_shelf_world_registry import TeamShelfWorldRegistry


class TeamShelfPersistentWorldTest(unittest.TestCase):
    def test_original_owner_can_resume_only_its_existing_world(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'registry.db'
            registry = TeamShelfWorldRegistry(path)
            registry.claim_persistent('A' * 32, '1' * 64)
            TeamShelfWorldRegistry(path).claim_persistent('a' * 32, '1' * 64, resume=True)
            for world, owner, resume in [('a' * 32, '2' * 64, True), ('b' * 32, '1' * 64, True),
                                         ('a' * 32, '1' * 64, False)]:
                with self.assertRaises(ValueError):
                    registry.claim_persistent(world, owner, resume=resume)
            with self.assertRaises(ValueError):
                registry.claim('a' * 32)

    def test_concurrent_initial_claims_choose_one_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = TeamShelfWorldRegistry(Path(folder) / 'registry.db')
            def attempt(owner):
                try:
                    registry.claim_persistent('c' * 32, owner)
                    return True
                except ValueError:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(attempt, ('1' * 64, '2' * 64))), [False, True])


if __name__ == '__main__':
    unittest.main()
