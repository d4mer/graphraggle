from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "/Users/imac/Documents/Programming/graphrag-implementation")
import app.graph_native as gn


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestGraphNativeHelpers(unittest.TestCase):
    def test_select_graph_seed_labels_caps_and_ranks(self):
        out = gn.select_graph_seed_labels(
            "firm horizon shipment planning",
            ["shipment plan", "firm horizon", "dashboard", "other", "planning node"],
            max_seeds=3,
        )
        self.assertEqual(len(out), 3)
        self.assertIn("firm horizon", out)

    def test_normalize_graph_result_extracts_provenance(self):
        payload = {
            "nodes": [{"label": "Firm Horizon", "source_id": "chunk-1"}],
            "edges": [{"path": "/docs/a.txt"}],
        }
        out = gn.normalize_graph_result("Firm Horizon", payload)
        self.assertEqual(out["seed_label"], "Firm Horizon")
        self.assertEqual(out["node_count"], 1)
        self.assertIn("chunk-1", out["provenance"])
        self.assertIn("/docs/a.txt", out["provenance"])


class TestFetchGraphNativeEvidence(unittest.TestCase):
    def test_fail_open_on_client_error(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=Exception("boom"))
        evidence, error, seeds = _run_async(gn.fetch_graph_native_evidence(client, "firm horizon process"))
        self.assertEqual(evidence, [])
        self.assertIsNotNone(error)
        self.assertEqual(seeds, [])

    def test_parses_label_search_and_graph_fetch(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=[
            ["Firm Horizon", "Shipment Plan"],
            {"nodes": [{"label": "Firm Horizon", "source_id": "chunk-1"}], "edges": []},
            {"nodes": [{"label": "Shipment Plan", "source_id": "chunk-2"}], "edges": []},
        ])
        evidence, error, seeds = _run_async(
            gn.fetch_graph_native_evidence(client, "shipment", max_seeds=2)
        )
        self.assertIsNone(error)
        self.assertEqual(len(seeds), 2)
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0]["seed_label"], seeds[0])
