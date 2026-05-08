from __future__ import annotations

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "/Users/imac/Documents/Programming/graphrag-implementation")
import app.graph_synthesis as gs


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestGraphSynthesis(unittest.TestCase):
    def test_prompt_contains_query_and_evidence(self):
        prompt = gs.build_graph_aware_prompt("What is firm horizon?", [{"kind": "graph", "evidence": {"seed_label": "Firm Horizon", "node_count": 2, "edge_count": 1, "labels": ["Firm Horizon"], "provenance": ["chunk-1"]}}])
        self.assertIn("USER QUERY", prompt)
        self.assertIn("What is firm horizon?", prompt)
        self.assertIn("[GRAPH]", prompt)

    def test_synthesis_fail_open_on_error(self):
        client = MagicMock()
        client.post_json = AsyncMock(side_effect=Exception("boom"))
        answer, error = _run_async(gs.synthesize_graph_aware_answer(client, "q", []))
        self.assertIsNone(answer)
        self.assertIsNotNone(error)
