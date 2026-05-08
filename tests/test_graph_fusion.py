from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "/Users/imac/Documents/Programming/graphrag-implementation")
import app.graph_fusion as gf


class TestGraphFusion(unittest.TestCase):
    def test_vector_then_graph_order(self):
        citations = [{"path": "/docs/a.txt", "content": "a"}]
        graph = [{"seed_label": "A", "provenance": ["/docs/b.txt"]}]
        fused = gf.fuse_graph_and_vector_evidence(citations, graph)
        self.assertEqual(fused[0]["kind"], "vector")
        self.assertEqual(fused[1]["kind"], "graph")

    def test_skip_graph_if_provenance_already_present(self):
        citations = [{"path": "/docs/a.txt", "content": "a"}]
        graph = [{"seed_label": "A", "provenance": ["/docs/a.txt"]}]
        fused = gf.fuse_graph_and_vector_evidence(citations, graph)
        self.assertEqual(len(fused), 1)
