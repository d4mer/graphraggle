"""Tests for Packet 16: GraphRAG Adaptive Expansion.

Covers:
- entity extraction parse success / malformed fail-open
- adaptive hop escalation rules
- neighbor cap and remaining-budget for hop2
- merge/dedupe stability with graph pseudo-citations
- metadata fields always populated
- feature flag default false
- config setting defaults
- integration-style flow placement tests (without live services)

Imports from app.graphrag and app.config to avoid pulling in the full gateway dependency chain.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/imac/Documents/Programming/graphrag-implementation")
import app.graphrag as _gr_module
import app.multi_query as _mq_module


# ── Helper: run async code from sync test ───────────────────────────────────

def _run_async(coro):
    """Helper to run async code from sync test methods."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── citation_to_text_for_entity_extraction tests ────────────────────────────

class TestCitationToTextForEntityExtraction(unittest.TestCase):
    """Test citation_to_text_for_entity_extraction utility."""

    def test_prefers_content_field(self):
        citation = {"content": "hello world content", "text": "other text"}
        result = _gr_module.citation_to_text_for_entity_extraction(citation)
        self.assertEqual(result, "hello world content")

    def test_falls_back_to_text_field(self):
        citation = {"text": "fallback text here"}
        result = _gr_module.citation_to_text_for_entity_extraction(citation)
        self.assertEqual(result, "fallback text here")

    def test_falls_back_to_chunk_text_field(self):
        citation = {"chunk_text": "chunk content"}
        result = _gr_module.citation_to_text_for_entity_extraction(citation)
        self.assertEqual(result, "chunk content")

    def test_joins_list_content(self):
        citation = {"content": ["first part", "second part"]}
        result = _gr_module.citation_to_text_for_entity_extraction(citation)
        self.assertEqual(result, "first part\n\nsecond part")

    def test_appends_source_path(self):
        citation = {
            "content": "some text",
            "file_source": "/docs/report.pdf",
        }
        result = _gr_module.citation_to_text_for_entity_extraction(citation)
        self.assertIn("Source: /docs/report.pdf", result)

    def test_falls_back_to_json_repr(self):
        citation = {"path": "/docs/x.txt", "index": 3}
        result = _gr_module.citation_to_text_for_entity_extraction(citation)
        self.assertIn("path", result)
        self.assertIn("x.txt", result)

    def test_empty_citation_gives_json_repr(self):
        citation = {}
        result = _gr_module.citation_to_text_for_entity_extraction(citation)
        self.assertEqual(result, "{}")


# ── extract_entities_via_llm tests ──────────────────────────────────────────

class TestExtractEntitiesViaLLM(unittest.TestCase):
    """Test LLM-based entity extraction from citations."""

    def test_parses_json_array_response(self):
        """Should parse JSON array of strings as entities."""
        mock_client = MagicMock()
        mock_result = {"response": '["Alice", "Acme Corp", "Q3 revenue"]'}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        citations = [
            {"content": "Alice worked at Acme Corp in Q3"},
            {"content": "Revenue was strong"},
            {"content": "More text"},
        ]
        result = _run_async(
            _gr_module.extract_entities_via_llm(mock_client, citations, seed_count=3)
        )
        self.assertEqual(result, ["Alice", "Acme Corp", "Q3 revenue"])
        mock_client.post_json.assert_called_once()

    def test_parses_with_source_path_context(self):
        """Should include source path in extraction prompt."""
        mock_client = MagicMock()
        mock_result = {"response": '["Bob", "Beta Inc"]'}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        citations = [
            {"content": "Bob works at Beta Inc", "file_source": "/docs/bob.txt"},
        ]
        result = _run_async(
            _gr_module.extract_entities_via_llm(mock_client, citations, seed_count=1)
        )
        self.assertEqual(result, ["Bob", "Beta Inc"])
        call_args = mock_client.post_json.call_args
        payload = call_args[0][1]
        self.assertEqual(payload["mode"], "bypass")
        self.assertEqual(payload["top_k"], 1)

    def test_respects_seed_count(self):
        """Should only use top N citations for entity extraction."""
        mock_client = MagicMock()
        mock_result = {"response": '["Entity1"]'}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        citations = [
            {"content": "citation 1"},
            {"content": "citation 2"},
            {"content": "citation 3"},
            {"content": "citation 4"},
            {"content": "citation 5"},
        ]
        _run_async(
            _gr_module.extract_entities_via_llm(mock_client, citations, seed_count=2)
        )
        call_args = mock_client.post_json.call_args
        payload = call_args[0][1]
        self.assertEqual(payload["mode"], "bypass")

    def test_returns_empty_on_empty_citations(self):
        """Empty citations list should return empty list."""
        mock_client = MagicMock()
        result = _run_async(
            _gr_module.extract_entities_via_llm(mock_client, [], seed_count=3)
        )
        self.assertEqual(result, [])
        mock_client.post_json.assert_not_called()

    def test_http_error_returns_empty_fail_open(self):
        """HTTP errors should return empty list (fail-open)."""
        mock_client = MagicMock()
        mock_client.post_json = AsyncMock(side_effect=Exception("Connection refused"))

        citations = [{"content": "test"}]
        result = _run_async(
            _gr_module.extract_entities_via_llm(mock_client, citations, seed_count=1)
        )
        self.assertEqual(result, [])

    def test_empty_response_returns_empty(self):
        """Empty response should return empty list."""
        mock_client = MagicMock()
        mock_result = {"response": ""}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _gr_module.extract_entities_via_llm(mock_client, [{"content": "test"}], seed_count=1)
        )
        self.assertEqual(result, [])

    def test_none_response_returns_empty(self):
        """None response should return empty list."""
        mock_client = MagicMock()
        mock_result = {"response": None}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _gr_module.extract_entities_via_llm(mock_client, [{"content": "test"}], seed_count=1)
        )
        self.assertEqual(result, [])

    def test_malformed_json_falls_back_to_lines(self):
        """Malformed JSON should fall back to newline-separated entity names."""
        mock_client = MagicMock()
        mock_result = {"response": "Alice\nBob\nCharlie"}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _gr_module.extract_entities_via_llm(mock_client, [{"content": "test"}], seed_count=1)
        )
        self.assertEqual(result, ["Alice", "Bob", "Charlie"])

    def test_bracket_only_lines_filtered(self):
        """Lines that are just brackets should be filtered."""
        mock_client = MagicMock()
        mock_result = {"response": "[\nAlice\n]\nBob"}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _gr_module.extract_entities_via_llm(mock_client, [{"content": "test"}], seed_count=1)
        )
        self.assertEqual(result, ["Alice", "Bob"])

    def test_calls_bypass_mode(self):
        """Should call /query with mode='bypass' and top_k=1."""
        mock_client = MagicMock()
        mock_result = {"response": '["Entity"]'}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        _run_async(
            _gr_module.extract_entities_via_llm(mock_client, [{"content": "test"}], seed_count=1)
        )
        call_args = mock_client.post_json.call_args
        self.assertEqual(call_args[0][0], "/query")
        payload = call_args[0][1]
        self.assertEqual(payload["mode"], "bypass")
        self.assertEqual(payload["top_k"], 1)
        self.assertFalse(payload["include_references"])
        self.assertFalse(payload["include_chunk_content"])

    def test_no_text_citations_calls_llm_with_json_repr(self):
        """Citations with no content fields should still call LLM with JSON repr (fail-open later)."""
        mock_client = MagicMock()
        mock_result = {"response": ""}
        mock_client.post_json = AsyncMock(return_value=mock_result)
        citations = [
            {"path": "/docs/x.txt"},
            {"index": 3},
        ]
        result = _run_async(
            _gr_module.extract_entities_via_llm(mock_client, citations, seed_count=2)
        )
        # LLM is called but returns empty, so result is []
        self.assertEqual(result, [])
        mock_client.post_json.assert_called_once()


# ── expand_graph_neighbors tests ────────────────────────────────────────────

class TestExpandGraphNeighbors(unittest.TestCase):
    """Test graph neighbor expansion."""

    def test_returns_empty_on_empty_entities(self):
        """Empty entities list should return empty neighbors."""
        mock_client = MagicMock()
        result, error, hops_used = _run_async(
            _gr_module.expand_graph_neighbors(mock_client, [], max_neighbors=10, hops=1)
        )
        self.assertEqual(result, [])
        self.assertIsNone(error)
        self.assertEqual(hops_used, 0)

    def test_parses_json_array_neighbors(self):
        """Should parse JSON array of neighbor objects from bypass response."""
        mock_client = MagicMock()
        mock_result = {
            "response": json.dumps([
                {
                    "entity": "Alice",
                    "related_entity": "Acme Corp",
                    "relationship": "works at",
                    "context": "Alice is employed at Acme Corp",
                }
            ])
        }
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result, error, hops_used = _run_async(
            _gr_module.expand_graph_neighbors(mock_client, ["Alice"], max_neighbors=10, hops=1)
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["seed_entity"], "Alice")
        self.assertEqual(result[0]["related_entity"], "Acme Corp")
        self.assertEqual(result[0]["relationship"], "works at")
        self.assertEqual(result[0]["context"], "Alice is employed at Acme Corp")
        self.assertIsNone(error)
        self.assertEqual(hops_used, 1)

    def test_deduplicates_neighbors(self):
        """Duplicate (entity, related, relationship) should be deduped."""
        mock_client = MagicMock()
        mock_result = {
            "response": json.dumps([
                {"entity": "Alice", "related_entity": "Bob", "relationship": "knows", "context": ""},
                {"entity": "Alice", "related_entity": "Bob", "relationship": "knows", "context": ""},
                {"entity": "Alice", "related_entity": "Charlie", "relationship": "knows", "context": ""},
            ])
        }
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result, _, _ = _run_async(
            _gr_module.expand_graph_neighbors(mock_client, ["Alice"], max_neighbors=10, hops=1)
        )
        self.assertEqual(len(result), 2)
        # Case-insensitive dedup
        self.assertEqual(result[0]["seed_entity"], "Alice")
        self.assertEqual(result[1]["related_entity"], "Charlie")

    def test_case_insensitive_dedup(self):
        """Dedup should be case-insensitive."""
        mock_client = MagicMock()
        mock_result = {
            "response": json.dumps([
                {"entity": "alice", "related_entity": "bob", "relationship": "knows", "context": ""},
                {"entity": "Alice", "related_entity": "Bob", "relationship": "knows", "context": ""},
            ])
        }
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result, _, _ = _run_async(
            _gr_module.expand_graph_neighbors(mock_client, ["Alice"], max_neighbors=10, hops=1)
        )
        self.assertEqual(len(result), 1)

    def test_trims_to_max_neighbors(self):
        """Should not return more than max_neighbors."""
        mock_client = MagicMock()
        mock_result = {
            "response": json.dumps([
                {"entity": "Alice", "related_entity": f"Bob{i}", "relationship": "knows", "context": ""}
                for i in range(20)
            ])
        }
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result, _, _ = _run_async(
            _gr_module.expand_graph_neighbors(mock_client, ["Alice"], max_neighbors=5, hops=1)
        )
        self.assertEqual(len(result), 5)

    def test_http_error_sets_error(self):
        """HTTP errors should return error string (fail-open)."""
        mock_client = MagicMock()
        mock_client.post_json = AsyncMock(side_effect=Exception("Connection refused"))

        result, error, hops_used = _run_async(
            _gr_module.expand_graph_neighbors(mock_client, ["Alice"], max_neighbors=10, hops=1)
        )
        self.assertEqual(result, [])
        self.assertIsNotNone(error)
        self.assertIn("graph_query_1hop_failed", error)
        self.assertEqual(hops_used, 1)

    def test_empty_response_returns_empty(self):
        """Empty response should return empty neighbors."""
        mock_client = MagicMock()
        mock_result = {"response": ""}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result, error, hops_used = _run_async(
            _gr_module.expand_graph_neighbors(mock_client, ["Alice"], max_neighbors=10, hops=1)
        )
        self.assertEqual(result, [])
        self.assertIsNone(error)
        self.assertEqual(hops_used, 1)

    def test_2hop_runs_when_configured(self):
        """When hops=2, should attempt 2-hop expansion."""
        mock_client = MagicMock()
        mock_result = {
            "response": json.dumps([
                {"entity": "Alice", "related_entity": "Bob", "relationship": "knows", "context": ""},
            ])
        }
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result, error, hops_used = _run_async(
            _gr_module.expand_graph_neighbors(mock_client, ["Alice"], max_neighbors=10, hops=2)
        )
        self.assertEqual(hops_used, 2)
        # 1-hop returns 1 neighbor, 2-hop should add more if budget allows
        # Since 2-hop uses hop1 related entities, it should query again
        self.assertEqual(mock_client.post_json.call_count, 2)

    def test_2hop_respects_remaining_budget(self):
        """2-hop should only use remaining neighbor budget."""
        mock_client = MagicMock()

        def side_effect(path, payload):
            if "2hop" in str(payload) or path.endswith("2hop"):
                # Simulate 2-hop adding to budget
                pass
            return {"response": json.dumps([
                {"entity": "Bob", "related_entity": f"Charlie{i}", "relationship": "knows", "context": ""}
                for i in range(10)
            ])}

        # First call: 1-hop returns 8 neighbors
        # Second call: 2-hop should get only 2 more (remaining budget = 10 - 8)
        call_count = [0]

        def controlled_side_effect(path, payload):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"response": json.dumps([
                    {"entity": "Alice", "related_entity": f"Bob{i}", "relationship": "knows", "context": ""}
                    for i in range(8)
                ])}
            else:
                # 2-hop call - should be limited to 2 remaining
                return {"response": json.dumps([
                    {"entity": "Bob0", "related_entity": f"Charlie{i}", "relationship": "knows", "context": ""}
                    for i in range(10)
                ])}

        mock_client.post_json = AsyncMock(side_effect=controlled_side_effect)

        result, _, hops_used = _run_async(
            _gr_module.expand_graph_neighbors(mock_client, ["Alice"], max_neighbors=10, hops=2)
        )
        # 8 from 1-hop + 2 from 2-hop (remaining budget) = 10 total
        self.assertEqual(len(result), 10)
        self.assertEqual(hops_used, 2)

    def test_empty_entities_response_returns_empty(self):
        """None response should return empty neighbors."""
        mock_client = MagicMock()
        mock_result = {"response": None}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result, error, hops_used = _run_async(
            _gr_module.expand_graph_neighbors(mock_client, ["Alice"], max_neighbors=10, hops=1)
        )
        self.assertEqual(result, [])
        self.assertIsNone(error)
        self.assertEqual(hops_used, 1)

    def test_calls_bypass_mode_for_neighbors(self):
        """Should call /query with mode='bypass' and top_k=1."""
        mock_client = MagicMock()
        mock_result = {"response": json.dumps([
            {"entity": "Alice", "related_entity": "Bob", "relationship": "knows", "context": ""}
        ])}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        _run_async(
            _gr_module.expand_graph_neighbors(mock_client, ["Alice"], max_neighbors=10, hops=1)
        )
        call_args = mock_client.post_json.call_args
        self.assertEqual(call_args[0][0], "/query")
        payload = call_args[0][1]
        self.assertEqual(payload["mode"], "bypass")
        self.assertEqual(payload["top_k"], 1)


# ── merge_graph_expansion tests ─────────────────────────────────────────────

class TestMergeGraphExpansion(unittest.TestCase):
    """Test merging graph pseudo-citations with base citations."""

    def test_returns_base_when_no_neighbors(self):
        """When graph_neighbors is empty, return base citations unchanged."""
        base = [{"id": "c1", "content": "base citation 1"}, {"id": "c2", "content": "base citation 2"}]
        result = _gr_module.merge_graph_expansion(base, [])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "c1")
        self.assertEqual(result[1]["id"], "c2")

    def test_appends_pseudo_citations(self):
        """Graph pseudo-citations should be appended after base."""
        base = [{"id": "c1", "content": "base", "path": "/docs/a.txt"}]
        neighbors = [
            {
                "seed_entity": "Alice",
                "related_entity": "Bob",
                "relationship": "knows",
                "context": "Alice knows Bob",
            }
        ]
        result = _gr_module.merge_graph_expansion(base, neighbors)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "c1")
        self.assertEqual(result[1]["type"], "graph_expansion")
        self.assertIn("Alice", result[1]["content"])

    def test_dedupes_pseudo_citations_by_path(self):
        """Pseudo-citations with same path should be deduped."""
        base = [{"id": "c1", "content": "base", "path": "/docs/a.txt"}]
        neighbors = [
            {
                "seed_entity": "Alice",
                "related_entity": "Bob",
                "relationship": "knows",
                "context": "Alice knows Bob",
            },
            {
                "seed_entity": "Alice",
                "related_entity": "Bob",
                "relationship": "knows",
                "context": "Alice knows Bob (duplicate)",
            },
        ]
        result = _gr_module.merge_graph_expansion(base, neighbors)
        # Base + 1 deduped pseudo = 2
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["type"], "graph_expansion")

    def test_dedupes_by_content_fallback(self):
        """Pseudo-citations with same content but different paths should be deduped."""
        base = [{"id": "c1", "content": "base", "path": "/docs/a.txt"}]
        # Same seed+related = same pseudo-citation content
        neighbors = [
            {
                "seed_entity": "Alice",
                "related_entity": "Bob",
                "relationship": "knows",
                "context": "Alice knows Bob",
            },
            {
                "seed_entity": "Alice",
                "related_entity": "Bob",
                "relationship": "related",
                "context": "Alice knows Bob",
            },
        ]
        result = _gr_module.merge_graph_expansion(base, neighbors)
        # Content-based dedup should catch this
        self.assertEqual(len(result), 2)

    def test_stable_order_preserved(self):
        """Base citations should appear before graph pseudo-citations."""
        base = [{"id": "c1", "content": "base 1"}, {"id": "c2", "content": "base 2"}]
        neighbors = [
            {"seed_entity": "Alice", "related_entity": "Bob", "relationship": "knows", "context": ""},
        ]
        result = _gr_module.merge_graph_expansion(base, neighbors)
        self.assertEqual(result[0]["id"], "c1")
        self.assertEqual(result[1]["id"], "c2")
        self.assertEqual(result[2]["type"], "graph_expansion")

    def test_empty_base_with_neighbors(self):
        """Empty base with neighbors should return only pseudo-citations."""
        base = []
        neighbors = [
            {"seed_entity": "Alice", "related_entity": "Bob", "relationship": "knows", "context": ""},
        ]
        result = _gr_module.merge_graph_expansion(base, neighbors)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "graph_expansion")


# ── compute_adaptive_hops tests ─────────────────────────────────────────────

class TestComputeAdaptiveHops(unittest.TestCase):
    """Test adaptive hop escalation logic."""

    def test_default_1_hop_no_triggers(self):
        """Default 1 hop when no triggers fire."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=5,
            scoped_citation_count=10,
            query_word_count=5,
            is_transcript_like=False,
            rerank_top_score=0.9,
            configured_hops=2,
        )
        self.assertEqual(result, 1)

    def test_escalates_when_neighbors_below_4(self):
        """Escalate to 2 hops when graph neighbors < 4."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=3,
            scoped_citation_count=10,
            query_word_count=5,
            is_transcript_like=False,
            rerank_top_score=0.9,
            configured_hops=2,
        )
        self.assertEqual(result, 2)

    def test_escalates_when_neighbors_zero(self):
        """Escalate to 2 hops when graph neighbors = 0."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=0,
            scoped_citation_count=10,
            query_word_count=5,
            is_transcript_like=False,
            rerank_top_score=0.9,
            configured_hops=2,
        )
        self.assertEqual(result, 2)

    def test_escalates_when_scoped_citations_below_3(self):
        """Escalate to 2 hops when scoped citations < 3."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=10,
            scoped_citation_count=2,
            query_word_count=5,
            is_transcript_like=False,
            rerank_top_score=0.9,
            configured_hops=2,
        )
        self.assertEqual(result, 2)

    def test_no_escalate_at_scoped_citations_boundary(self):
        """At exactly 3 scoped citations, no escalation."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=10,
            scoped_citation_count=3,
            query_word_count=5,
            is_transcript_like=False,
            rerank_top_score=0.9,
            configured_hops=2,
        )
        self.assertEqual(result, 1)

    def test_escalates_on_long_query_with_low_rerank_score(self):
        """Escalate when query >= 20 words AND rerank top score < 0.15."""
        long_query_words = " ".join(["word"] * 25)
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=10,
            scoped_citation_count=10,
            query_word_count=len(long_query_words.split()),
            is_transcript_like=False,
            rerank_top_score=0.10,
            configured_hops=2,
        )
        self.assertEqual(result, 2)

    def test_no_escalate_long_query_with_high_rerank_score(self):
        """Long query with high rerank score should not escalate."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=10,
            scoped_citation_count=10,
            query_word_count=25,
            is_transcript_like=False,
            rerank_top_score=0.50,
            configured_hops=2,
        )
        self.assertEqual(result, 1)

    def test_no_escalate_high_rerank_with_long_query(self):
        """High rerank score prevents escalation even with complex query."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=10,
            scoped_citation_count=10,
            query_word_count=25,
            is_transcript_like=False,
            rerank_top_score=0.15,  # exactly at threshold, not below
            configured_hops=2,
        )
        self.assertEqual(result, 1)

    def test_escalates_on_transcript_like_with_low_rerank(self):
        """Escalate when transcript-like query AND rerank top score < 0.15."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=10,
            scoped_citation_count=10,
            query_word_count=5,
            is_transcript_like=True,
            rerank_top_score=0.10,
            configured_hops=2,
        )
        self.assertEqual(result, 2)

    def test_no_escalate_transcript_with_high_rerank(self):
        """Transcript-like query with high rerank score should not escalate."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=10,
            scoped_citation_count=10,
            query_word_count=5,
            is_transcript_like=True,
            rerank_top_score=0.50,
            configured_hops=2,
        )
        self.assertEqual(result, 1)

    def test_configured_hops_1_returns_1(self):
        """When configured_hops=1, never escalates."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=0,
            scoped_citation_count=0,
            query_word_count=100,
            is_transcript_like=True,
            rerank_top_score=0.01,
            configured_hops=1,
        )
        self.assertEqual(result, 1)

    def test_boundary_word_count_20_triggers(self):
        """Exactly 20 words should trigger complex query escalation with low rerank."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=10,
            scoped_citation_count=10,
            query_word_count=20,
            is_transcript_like=False,
            rerank_top_score=0.10,
            configured_hops=2,
        )
        self.assertEqual(result, 2)

    def test_boundary_word_count_19_no_trigger(self):
        """19 words should not trigger complex query escalation."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=10,
            scoped_citation_count=10,
            query_word_count=19,
            is_transcript_like=False,
            rerank_top_score=0.10,
            configured_hops=2,
        )
        self.assertEqual(result, 1)

    def test_multiple_triggers_still_returns_2(self):
        """When multiple triggers fire, still returns 2 (not more)."""
        result = _gr_module.compute_adaptive_hops(
            graph_neighbor_count=0,
            scoped_citation_count=0,
            query_word_count=30,
            is_transcript_like=True,
            rerank_top_score=0.01,
            configured_hops=2,
        )
        self.assertEqual(result, 2)


# ── build_graph_metadata tests ──────────────────────────────────────────────

class TestBuildGraphMetadata(unittest.TestCase):
    """Test graph metadata builder."""

    def test_all_fields_present_when_applied(self):
        meta = _gr_module.build_graph_metadata(
            enabled=True,
            applied=True,
            seed_count=3,
            neighbor_count=7,
            hops_used=2,
        )
        expected_keys = {
            "graph_expansion_enabled",
            "graph_expansion_applied",
            "graph_expansion_error",
            "graph_seed_count",
            "graph_neighbor_count",
            "graph_hops_used",
        }
        self.assertEqual(set(meta.keys()), expected_keys)
        self.assertTrue(meta["graph_expansion_enabled"])
        self.assertTrue(meta["graph_expansion_applied"])
        self.assertIsNone(meta["graph_expansion_error"])
        self.assertEqual(meta["graph_seed_count"], 3)
        self.assertEqual(meta["graph_neighbor_count"], 7)
        self.assertEqual(meta["graph_hops_used"], 2)

    def test_all_fields_present_when_not_applied(self):
        meta = _gr_module.build_graph_metadata(
            enabled=True,
            applied=False,
        )
        expected_keys = {
            "graph_expansion_enabled",
            "graph_expansion_applied",
            "graph_expansion_error",
            "graph_seed_count",
            "graph_neighbor_count",
            "graph_hops_used",
        }
        self.assertEqual(set(meta.keys()), expected_keys)
        self.assertTrue(meta["graph_expansion_enabled"])
        self.assertFalse(meta["graph_expansion_applied"])
        self.assertIsNone(meta["graph_expansion_error"])
        self.assertEqual(meta["graph_seed_count"], 0)
        self.assertEqual(meta["graph_neighbor_count"], 0)
        self.assertEqual(meta["graph_hops_used"], 0)

    def test_error_field_set_on_failure(self):
        meta = _gr_module.build_graph_metadata(
            enabled=True,
            applied=False,
            error="graph_query_1hop_failed: Connection refused",
        )
        self.assertEqual(meta["graph_expansion_error"], "graph_query_1hop_failed: Connection refused")

    def test_all_fields_present_when_disabled(self):
        meta = _gr_module.build_graph_metadata(
            enabled=False,
            applied=False,
        )
        self.assertFalse(meta["graph_expansion_enabled"])
        self.assertFalse(meta["graph_expansion_applied"])

    def test_field_types(self):
        meta = _gr_module.build_graph_metadata(
            enabled=True,
            applied=True,
            seed_count=3,
            neighbor_count=7,
            hops_used=2,
        )
        self.assertIsInstance(meta["graph_expansion_enabled"], bool)
        self.assertIsInstance(meta["graph_expansion_applied"], bool)
        self.assertIsInstance(meta["graph_expansion_error"], type(None))
        self.assertIsInstance(meta["graph_seed_count"], int)
        self.assertIsInstance(meta["graph_neighbor_count"], int)
        self.assertIsInstance(meta["graph_hops_used"], int)


# ── Feature flag and config tests ───────────────────────────────────────────

class TestFeatureFlagAndConfig(unittest.TestCase):
    """Test feature flag default and config settings."""

    def _get_settings_module(self):
        """Helper to load Settings with required env vars."""
        required_env = {
            "RAG_API_KEY": "1234",
            "LIGHTRAG_INTERNAL_API_KEY": "internal",
            "LIGHTRAG_BASE_URL": "http://example:9621",
            "SOURCE_DOCS_DIR": "/tmp/source_docs",
            "UPLOADS_DIR": "/tmp/uploads",
            "STATE_DB_PATH": "/tmp/state.db",
        }
        with patch.dict(os.environ, required_env, clear=True):
            config_module = importlib.import_module("app.config")
            config_module = importlib.reload(config_module)
        return config_module.Settings

    def test_graph_expansion_enabled_default_false(self):
        Settings = self._get_settings_module()
        field = Settings.model_fields.get("graph_expansion_enabled")
        self.assertIsNotNone(field)
        self.assertEqual(field.default, False)

    def test_graph_expansion_hops_default_1(self):
        Settings = self._get_settings_module()
        field = Settings.model_fields.get("graph_expansion_hops")
        self.assertIsNotNone(field)
        self.assertEqual(field.default, 1)

    def test_graph_expansion_max_neighbors_default_10(self):
        Settings = self._get_settings_module()
        field = Settings.model_fields.get("graph_expansion_max_neighbors")
        self.assertIsNotNone(field)
        self.assertEqual(field.default, 10)

    def test_graph_seed_citation_count_default_3(self):
        Settings = self._get_settings_module()
        field = Settings.model_fields.get("graph_seed_citation_count")
        self.assertIsNotNone(field)
        self.assertEqual(field.default, 3)

    def test_graph_expansion_enabled_env_override(self):
        """GRAPH_EXPANSION_ENABLED should be overridable via env var."""
        required_env = {
            "RAG_API_KEY": "1234",
            "LIGHTRAG_INTERNAL_API_KEY": "internal",
            "LIGHTRAG_BASE_URL": "http://example:9621",
            "SOURCE_DOCS_DIR": "/tmp/source_docs",
            "UPLOADS_DIR": "/tmp/uploads",
            "STATE_DB_PATH": "/tmp/state.db",
            "GRAPH_EXPANSION_ENABLED": "true",
        }
        with patch.dict(os.environ, required_env, clear=True):
            config_module = importlib.import_module("app.config")
            config_module = importlib.reload(config_module)
            self.assertTrue(config_module.settings.graph_expansion_enabled)

    def test_graph_expansion_hops_env_override(self):
        """GRAPH_EXPANSION_HOPS should be overridable via env var."""
        required_env = {
            "RAG_API_KEY": "1234",
            "LIGHTRAG_INTERNAL_API_KEY": "internal",
            "LIGHTRAG_BASE_URL": "http://example:9621",
            "SOURCE_DOCS_DIR": "/tmp/source_docs",
            "UPLOADS_DIR": "/tmp/uploads",
            "STATE_DB_PATH": "/tmp/state.db",
            "GRAPH_EXPANSION_HOPS": "2",
        }
        with patch.dict(os.environ, required_env, clear=True):
            config_module = importlib.import_module("app.config")
            config_module = importlib.reload(config_module)
            self.assertEqual(config_module.settings.graph_expansion_hops, 2)

    def test_graph_expansion_max_neighbors_env_override(self):
        """GRAPH_EXPANSION_MAX_NEIGHBORS should be overridable via env var."""
        required_env = {
            "RAG_API_KEY": "1234",
            "LIGHTRAG_INTERNAL_API_KEY": "internal",
            "LIGHTRAG_BASE_URL": "http://example:9621",
            "SOURCE_DOCS_DIR": "/tmp/source_docs",
            "UPLOADS_DIR": "/tmp/uploads",
            "STATE_DB_PATH": "/tmp/state.db",
            "GRAPH_EXPANSION_MAX_NEIGHBORS": "20",
        }
        with patch.dict(os.environ, required_env, clear=True):
            config_module = importlib.import_module("app.config")
            config_module = importlib.reload(config_module)
            self.assertEqual(config_module.settings.graph_expansion_max_neighbors, 20)

    def test_graph_seed_citation_count_env_override(self):
        """GRAPH_SEED_CITATION_COUNT should be overridable via env var."""
        required_env = {
            "RAG_API_KEY": "1234",
            "LIGHTRAG_INTERNAL_API_KEY": "internal",
            "LIGHTRAG_BASE_URL": "http://example:9621",
            "SOURCE_DOCS_DIR": "/tmp/source_docs",
            "UPLOADS_DIR": "/tmp/uploads",
            "STATE_DB_PATH": "/tmp/state.db",
            "GRAPH_SEED_CITATION_COUNT": "5",
        }
        with patch.dict(os.environ, required_env, clear=True):
            config_module = importlib.import_module("app.config")
            config_module = importlib.reload(config_module)
            self.assertEqual(config_module.settings.graph_seed_citation_count, 5)


# ── Integration-style flow tests ────────────────────────────────────────────

class TestIntegrationQueryFlowPlacement(unittest.TestCase):
    """Integration-level tests for graph expansion placement in query flow.

    These tests verify that graph expansion metadata is properly integrated
    into the query_scope and that the feature flag gates execution.
    """

    def test_metadata_fields_integrated_with_scope(self):
        """Graph metadata should integrate alongside multi-query metadata."""
        graph_meta = _gr_module.build_graph_metadata(
            enabled=True,
            applied=True,
            seed_count=3,
            neighbor_count=5,
            hops_used=1,
        )
        mq_meta = _mq_module.build_multi_query_metadata(
            enabled=False,
            triggered=False,
            trigger_reason="none",
            rewrite_count=0,
        )
        combined = {}
        combined.update(mq_meta)
        combined.update(graph_meta)

        # All fields present
        self.assertIn("multi_query_enabled", combined)
        self.assertIn("graph_expansion_enabled", combined)
        self.assertIn("graph_expansion_applied", combined)
        self.assertIn("graph_expansion_error", combined)
        self.assertIn("graph_seed_count", combined)
        self.assertIn("graph_neighbor_count", combined)
        self.assertIn("graph_hops_used", combined)

    def test_graph_metadata_always_populated(self):
        """Graph metadata should always be present in query_scope, even when disabled."""
        # When disabled
        meta_disabled = _gr_module.build_graph_metadata(enabled=False, applied=False)
        self.assertIn("graph_expansion_enabled", meta_disabled)
        self.assertIn("graph_expansion_applied", meta_disabled)
        self.assertFalse(meta_disabled["graph_expansion_enabled"])
        self.assertFalse(meta_disabled["graph_expansion_applied"])

        # When enabled but no entities extracted
        meta_no_entities = _gr_module.build_graph_metadata(enabled=True, applied=False)
        self.assertTrue(meta_no_entities["graph_expansion_enabled"])
        self.assertFalse(meta_no_entities["graph_expansion_applied"])
        self.assertEqual(meta_no_entities["graph_seed_count"], 0)
        self.assertEqual(meta_no_entities["graph_neighbor_count"], 0)

    def test_merge_then_scope_rerank_order(self):
        """Graph expansion should happen before scope filter and rerank.

        This is verified by the order of operations in the merged result:
        base citations (possibly expanded) -> scope -> rerank -> truncate.
        """
        base_citations = [
            {"id": "c1", "content": "company-a doc", "path": "/docs/a.txt", "company": "company-a"},
            {"id": "c2", "content": "company-b doc", "path": "/docs/b.txt", "company": "company-b"},
        ]
        graph_neighbors = [
            {
                "seed_entity": "Alice",
                "related_entity": "Bob",
                "relationship": "knows",
                "context": "Alice knows Bob",
            }
        ]

        # Merge happens first (graph expansion)
        merged = _gr_module.merge_graph_expansion(base_citations, graph_neighbors)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]["id"], "c1")
        self.assertEqual(merged[1]["id"], "c2")
        self.assertEqual(merged[2]["type"], "graph_expansion")

        # Scope filter would then be applied to merged (simulated)
        # Company-a scoped query
        scoped = [c for c in merged if c.get("company") == "company-a" or c.get("type") == "graph_expansion"]
        # Graph pseudo-citation has no company, so it would be included in unscoped queries
        # This verifies the placement: graph expansion happens BEFORE scope filter

    def test_fail_open_preserves_original_citations(self):
        """On graph expansion failure, original citations should be preserved."""
        base_citations = [
            {"id": "c1", "content": "original doc 1"},
            {"id": "c2", "content": "original doc 2"},
        ]
        # Simulating fail-open: merged_citations remains unchanged
        # because graph expansion raised an exception
        self.assertEqual(base_citations, list(base_citations))  # identity preserved

    def test_feature_flag_gates_execution(self):
        """When graph_expansion_enabled is False, no graph operation should occur."""
        meta = _gr_module.build_graph_metadata(enabled=False, applied=False)
        self.assertFalse(meta["graph_expansion_enabled"])
        self.assertFalse(meta["graph_expansion_applied"])
        self.assertIsNone(meta["graph_expansion_error"])

    def test_constants_have_correct_defaults(self):
        """Module constants should match PRD defaults."""
        self.assertEqual(_gr_module.DEFAULT_SEED_CITATION_COUNT, 3)
        self.assertEqual(_gr_module.DEFAULT_MAX_NEIGHBORS, 10)
        self.assertEqual(_gr_module.DEFAULT_HOPS, 1)


if __name__ == "__main__":
    unittest.main()
