import importlib
import importlib.util
import unittest


def example_map():
    return {
        "map_id": "layout_A",
        "version": 4,
        "unit": "mm",
        "nodes": [{"id": name} for name in ("N1", "N2", "N3", "N4")],
        "edges": [
            {"from": "N1", "to": "N2", "shape": [[0, 0], [100, 0]], "bidirectional": True},
            {"from": "N2", "to": "N3", "shape": [[100, 0], [200, 0]], "bidirectional": False},
            {"from": "N1", "to": "N3", "shape": [[0, 0], [0, 200], [200, 200], [200, 0]], "bidirectional": False},
        ],
    }


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("server.routing"), "routing module is not implemented")
        self.routing = importlib.import_module("server.routing")

    def test_shortest_route_uses_full_polyline_distance_and_map_identity(self):
        graph = self.routing.MapGraph(example_map())
        path = graph.shortest_path("N1", "N3")
        self.assertEqual(path.nodes, ("N1", "N2", "N3"))
        self.assertEqual(path.distance_mm, 200)
        self.assertEqual((graph.map_id, graph.version), ("layout_A", 4))

    def test_reverse_only_allowed_on_bidirectional_edge(self):
        graph = self.routing.MapGraph(example_map())
        self.assertEqual(graph.shortest_path("N2", "N1").nodes, ("N2", "N1"))
        with self.assertRaises(self.routing.NoRouteError):
            graph.shortest_path("N3", "N2")

    def test_zero_length_route_and_disconnected_target(self):
        graph = self.routing.MapGraph(example_map())
        path = graph.shortest_path("N1", "N1")
        self.assertEqual((path.nodes, path.distance_mm), (("N1",), 0))
        with self.assertRaises(self.routing.NoRouteError):
            graph.shortest_path("N1", "N4")

    def test_unknown_node_is_an_error(self):
        graph = self.routing.MapGraph(example_map())
        with self.assertRaises(self.routing.UnknownNodeError):
            graph.shortest_path("missing", "N1")
        with self.assertRaises(self.routing.UnknownNodeError):
            graph.shortest_path("N1", "missing")

    def test_rejects_invalid_or_ambiguous_graphs(self):
        variants = []
        graph = example_map(); graph["unit"] = "cm"; variants.append(graph)
        graph = example_map(); graph["nodes"].append({"id": "N1"}); variants.append(graph)
        graph = example_map(); graph["edges"][0]["to"] = "missing"; variants.append(graph)
        graph = example_map(); del graph["edges"][0]["shape"]; variants.append(graph)
        graph = example_map(); graph["edges"][0]["shape"] = [[0, 0], [0, 0]]; variants.append(graph)
        graph = example_map(); graph["edges"][0]["shape"] = [[0, 0], [float("nan"), 1]]; variants.append(graph)
        graph = example_map(); graph["edges"][0]["shape"] = [[0, 0], [True, 1]]; variants.append(graph)
        graph = example_map(); graph["edges"][0]["bidirectional"] = "false"; variants.append(graph)
        graph = example_map(); graph["edges"].append(dict(graph["edges"][0])); variants.append(graph)
        for invalid in variants:
            with self.subTest(invalid=invalid):
                with self.assertRaises(self.routing.InvalidMapError):
                    self.routing.MapGraph(invalid)


if __name__ == "__main__":
    unittest.main()
