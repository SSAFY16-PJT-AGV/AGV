"""Direction-aware allocation is an explicit isolated-simulation mode."""
import tempfile
from pathlib import Path
import unittest

from server.allocation_store import AllocationStore
from server.coordinator import Coordinator, CoordinatorError
from server.field_dispatch import FieldCandidate
from server.dispatch import RobotCandidate
from server.tests.test_coordinator import FakePublisher
from server.tests.test_occurrence_allocation_lab import laboratory_map


class FieldCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AllocationStore(Path(self.tmp.name) / 'field.sqlite3')
        self.store.initialize()
        self.store.create_task('T1')
        self.map = laboratory_map()
        self.candidates = [FieldCandidate('sim-a', 'A', 'E0', True)]

    def test_field_allocation_requires_explicit_simulation_opt_in(self):
        coordinator = Coordinator(self.store, self.map, FakePublisher())
        with self.assertRaises((ValueError, RuntimeError)):
            coordinator.prepare_field('T1', 'D', self.candidates, {'type': 'none'})
        self.assertEqual(self.store.list_occupancy(), {})

    def test_field_allocation_persists_repeated_release_and_direction(self):
        publisher = FakePublisher()
        coordinator = Coordinator(self.store, self.map, publisher, field_simulation=True)
        allocation = coordinator.prepare_field('T1', 'D', self.candidates, {'type': 'none'})
        self.assertEqual(allocation['nodes'], ['A', 'B', 'C', 'B', 'D'])
        self.assertEqual(allocation['released_until'], 'B')
        self.assertEqual(self.store.list_occupancy().get(('node', 'B')), 'sim-a')
        self.assertEqual(coordinator.publish('T1')['state'], 'acked')
        self.assertEqual(publisher.published[0][0], 'sim-a')

    def test_field_mode_cannot_bypass_direction_with_generic_prepare(self):
        coordinator = Coordinator(self.store, self.map, FakePublisher(), field_simulation=True)
        with self.assertRaisesRegex(CoordinatorError, 'field simulation requires direction-aware preparation'):
            coordinator.prepare('T1', 'D', [RobotCandidate('sim-a', 'A', True)], {'type': 'none'})
        self.assertEqual(self.store.list_occupancy(), {})


if __name__ == '__main__':
    unittest.main()
