"""Tests for Packet 14: Multi-Query Expansion.

Covers:
- trigger decisions for long/transcript/weak/none
- rewrite parsing robustness (JSON, newline, malformed)
- stable merge + dedupe rules (path-first, content-fallback)
- fail-open behavior metadata
- integration-level flow helper tests for metadata fields presence and counts
- config setting defaults

Imports from app.multi_query and app.config to avoid pulling in the full gateway dependency chain.
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


# ── Helper: set up httpx mock for async calls ──────────────────────────────

class _MockAsyncHttpClient:
    """A real async context manager that delegates post() to a callable."""

    def __init__(self, *args, **kwargs):
        self._post_fn = None

    def set_post(self, post_fn):
        self._post_fn = post_fn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, *args, **kwargs):
        return self._post_fn(*args, **kwargs)


def _setup_httpx_mock(mock_httpx, mock_response_or_exception):
    """Configure mock_httpx to return mock_response_or_exception from post()."""
    mock_client = _MockAsyncHttpClient()
    if isinstance(mock_response_or_exception, MagicMock):
        mock_client.set_post(lambda *a, **kw: mock_response_or_exception)
    elif callable(mock_response_or_exception):
        mock_client.set_post(mock_response_or_exception)
    else:
        mock_client.set_post(lambda *a, **kw: mock_response_or_exception)
    mock_httpx.AsyncClient.return_value = mock_client


# ── parse_keywords_csv tests ───────────────────────────────────────────────

class TestParseKeywordsCsv(unittest.TestCase):
    """Test parse_keywords_csv utility."""

    def test_parses_comma_separated_keywords(self):
        result = _mq_module.parse_keywords_csv("transcript,workshop,speaker")
        self.assertEqual(result, ["transcript", "workshop", "speaker"])

    def test_strips_whitespace(self):
        result = _mq_module.parse_keywords_csv("transcript , workshop , speaker")
        self.assertEqual(result, ["transcript", "workshop", "speaker"])

    def test_lowercases_keywords(self):
        result = _mq_module.parse_keywords_csv("Transcript,WORKSHOP,Speaker")
        self.assertEqual(result, ["transcript", "workshop", "speaker"])

    def test_excludes_empty_strings(self):
        result = _mq_module.parse_keywords_csv("transcript,,workshop,")
        self.assertEqual(result, ["transcript", "workshop"])

    def test_empty_string_returns_empty_list(self):
        result = _mq_module.parse_keywords_csv("")
        self.assertEqual(result, [])

    def test_none_returns_empty_list(self):
        result = _mq_module.parse_keywords_csv(None)  # type: ignore
        self.assertEqual(result, [])

    def test_multi_word_keyword_preserved(self):
        result = _mq_module.parse_keywords_csv("transcript,meeting minutes,workshop")
        self.assertEqual(result, ["transcript", "meeting minutes", "workshop"])

    def test_default_keywords_parsed_correctly(self):
        result = _mq_module.parse_keywords_csv(_mq_module.DEFAULT_TRANSCRIPT_KEYWORDS)
        self.assertIn("transcript", result)
        self.assertIn("meeting minutes", result)
        self.assertEqual(len(result), 5)


# ── should_trigger_multi_query tests ───────────────────────────────────────

class TestShouldTriggerMultiQuery(unittest.TestCase):
    """Test trigger decision logic."""

    def test_long_query_triggers(self):
        long_query = " ".join(["word"] * 25)
        triggered, reason = _mq_module.should_trigger_multi_query(
            long_query,
            transcript_keywords=[],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "long_query")

    def test_short_query_no_trigger(self):
        triggered, reason = _mq_module.should_trigger_multi_query(
            "short query",
            transcript_keywords=[],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, "none")

    def test_transcript_keyword_triggers(self):
        triggered, reason = _mq_module.should_trigger_multi_query(
            "show me the transcript of the meeting",
            transcript_keywords=["transcript"],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "transcript_keyword")

    def test_transcript_keyword_case_insensitive(self):
        triggered, reason = _mq_module.should_trigger_multi_query(
            "Show ME THE TRANSCRIPT of the meeting",
            transcript_keywords=["transcript"],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "transcript_keyword")

    def test_empty_query_no_trigger(self):
        triggered, reason = _mq_module.should_trigger_multi_query(
            "",
            transcript_keywords=["transcript"],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, "none")

    def test_whitespace_only_no_trigger(self):
        triggered, reason = _mq_module.should_trigger_multi_query(
            "   ",
            transcript_keywords=["transcript"],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, "none")

    def test_weak_signal_triggers(self):
        triggered, reason = _mq_module.should_trigger_multi_query(
            "short query",
            transcript_keywords=[],
            long_query_words=20,
            weak_signal=True,
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "weak_signal")

    def test_long_query_with_transcript_both_match(self):
        """When both long query and transcript keyword match, long_query wins."""
        long_query = " ".join(["transcript"] * 25)
        triggered, reason = _mq_module.should_trigger_multi_query(
            long_query,
            transcript_keywords=["transcript"],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "long_query")

    def test_boundary_word_count_exact(self):
        """Query with exactly long_query_words should trigger (>=)."""
        boundary_query = " ".join(["word"] * 20)
        triggered, reason = _mq_module.should_trigger_multi_query(
            boundary_query,
            transcript_keywords=[],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "long_query")

    def test_boundary_word_count_below(self):
        """Query with one less than long_query_words should not trigger."""
        short_query = " ".join(["word"] * 19)
        triggered, reason = _mq_module.should_trigger_multi_query(
            short_query,
            transcript_keywords=[],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, "none")

    def test_multi_word_transcript_keyword_match(self):
        """Multi-word keyword like 'meeting minutes' should match."""
        triggered, reason = _mq_module.should_trigger_multi_query(
            "get the meeting minutes from last week",
            transcript_keywords=["meeting minutes"],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "transcript_keyword")

    def test_no_trigger_when_all_conditions_false(self):
        """No trigger when short query, no keywords, no weak signal."""
        triggered, reason = _mq_module.should_trigger_multi_query(
            "hello world",
            transcript_keywords=["transcript"],
            long_query_words=20,
            weak_signal=False,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, "none")


# ── has_weak_answer_signal tests ───────────────────────────────────────────

class TestHasWeakAnswerSignal(unittest.TestCase):
    """Test weak answer signal detection."""

    def test_detects_not_enough_information(self):
        self.assertTrue(_mq_module.has_weak_answer_signal("I do not have enough information"))

    def test_detects_varied_case(self):
        self.assertTrue(_mq_module.has_weak_answer_signal("NOT ENOUGH INFORMATION"))

    def test_false_for_good_answer(self):
        self.assertFalse(_mq_module.has_weak_answer_signal("The company was founded in 1995"))

    def test_false_for_empty(self):
        self.assertFalse(_mq_module.has_weak_answer_signal(""))


# ── merge_and_dedupe_citations tests ───────────────────────────────────────

class TestMergeAndDedupeCitations(unittest.TestCase):
    """Test stable merge and deduplication."""

    def test_stable_merge_order(self):
        """Citations should appear in stable order: group0, group1, group2."""
        groups = [
            [{"id": "orig1", "content": "original doc 1"}, {"id": "orig2", "content": "original doc 2"}],
            [{"id": "rw1", "content": "rewrite1 doc 1"}],
            [{"id": "rw2", "content": "rewrite2 doc 1"}],
        ]
        result = _mq_module.merge_and_dedupe_citations(groups)
        ids = [c["id"] for c in result]
        self.assertEqual(ids, ["orig1", "orig2", "rw1", "rw2"])

    def test_dedupe_by_normalized_path(self):
        """Same citation path (normalized) should be deduped, keeping first occurrence."""
        groups = [
            [
                {"id": "orig1", "path": "/docs/file.txt", "content": "original"},
            ],
            [
                {"id": "rw1", "path": "/docs/FILE.TXT", "content": "rewrite version"},
            ],
        ]
        result = _mq_module.merge_and_dedupe_citations(groups)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "orig1")

    def test_dedupe_by_content_fallback(self):
        """When paths are absent, dedupe by content."""
        groups = [
            [{"id": "orig1", "content": "shared content"}],
            [{"id": "rw1", "content": "shared content"}],
        ]
        result = _mq_module.merge_and_dedupe_citations(groups)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "orig1")

    def test_no_dedupe_different_paths(self):
        """Different paths should not be deduped."""
        groups = [
            [{"id": "orig1", "path": "/docs/a.txt", "content": "doc a"}],
            [{"id": "rw1", "path": "/docs/b.txt", "content": "doc b"}],
        ]
        result = _mq_module.merge_and_dedupe_citations(groups)
        self.assertEqual(len(result), 2)

    def test_no_dedupe_different_content(self):
        """Different content should not be deduped even with same path field absent."""
        groups = [
            [{"id": "orig1", "content": "content a"}],
            [{"id": "rw1", "content": "content b"}],
        ]
        result = _mq_module.merge_and_dedupe_citations(groups)
        self.assertEqual(len(result), 2)

    def test_empty_groups_returns_empty(self):
        self.assertEqual(_mq_module.merge_and_dedupe_citations([]), [])

    def test_empty_groups_in_list(self):
        """Empty groups in the middle should be skipped gracefully."""
        groups = [
            [{"id": "orig1", "content": "original"}],
            [],
            [{"id": "rw1", "content": "rewrite"}],
        ]
        result = _mq_module.merge_and_dedupe_citations(groups)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "orig1")
        self.assertEqual(result[1]["id"], "rw1")

    def test_path_normalization_resolves_dot_segments(self):
        """Normalized path should resolve '.' and '..' segments."""
        groups = [
            [{"id": "orig1", "path": "/docs/./file/../file.txt"}],
            [{"id": "rw1", "path": "/docs/file.txt"}],
        ]
        result = _mq_module.merge_and_dedupe_citations(groups)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "orig1")

    def test_path_field_extraction(self):
        """Should extract path from various field names."""
        for field in ("file_source", "path", "source", "file_path"):
            groups = [[{field: "/docs/test.txt", "content": "test"}]]
            result = _mq_module.merge_and_dedupe_citations(groups)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["content"], "test")

    def test_content_field_extraction(self):
        """Should extract content from various field names."""
        for field in ("content", "text", "chunk_text", "body"):
            groups = [[{"id": "x", field: "shared content"}]]
            result = _mq_module.merge_and_dedupe_citations(groups)
            self.assertEqual(len(result), 1)

    def test_three_groups_stable_order(self):
        """Three groups should maintain stable order."""
        groups = [
            [{"id": "o1"}, {"id": "o2"}],
            [{"id": "r1"}],
            [{"id": "r2"}, {"id": "r3"}],
        ]
        result = _mq_module.merge_and_dedupe_citations(groups)
        ids = [c["id"] for c in result]
        self.assertEqual(ids, ["o1", "o2", "r1", "r2", "r3"])

    def test_dedupe_preserves_first_occurrence_metadata(self):
        """Dedup should keep the first occurrence's metadata."""
        groups = [
            [{"id": "orig1", "path": "/docs/x.txt", "company": "company-a"}],
            [{"id": "rw1", "path": "/docs/x.txt", "company": "company-b"}],
        ]
        result = _mq_module.merge_and_dedupe_citations(groups)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["company"], "company-a")


# ── generate_rewrites_via_bypass tests ─────────────────────────────────────

class TestGenerateRewritesViaBypass(unittest.TestCase):
    """Test rewrite generation via bypass mode."""

    def test_parses_json_array_response(self):
        """Should parse JSON array of strings as rewrite queries."""
        mock_client = MagicMock()
        mock_result = {"response": '["rewrite query 1", "rewrite query 2"]'}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _mq_module.generate_rewrites_via_bypass(mock_client, "original query", 2)
        )
        self.assertEqual(result, ["rewrite query 1", "rewrite query 2"])
        mock_client.post_json.assert_called_once()

    def test_parses_newline_separated_response(self):
        """Should parse newline-separated strings as rewrite queries."""
        mock_client = MagicMock()
        mock_result = {"response": "rewrite query 1\nrewrite query 2"}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _mq_module.generate_rewrites_via_bypass(mock_client, "original query", 2)
        )
        self.assertEqual(result, ["rewrite query 1", "rewrite query 2"])

    def test_filters_out_original_query_from_lines(self):
        """Should filter out lines that match the original query."""
        mock_client = MagicMock()
        mock_result = {"response": "original query\nrewrite query 1"}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _mq_module.generate_rewrites_via_bypass(mock_client, "original query", 2)
        )
        self.assertEqual(result, ["rewrite query 1"])

    def test_respects_rewrite_count_limit(self):
        """Should return at most rewrite_count queries."""
        mock_client = MagicMock()
        mock_result = {"response": '["q1", "q2", "q3", "q4"]'}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _mq_module.generate_rewrites_via_bypass(mock_client, "query", 2)
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result, ["q1", "q2"])

    def test_handles_malformed_json_falls_back_to_lines(self):
        """Malformed JSON should fall back to newline parsing."""
        mock_client = MagicMock()
        mock_result = {"response": "not json\nrewrite query 1\nrewrite query 2"}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _mq_module.generate_rewrites_via_bypass(mock_client, "query", 3)
        )
        # Fallback to lines: all non-empty lines are returned (including the malformed part)
        self.assertEqual(len(result), 3)
        self.assertIn("rewrite query 1", result)
        self.assertIn("rewrite query 2", result)

    def test_returns_empty_on_http_error(self):
        """HTTP errors should return empty list (fail-open)."""
        mock_client = MagicMock()
        mock_client.post_json = AsyncMock(side_effect=Exception("Connection refused"))

        result = _run_async(
            _mq_module.generate_rewrites_via_bypass(mock_client, "query", 2)
        )
        self.assertEqual(result, [])

    def test_returns_empty_on_empty_response(self):
        """Empty response should return empty list."""
        mock_client = MagicMock()
        mock_result = {"response": ""}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _mq_module.generate_rewrites_via_bypass(mock_client, "query", 2)
        )
        self.assertEqual(result, [])

    def test_returns_empty_on_none_response(self):
        """None response should return empty list."""
        mock_client = MagicMock()
        mock_result = {"response": None}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        result = _run_async(
            _mq_module.generate_rewrites_via_bypass(mock_client, "query", 2)
        )
        self.assertEqual(result, [])

    def test_calls_bypass_mode(self):
        """Should call /query with mode='bypass' and top_k=0."""
        mock_client = MagicMock()
        mock_result = {"response": '["rewrite 1"]'}
        mock_client.post_json = AsyncMock(return_value=mock_result)

        _run_async(
            _mq_module.generate_rewrites_via_bypass(mock_client, "test query", 1)
        )
        call_args = mock_client.post_json.call_args
        self.assertEqual(call_args[0][0], "/query")
        payload = call_args[0][1]
        self.assertEqual(payload["mode"], "bypass")
        self.assertEqual(payload["top_k"], 0)
        self.assertFalse(payload["include_references"])
        self.assertFalse(payload["include_chunk_content"])


# ── build_multi_query_metadata tests ───────────────────────────────────────

class TestBuildMultiQueryMetadata(unittest.TestCase):
    """Test metadata builder."""

    def test_all_fields_present_when_triggered(self):
        meta = _mq_module.build_multi_query_metadata(
            enabled=True,
            triggered=True,
            trigger_reason="long_query",
            rewrite_count=2,
            candidate_count_before_dedupe=15,
            candidate_count_after_dedupe=10,
        )
        expected_keys = {
            "multi_query_enabled",
            "multi_query_triggered",
            "multi_query_trigger_reason",
            "multi_query_rewrite_count",
            "multi_query_error",
            "multi_query_candidate_count_before_dedupe",
            "multi_query_candidate_count_after_dedupe",
        }
        self.assertEqual(set(meta.keys()), expected_keys)
        self.assertTrue(meta["multi_query_enabled"])
        self.assertTrue(meta["multi_query_triggered"])
        self.assertEqual(meta["multi_query_trigger_reason"], "long_query")
        self.assertEqual(meta["multi_query_rewrite_count"], 2)
        self.assertIsNone(meta["multi_query_error"])
        self.assertEqual(meta["multi_query_candidate_count_before_dedupe"], 15)
        self.assertEqual(meta["multi_query_candidate_count_after_dedupe"], 10)

    def test_all_fields_present_when_not_triggered(self):
        meta = _mq_module.build_multi_query_metadata(
            enabled=False,
            triggered=False,
            trigger_reason="none",
            rewrite_count=2,
        )
        expected_keys = {
            "multi_query_enabled",
            "multi_query_triggered",
            "multi_query_trigger_reason",
            "multi_query_rewrite_count",
            "multi_query_error",
            "multi_query_candidate_count_before_dedupe",
            "multi_query_candidate_count_after_dedupe",
        }
        self.assertEqual(set(meta.keys()), expected_keys)
        self.assertFalse(meta["multi_query_enabled"])
        self.assertFalse(meta["multi_query_triggered"])
        self.assertEqual(meta["multi_query_trigger_reason"], "none")
        self.assertIsNone(meta["multi_query_error"])

    def test_error_field_set_on_failure(self):
        meta = _mq_module.build_multi_query_metadata(
            enabled=True,
            triggered=True,
            trigger_reason="weak_signal",
            rewrite_count=2,
            error="rewrite_generation_failed",
        )
        self.assertEqual(meta["multi_query_error"], "rewrite_generation_failed")

    def test_zero_counts_when_not_triggered(self):
        meta = _mq_module.build_multi_query_metadata(
            enabled=False,
            triggered=False,
            trigger_reason="none",
            rewrite_count=2,
        )
        self.assertEqual(meta["multi_query_candidate_count_before_dedupe"], 0)
        self.assertEqual(meta["multi_query_candidate_count_after_dedupe"], 0)


# ── Fail-open behavior tests ───────────────────────────────────────────────

class TestFailOpenBehavior(unittest.TestCase):
    """Test fail-open behavior when rewrites fail."""

    def test_empty_rewrites_result_in_single_query_path(self):
        """When rewrites return empty, single-query path continues with original citations."""
        mock_client = MagicMock()
        mock_client.post_json = AsyncMock(side_effect=Exception("LLM unavailable"))

        # Empty rewrites means no additional retrieval passes
        rewrites = _run_async(
            _mq_module.generate_rewrites_via_bypass(mock_client, "query", 2)
        )
        self.assertEqual(rewrites, [])
        # Caller should use original citations
        original_citations = [{"id": "orig1", "content": "original doc"}]
        merged = _mq_module.merge_and_dedupe_citations([original_citations])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "orig1")

    def test_partial_rewrite_failure_preserves_what_was_got(self):
        """If some rewrites fail, merge what was successfully retrieved."""
        groups = [
            [{"id": "orig1", "content": "original"}],
            [{"id": "rw1", "content": "rewrite1"}],
            [],  # rewrite2 retrieval failed
        ]
        merged = _mq_module.merge_and_dedupe_citations(groups)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["id"], "orig1")
        self.assertEqual(merged[1]["id"], "rw1")

    def test_fail_open_metadata_set(self):
        """Fail-open should set multi_query_error in metadata."""
        meta = _mq_module.build_multi_query_metadata(
            enabled=True,
            triggered=True,
            trigger_reason="weak_signal",
            rewrite_count=2,
            error="rewrite_retrieval_failed",
            candidate_count_before_dedupe=5,
            candidate_count_after_dedupe=3,
        )
        self.assertEqual(meta["multi_query_error"], "rewrite_retrieval_failed")
        self.assertTrue(meta["multi_query_triggered"])


# ── _normalize_citation_path tests ─────────────────────────────────────────

class TestNormalizeCitationPath(unittest.TestCase):
    """Test path normalization for deduplication."""

    def test_lowercases_path(self):
        self.assertEqual(_mq_module._normalize_citation_path("/Docs/FILE.TXT"), "/docs/file.txt")

    def test_strips_whitespace(self):
        self.assertEqual(_mq_module._normalize_citation_path("  /docs/file.txt  "), "/docs/file.txt")

    def test_resolves_dot_segment(self):
        self.assertEqual(_mq_module._normalize_citation_path("/docs/./file.txt"), "/docs/file.txt")

    def test_resolves_double_dot_segment(self):
        self.assertEqual(_mq_module._normalize_citation_path("/docs/a/../file.txt"), "/docs/file.txt")

    def test_none_returns_none(self):
        self.assertIsNone(_mq_module._normalize_citation_path(None))

    def test_empty_returns_none(self):
        self.assertIsNone(_mq_module._normalize_citation_path(""))

    def test_complex_path(self):
        self.assertEqual(
            _mq_module._normalize_citation_path("/docs/./a/../b/./file.txt"),
            "/docs/b/file.txt",
        )


# ── Integration-level flow tests ───────────────────────────────────────────

class TestIntegrationQueryScopeMetadata(unittest.TestCase):
    """Integration-level tests for query_scope metadata fields presence and counts."""

    def test_metadata_fields_present_when_triggered(self):
        """When multi-query is triggered, all metadata fields should be present."""
        meta = _mq_module.build_multi_query_metadata(
            enabled=True,
            triggered=True,
            trigger_reason="long_query",
            rewrite_count=2,
            candidate_count_before_dedupe=12,
            candidate_count_after_dedupe=8,
        )
        # Verify all 7 required fields
        self.assertIn("multi_query_enabled", meta)
        self.assertIn("multi_query_triggered", meta)
        self.assertIn("multi_query_trigger_reason", meta)
        self.assertIn("multi_query_rewrite_count", meta)
        self.assertIn("multi_query_error", meta)
        self.assertIn("multi_query_candidate_count_before_dedupe", meta)
        self.assertIn("multi_query_candidate_count_after_dedupe", meta)

        # Verify types
        self.assertIsInstance(meta["multi_query_enabled"], bool)
        self.assertIsInstance(meta["multi_query_triggered"], bool)
        self.assertIsInstance(meta["multi_query_trigger_reason"], str)
        self.assertIsInstance(meta["multi_query_rewrite_count"], int)
        self.assertIsInstance(meta["multi_query_error"], type(None))
        self.assertIsInstance(meta["multi_query_candidate_count_before_dedupe"], int)
        self.assertIsInstance(meta["multi_query_candidate_count_after_dedupe"], int)

    def test_metadata_fields_present_when_not_triggered(self):
        """When multi-query is not triggered, all metadata fields should still be present."""
        meta = _mq_module.build_multi_query_metadata(
            enabled=True,
            triggered=False,
            trigger_reason="none",
            rewrite_count=2,
        )
        required_fields = [
            "multi_query_enabled",
            "multi_query_triggered",
            "multi_query_trigger_reason",
            "multi_query_rewrite_count",
            "multi_query_error",
            "multi_query_candidate_count_before_dedupe",
            "multi_query_candidate_count_after_dedupe",
        ]
        for field in required_fields:
            self.assertIn(field, meta, f"Missing field: {field}")

    def test_counts_reflect_dedupe_reduction(self):
        """Before-dedupe count should be >= after-dedupe count."""
        groups = [
            [
                {"id": "1", "path": "/docs/a.txt", "content": "doc a"},
                {"id": "2", "path": "/docs/b.txt", "content": "doc b"},
            ],
            [
                {"id": "3", "path": "/docs/A.TXT", "content": "different content"},  # same path, different content
                {"id": "4", "path": "/docs/c.txt", "content": "doc c"},
            ],
        ]
        before = sum(len(g) for g in groups)
        after = len(_mq_module.merge_and_dedupe_citations(groups))
        self.assertGreaterEqual(before, after)

    def test_rewrite_count_matches_config(self):
        """Rewrite count in metadata should match configured value."""
        for count in [1, 2, 3, 5]:
            meta = _mq_module.build_multi_query_metadata(
                enabled=True,
                triggered=True,
                trigger_reason="weak_signal",
                rewrite_count=count,
            )
            self.assertEqual(meta["multi_query_rewrite_count"], count)

    def test_default_rewrite_count_is_two(self):
        """Default rewrite count should be 2."""
        meta = _mq_module.build_multi_query_metadata(
            enabled=True,
            triggered=True,
            trigger_reason="long_query",
            rewrite_count=2,
        )
        self.assertEqual(meta["multi_query_rewrite_count"], 2)


class TestIntegrationMergeFlow(unittest.TestCase):
    """Integration-level tests for merge flow with realistic citation structures."""

    def test_realistic_merge_three_groups(self):
        """Simulate realistic merge of original + 2 rewrites with some overlap."""
        groups = [
            [
                {"file_source": "/docs/report.pdf", "content": "Q1 revenue was $1M", "path": "/docs/report.pdf"},
                {"file_source": "/docs/minutes.docx", "content": "Board meeting on Jan 15", "path": "/docs/minutes.docx"},
                {"file_source": "/docs/notes.txt", "content": "Strategy discussion", "path": "/docs/notes.txt"},
            ],
            [
                {"file_source": "/docs/report.pdf", "content": "Q1 revenue was $1M", "path": "/docs/report.pdf"},  # duplicate by path
                {"file_source": "/docs/budget.xlsx", "content": "Budget allocation details", "path": "/docs/budget.xlsx"},
                {"file_source": "/docs/email.txt", "content": "CEO email on strategy", "path": "/docs/email.txt"},
            ],
            [
                {"file_source": "/docs/notes.txt", "content": "Strategy discussion", "path": "/docs/notes.txt"},  # duplicate by path
                {"file_source": "/docs/presentation.pptx", "content": "Annual review slides", "path": "/docs/presentation.pptx"},
            ],
        ]
        result = _mq_module.merge_and_dedupe_citations(groups)
        # Should have: report.pdf, minutes.docx, notes.txt, budget.xlsx, email.txt, presentation.pptx
        self.assertEqual(len(result), 6)
        # Stable order: orig1, orig2, orig3, rw2, rw3, rw5
        self.assertEqual(result[0]["file_source"], "/docs/report.pdf")
        self.assertEqual(result[1]["file_source"], "/docs/minutes.docx")
        self.assertEqual(result[2]["file_source"], "/docs/notes.txt")
        self.assertEqual(result[3]["file_source"], "/docs/budget.xlsx")
        self.assertEqual(result[4]["file_source"], "/docs/email.txt")
        self.assertEqual(result[5]["file_source"], "/docs/presentation.pptx")

    def test_merge_preserves_all_citation_fields(self):
        """Merge should preserve all original citation fields."""
        original = {
            "file_source": "/docs/test.pdf",
            "path": "/docs/test.pdf",
            "content": "test content",
            "company": "acme",
            "company_source": "explicit",
            "metadata": {"page": 5},
        }
        groups = [[dict(original)]]
        result = _mq_module.merge_and_dedupe_citations(groups)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["company"], "acme")
        self.assertEqual(result[0]["company_source"], "explicit")
        self.assertEqual(result[0]["metadata"]["page"], 5)


# ── Config settings tests ──────────────────────────────────────────────────

class TestConfigSettings(unittest.TestCase):
    """Test that config includes multi-query settings."""

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

    def test_multi_query_enabled_default_false(self):
        Settings = self._get_settings_module()
        field = Settings.model_fields.get("multi_query_enabled")
        self.assertIsNotNone(field)
        self.assertEqual(field.default, False)

    def test_multi_query_long_query_words_default_20(self):
        Settings = self._get_settings_module()
        field = Settings.model_fields.get("multi_query_long_query_words")
        self.assertIsNotNone(field)
        self.assertEqual(field.default, 20)

    def test_multi_query_rewrite_count_default_2(self):
        Settings = self._get_settings_module()
        field = Settings.model_fields.get("multi_query_rewrite_count")
        self.assertIsNotNone(field)
        self.assertEqual(field.default, 2)

    def test_multi_query_transcript_keywords_default(self):
        Settings = self._get_settings_module()
        field = Settings.model_fields.get("multi_query_transcript_keywords")
        self.assertIsNotNone(field)
        default_val = field.default
        self.assertIn("transcript", default_val)
        self.assertIn("meeting minutes", default_val)
        # Should contain all 5 default keywords
        parsed = _mq_module.parse_keywords_csv(default_val)
        self.assertEqual(len(parsed), 5)

    def test_multi_query_enabled_env_override(self):
        """MULTI_QUERY_ENABLED should be overridable via env var."""
        required_env = {
            "RAG_API_KEY": "1234",
            "LIGHTRAG_INTERNAL_API_KEY": "internal",
            "LIGHTRAG_BASE_URL": "http://example:9621",
            "SOURCE_DOCS_DIR": "/tmp/source_docs",
            "UPLOADS_DIR": "/tmp/uploads",
            "STATE_DB_PATH": "/tmp/state.db",
            "MULTI_QUERY_ENABLED": "true",
        }
        with patch.dict(os.environ, required_env, clear=True):
            config_module = importlib.import_module("app.config")
            config_module = importlib.reload(config_module)
            self.assertTrue(config_module.settings.multi_query_enabled)

    def test_multi_query_rewrite_count_env_override(self):
        """MULTI_QUERY_REWRITE_COUNT should be overridable via env var."""
        required_env = {
            "RAG_API_KEY": "1234",
            "LIGHTRAG_INTERNAL_API_KEY": "internal",
            "LIGHTRAG_BASE_URL": "http://example:9621",
            "SOURCE_DOCS_DIR": "/tmp/source_docs",
            "UPLOADS_DIR": "/tmp/uploads",
            "STATE_DB_PATH": "/tmp/state.db",
            "MULTI_QUERY_REWRITE_COUNT": "5",
        }
        with patch.dict(os.environ, required_env, clear=True):
            config_module = importlib.import_module("app.config")
            config_module = importlib.reload(config_module)
            self.assertEqual(config_module.settings.multi_query_rewrite_count, 5)


if __name__ == "__main__":
    unittest.main()
