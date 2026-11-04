import importlib
import importlib.util
import unittest

from server.routing import MapGraph, UnknownNodeError


def graph():
    return MapGraph({
        "map_id": "map", "version": 1, "unit": "mm",
        "nodes": [{"id": "A", "pos": [0, 0]},
                  {"id": "B", "pos": [100, 0]},
                  {"id": "C", "pos": [200, 0]},
                  {"id": "X", "pos": [400, 0]}],
        "edges": [{"from": "A", "to": "B", "shape": [[0, 0], [100, 0]],
                   "bidirectional": True},
                  {"from": "B", "to": "C", "shape": [[100, 0], [200, 0]],
                   "bidirectional": False}],
    })


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("server.dispatch"),
                             "dispatch module is not implemented")
        self.dispatch = importlib.import_module("server.dispatch")

    def candidate(self, name, node, available=True):
        return self.dispatch.RobotCandidate(name, node, available)

    def test_choose_closest_available_reachable_robot(self):
        choices = [self.candidate("r1", "A"), self.candidate("r2", "B"),
                   self.candidate("r3", "C", False)]
        selected = self.dispatch.choose_robot(graph(), "C", choices)
        self.assertEqual(selected.robot_id, "r2")
        self.assertEqual(selected.route.nodes, ("B", "C"))
        self.assertEqual(selected.route.distance_mm, 100)

    def test_exact_distance_ties_resolve_by_robot_id(self):
        choices = [self.candidate("z", "B"), self.candidate("a", "B")]
        self.assertEqual(self.dispatch.choose_robot(graph(), "C", choices).robot_id, "a")
        self.assertEqual(self.dispatch.choose_robot(graph(), "C", reversed(choices)).robot_id, "a")

    def test_excludes_directed_unreachable_and_unavailable_robots(self):
        choices = [self.candidate("r1", "X"), self.candidate("r2", "C"),
                   self.candidate("r3", "A", False)]
        self.assertIsNone(self.dispatch.choose_robot(graph(), "A", choices))

    def test_pickup_must_exist_even_with_no_candidates(self):
        with self.assertRaises(UnknownNodeError):
            self.dispatch.choose_robot(graph(), "missing", [])

    def test_invalid_candidate_or_duplicate_id_never_selected(self):
        for choices in ([self.candidate("", "A")],
                        [self.candidate("r1", "A", 1)],
                        [self.candidate("r1", "A"), self.candidate("r1", "B", False)],
                        [self.candidate("r1", "")],
                        [{"robot_id": "r1", "node": "A", "available": True}]):
            with self.subTest(choices=choices), self.assertRaises((TypeError, ValueError)):
                self.dispatch.choose_robot(graph(), "C", choices)

    def test_available_robot_at_unknown_node_is_error_not_unreachable(self):
        with self.assertRaises(UnknownNodeError):
            self.dispatch.choose_robot(graph(), "C", [self.candidate("r1", "missing")])


if __name__ == "__main__":
    unittest.main()
