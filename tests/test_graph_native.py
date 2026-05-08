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

    def test_build_graph_native_metadata(self):
        meta = gn.build_graph_native_metadata(
            enabled=True,
            applied=False,
            error="graph_native_query_family_skipped",
            seed_labels=["Firm Horizon"],
            result_count=0,
        )
        self.assertTrue(meta["graph_native_enabled"])
        self.assertFalse(meta["graph_native_applied"])
        self.assertEqual(meta["graph_native_error"], "graph_native_query_family_skipped")
        self.assertEqual(meta["graph_native_seed_labels"], ["Firm Horizon"])
        self.assertEqual(meta["graph_native_result_count"], 0)


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
