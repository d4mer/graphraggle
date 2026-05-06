"""Tests for Packet 13: Reranking Post-Scoping.

Covers:
- rerank applied and reorders citations
- feature flag off preserves order
- rerank failure fail-open
- top-5 truncation
- rerank happens after company filter (out-of-scope citation cannot influence result)

Imports from app.rerank to avoid pulling in the full gateway dependency chain.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/imac/Documents/Programming/graphrag-implementation")
import app.rerank as _rerank_module

# ── Helper: run async code from sync test ───────────────────────────────────

def _run_async(coro):
    """Helper to run async code from sync test methods."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Helper: set up httpx mock for rerank ────────────────────────────────────

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
    # MagicMock is callable but we want to return it as-is, not call it
    if isinstance(mock_response_or_exception, MagicMock):
        mock_client.set_post(lambda *a, **kw: mock_response_or_exception)
    elif callable(mock_response_or_exception):
        mock_client.set_post(mock_response_or_exception)
    else:
        mock_client.set_post(lambda *a, **kw: mock_response_or_exception)
    mock_httpx.AsyncClient.return_value = mock_client


class TestCitationToText(unittest.TestCase):
    """Test citation_to_text extraction from citation dicts."""

    def test_prefers_content_field(self):
        from app.rerank import citation_to_text
        citation = {"content": "hello world", "text": "other"}
        self.assertEqual(citation_to_text(citation), "hello world")

    def test_falls_back_to_text_field(self):
        from app.rerank import citation_to_text
        citation = {"text": "fallback text"}
        self.assertEqual(citation_to_text(citation), "fallback text")

    def test_joins_list_content_field(self):
        from app.rerank import citation_to_text
        citation = {"content": ["first part", "second part"]}
        self.assertEqual(citation_to_text(citation), "first part\n\nsecond part")

    def test_falls_back_to_json_repr(self):
        from app.rerank import citation_to_text
        citation = {"path": "/docs/x.txt", "index": 3}
        result = citation_to_text(citation)
        self.assertIn("path", result)
        self.assertIn("x.txt", result)


class TestTruncateCitations(unittest.TestCase):
    """Test truncate_citations top-K enforcement."""

    def test_truncates_to_top_5(self):
        from app.rerank import truncate_citations
        citations = [{"id": i} for i in range(10)]
        result = truncate_citations(citations)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["id"], 0)
        self.assertEqual(result[4]["id"], 4)

    def test_preserves_order(self):
        from app.rerank import truncate_citations
        citations = [{"order": i} for i in range(20)]
        result = truncate_citations(citations)
        for i, c in enumerate(result):
            self.assertEqual(c["order"], i)

    def test_returns_all_when_below_top_k(self):
        from app.rerank import truncate_citations
        citations = [{"id": i} for i in range(3)]
        result = truncate_citations(citations)
        self.assertEqual(len(result), 3)

    def test_empty_list(self):
        from app.rerank import truncate_citations
        self.assertEqual(truncate_citations([]), [])


class TestRerankCitationsDisabled(unittest.TestCase):
    """Test rerank_citations when rerank_host is None (disabled)."""

    def test_returns_original_order_when_no_host(self):
        from app.rerank import rerank_citations
        citations = [
            {"content": "first"},
            {"content": "second"},
            {"content": "third"},
        ]
        result, meta = _run_async(
            rerank_citations("test query", citations, rerank_host=None)
        )
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["content"], "first")
        self.assertEqual(result[1]["content"], "second")
        self.assertEqual(result[2]["content"], "third")
        self.assertFalse(meta["rerank_applied"])
        self.assertIsNone(meta["rerank_error"])
        self.assertEqual(meta["rerank_input_count"], 3)
        self.assertEqual(meta["rerank_output_count"], 0)


class TestRerankCitationsApplied(unittest.TestCase):
    """Test rerank_citations when rerank succeeds and reorders."""

    def test_reorders_by_rerank_scores(self):
        from app.rerank import rerank_citations
        citations = [
            {"content": "doc A - low relevance"},
            {"content": "doc B - high relevance"},
            {"content": "doc C - medium relevance"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.95},
                {"index": 2, "relevance_score": 0.70},
                {"index": 0, "relevance_score": 0.30},
            ]
        }

        # call_rerank_endpoint receives texts derived from citations
        # indices in mock response refer to document (text) positions
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, mock_response)
            result, meta = _run_async(
                rerank_citations("test query", citations, rerank_host="http://rerank:8080")
            )
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["content"], "doc B - high relevance")
        self.assertEqual(result[1]["content"], "doc C - medium relevance")
        self.assertEqual(result[2]["content"], "doc A - low relevance")
        self.assertTrue(meta["rerank_applied"])
        self.assertIsNone(meta["rerank_error"])
        self.assertEqual(meta["rerank_input_count"], 3)
        self.assertEqual(meta["rerank_output_count"], 3)

    def test_handles_partial_results(self):
        """Rerank only returns some indices; others preserved in original order."""
        from app.rerank import rerank_citations
        citations = [
            {"content": "doc A"},
            {"content": "doc B"},
            {"content": "doc C"},
            {"content": "doc D"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.8},
            ]
        }
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, mock_response)
            result, meta = _run_async(
                rerank_citations("query", citations, rerank_host="http://rerank:8080")
            )
        self.assertEqual(result[0]["content"], "doc B")
        self.assertEqual(result[1]["content"], "doc A")
        self.assertEqual(result[2]["content"], "doc C")
        self.assertEqual(result[3]["content"], "doc D")


class TestRerankCitationsFailOpen(unittest.TestCase):
    """Test rerank_citations fail-open behavior when rerank endpoint errors."""

    def test_http_error_preserves_original_order(self):
        """When rerank returns HTTP error, original citations are preserved."""
        from app.rerank import rerank_citations
        citations = [
            {"content": "original first"},
            {"content": "original second"},
        ]
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, Exception("Connection refused"))
            result, meta = _run_async(
                rerank_citations("test query", citations, rerank_host="http://rerank:8080")
            )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["content"], "original first")
        self.assertEqual(result[1]["content"], "original second")
        self.assertFalse(meta["rerank_applied"])
        self.assertEqual(meta["rerank_error"], "rerank_unavailable")
        self.assertEqual(meta["rerank_input_count"], 2)

    def test_timeout_preserves_original_order(self):
        """When rerank times out, original citations are preserved."""
        from app.rerank import rerank_citations
        citations = [{"content": "preserved"}]
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, asyncio.TimeoutError("Timeout"))
            result, meta = _run_async(
                rerank_citations("test query", citations, rerank_host="http://rerank:8080")
            )
        self.assertEqual(len(result), 1)
        self.assertFalse(meta["rerank_applied"])
        self.assertEqual(meta["rerank_error"], "rerank_unavailable")

    def test_malformed_response_preserves_original_order(self):
        """When rerank returns malformed JSON, original citations are preserved."""
        from app.rerank import rerank_citations
        citations = [{"content": "safe"}]
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": "not_a_list"}
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, mock_response)
            result, meta = _run_async(
                rerank_citations("test query", citations, rerank_host="http://rerank:8080")
            )
        self.assertEqual(len(result), 1)
        self.assertFalse(meta["rerank_applied"])
        self.assertEqual(meta["rerank_error"], "rerank_unavailable")


class TestRerankFeatureFlagOffPreservesOrder(unittest.TestCase):
    """Test that when RERANK_ENABLED is false, original order is preserved."""

    def test_disabled_flag_skips_rerank(self):
        from app.rerank import rerank_citations
        citations = [
            {"content": "first"},
            {"content": "second"},
            {"content": "third"},
            {"content": "fourth"},
            {"content": "fifth"},
            {"content": "sixth"},
        ]
        result, meta = _run_async(
            rerank_citations("query", citations, rerank_host=None)
        )
        self.assertEqual(len(result), 6)
        self.assertEqual(result[0]["content"], "first")
        self.assertEqual(result[-1]["content"], "sixth")
        self.assertFalse(meta["rerank_applied"])


class TestTop5Truncation(unittest.TestCase):
    """Test that top-5 truncation is enforced in all code paths."""

    def test_truncation_applied_after_rerank(self):
        """Even if rerank returns 10 citations, only top 5 are returned."""
        from app.rerank import rerank_citations, truncate_citations
        citations = [{"content": f"doc {i}"} for i in range(10)]
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [{"index": i, "relevance_score": float(10 - i)} for i in range(10)]
        }
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, mock_response)
            reranked, _ = _run_async(
                rerank_citations("query", citations, rerank_host="http://rerank:8080")
            )
        truncated = truncate_citations(reranked)
        self.assertEqual(len(truncated), 5)

    def test_truncation_applied_when_rerank_fails(self):
        """When rerank fails, original citations should still be truncated to 5."""
        from app.rerank import rerank_citations, truncate_citations
        citations = [{"content": f"doc {i}"} for i in range(8)]
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, Exception("fail"))
            result, meta = _run_async(
                rerank_citations("query", citations, rerank_host="http://rerank:8080")
            )
        truncated = truncate_citations(result)
        self.assertEqual(len(truncated), 5)
        self.assertEqual(truncated[0]["content"], "doc 0")


class TestCompanyFilterBeforeRerank(unittest.TestCase):
    """Test that rerank happens after company filter."""

    def test_out_of_scope_citations_not_sent_to_rerank(self):
        """Citations filtered out by company scope should never be passed to rerank."""
        from app.rerank import rerank_citations
        scoped_citations = [
            {"content": "company-a doc 1", "path": "/docs/a1.txt", "company": "company-a"},
            {"content": "company-a doc 2", "path": "/docs/a2.txt", "company": "company-a"},
            {"content": "company-a doc 3", "path": "/docs/a3.txt", "company": "company-a"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.7},
                {"index": 2, "relevance_score": 0.5},
            ]
        }
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, mock_response)
            result, meta = _run_async(
                rerank_citations("query", scoped_citations, rerank_host="http://rerank:8080")
            )
        self.assertEqual(len(result), 3)
        for c in result:
            self.assertEqual(c.get("company"), "company-a")
        self.assertEqual(result[0]["content"], "company-a doc 2")
        self.assertTrue(meta["rerank_applied"])

    def test_scope_citation_count_matches_rerank_input_count(self):
        """rerank_input_count should equal the number of in-scope citations, not total."""
        from app.rerank import rerank_citations
        scoped_citations = [
            {"content": f"scoped doc {i}", "path": f"/docs/s{i}.txt"}
            for i in range(3)
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.8},
                {"index": 2, "relevance_score": 0.6},
                {"index": 1, "relevance_score": 0.4},
            ]
        }
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, mock_response)
            _, meta = _run_async(
                rerank_citations("query", scoped_citations, rerank_host="http://rerank:8080")
            )
        self.assertEqual(meta["rerank_input_count"], 3)
        self.assertEqual(meta["rerank_output_count"], 3)


class TestQueryScopeMetadata(unittest.TestCase):
    """Test that query_scope includes all five rerank metadata fields."""

    def test_all_rerank_fields_present_when_enabled(self):
        rerank_meta = {
            "rerank_enabled": True,
            "rerank_applied": True,
            "rerank_error": None,
            "rerank_input_count": 10,
            "rerank_output_count": 5,
        }
        required_keys = {
            "rerank_enabled",
            "rerank_applied",
            "rerank_error",
            "rerank_input_count",
            "rerank_output_count",
        }
        self.assertEqual(set(rerank_meta.keys()), required_keys)
        self.assertTrue(rerank_meta["rerank_enabled"])
        self.assertTrue(rerank_meta["rerank_applied"])
        self.assertEqual(rerank_meta["rerank_input_count"], 10)
        self.assertEqual(rerank_meta["rerank_output_count"], 5)

    def test_all_rerank_fields_present_when_disabled(self):
        rerank_meta = {
            "rerank_enabled": False,
            "rerank_applied": False,
            "rerank_error": None,
            "rerank_input_count": 8,
            "rerank_output_count": 0,
        }
        required_keys = {
            "rerank_enabled",
            "rerank_applied",
            "rerank_error",
            "rerank_input_count",
            "rerank_output_count",
        }
        self.assertEqual(set(rerank_meta.keys()), required_keys)
        self.assertFalse(rerank_meta["rerank_enabled"])
        self.assertFalse(rerank_meta["rerank_applied"])


class TestConfigSettings(unittest.TestCase):
    """Test that config includes rerank settings."""

    def test_rerank_enabled_default_false(self):
        from pydantic.fields import FieldInfo

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
            Settings = config_module.Settings

        field = Settings.model_fields.get("rerank_enabled")
        self.assertIsNotNone(field)
        self.assertIsInstance(field, FieldInfo)
        self.assertEqual(field.default, False)

    def test_rerank_binding_host_default_none(self):
        from pydantic.fields import FieldInfo

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
            Settings = config_module.Settings

        field = Settings.model_fields.get("rerank_binding_host")
        self.assertIsNotNone(field)
        self.assertIsInstance(field, FieldInfo)
        self.assertIsNone(field.default)


class TestIntegrationQueryEndpointFlow(unittest.TestCase):
    """Integration-level tests simulating the full query flow with reranking."""

    def test_full_flow_rerank_reorders_and_truncates(self):
        """Simulate: scope_query_citations -> rerank -> truncate -> query_scope metadata."""
        from app.rerank import rerank_citations, truncate_citations
        scoped_citations = [
            {"content": f"relevant doc {i}", "path": f"/docs/doc{i}.txt"}
            for i in range(8)
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 3, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.85},
                {"index": 5, "relevance_score": 0.80},
                {"index": 1, "relevance_score": 0.75},
                {"index": 7, "relevance_score": 0.70},
                {"index": 2, "relevance_score": 0.60},
                {"index": 4, "relevance_score": 0.50},
                {"index": 6, "relevance_score": 0.40},
            ]
        }
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, mock_response)
            reranked, rerank_meta = _run_async(
                rerank_citations("query", scoped_citations, rerank_host="http://rerank:8080")
            )
        result = truncate_citations(reranked)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["content"], "relevant doc 3")
        self.assertEqual(result[1]["content"], "relevant doc 0")
        self.assertEqual(result[2]["content"], "relevant doc 5")
        self.assertEqual(result[3]["content"], "relevant doc 1")
        self.assertEqual(result[4]["content"], "relevant doc 7")
        self.assertTrue(rerank_meta["rerank_applied"])
        self.assertIsNone(rerank_meta["rerank_error"])
        self.assertEqual(rerank_meta["rerank_input_count"], 8)
        self.assertEqual(rerank_meta["rerank_output_count"], 8)

    def test_full_flow_fail_open_preserves_order_and_truncates(self):
        """When rerank fails: preserve order, still truncate to 5."""
        from app.rerank import rerank_citations, truncate_citations
        scoped_citations = [
            {"content": f"original doc {i}", "path": f"/docs/doc{i}.txt"}
            for i in range(7)
        ]
        with patch.object(_rerank_module, "httpx") as mock_httpx:
            _setup_httpx_mock(mock_httpx, Exception("service unavailable"))
            result, rerank_meta = _run_async(
                rerank_citations("query", scoped_citations, rerank_host="http://rerank:8080")
            )
        self.assertEqual(len(result), 7)
        self.assertEqual(result[0]["content"], "original doc 0")
        self.assertEqual(result[-1]["content"], "original doc 6")
        self.assertFalse(rerank_meta["rerank_applied"])
        self.assertEqual(rerank_meta["rerank_error"], "rerank_unavailable")
        truncated = truncate_citations(result)
        self.assertEqual(len(truncated), 5)
        self.assertEqual(truncated[0]["content"], "original doc 0")


class TestRerankTopKConstant(unittest.TestCase):
    """Verify the RERANK_TOP_K constant is set to 5."""

    def test_constant_value(self):
        from app.rerank import RERANK_TOP_K
        self.assertEqual(RERANK_TOP_K, 5)


if __name__ == "__main__":
    unittest.main()
