"""Multi-query expansion helpers for citation retrieval.

Pure functions that can be imported without pulling in the full gateway dependency chain.
This module has no dependencies on FastAPI, auth, config, or state store.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

# Default transcript-like keywords (mirrors api.py TRANSCRIPT_QUERY_KEYWORDS)
DEFAULT_TRANSCRIPT_KEYWORDS = "transcript,workshop,speaker,meeting minutes,recording"

# Weak answer markers (mirrors api.py WEAK_ANSWER_MARKERS)
WEAK_ANSWER_MARKERS = ("not enough information", "do not have enough information")


def parse_keywords_csv(csv_string: str) -> list[str]:
    """Parse a comma-separated keywords string into a list of stripped, lowercased keywords.

    Empty strings after splitting are excluded.
    """
    if not csv_string:
        return []
    return [kw.strip().lower() for kw in csv_string.split(",") if kw.strip()]


def should_trigger_multi_query(
    query: str,
    transcript_keywords: list[str],
    long_query_words: int,
    weak_signal: bool,
) -> tuple[bool, str]:
    """Decide whether multi-query expansion should be triggered.

    Returns (triggered: bool, reason: str).

    Trigger conditions (any one):
    - Query word count exceeds long_query_words threshold.
    - Query contains a transcript-like keyword.
    - weak_signal is True (weak first-pass retrieval signal).

    If none match, returns (False, "none").
    """
    if not query or not query.strip():
        return False, "none"

    lowered = query.lower()
    words = query.split()

    # Long query trigger
    if len(words) >= long_query_words:
        return True, "long_query"

    # Transcript keyword trigger
    for kw in transcript_keywords:
        if kw in lowered:
            return True, "transcript_keyword"

    # Weak signal trigger
    if weak_signal:
        return True, "weak_signal"

    return False, "none"


def has_weak_answer_signal(answer: str) -> bool:
    """Check if an answer string contains weak-answer markers."""
    lowered = answer.lower()
    return any(marker in lowered for marker in WEAK_ANSWER_MARKERS)


def _normalize_citation_path(path: str | None) -> str | None:
    """Normalize a citation path for deduplication comparison.

    Lowercases, strips whitespace, resolves '.' and '..' segments.
    Preserves leading '/' for absolute paths.
    Returns None if path is None or empty after normalization.
    """
    if not path:
        return None
    normalized = path.strip().lower()
    if not normalized:
        return None
    # Preserve leading slash for absolute paths
    is_absolute = normalized.startswith("/")
    # Simple path normalization: split by '/', resolve '.' and '..', rejoin
    parts = normalized.split("/")
    resolved: list[str] = []
    for part in parts:
        if part == "." or part == "":
            continue
        elif part == ".." and resolved:
            resolved.pop()
        else:
            resolved.append(part)
    result = "/".join(resolved)
    if is_absolute and result:
        result = "/" + result
    return result if result else None


def merge_and_dedupe_citations(
    groups: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Merge citation groups in stable order and deduplicate.

    Groups are processed in order: group 0 (original) first, then group 1 (rewrite1), etc.
    Deduplication strategy:
    1. Primary: normalized citation path (exact match after normalization).
    2. Fallback: content-based match (first field among 'content', 'text', 'chunk_text', 'body').

    Returns a deduplicated list of citations in stable merge order.
    """
    if not groups:
        return []

    seen_paths: set[str] = set()
    seen_contents: set[str] = set()
    result: list[dict[str, Any]] = []

    # Extract content key for a citation
    def _get_content(citation: dict[str, Any]) -> str | None:
        for key in ("content", "text", "chunk_text", "body"):
            value = citation.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    for group in groups:
        if not group:
            continue
        for citation in group:
            # Primary dedupe: normalized path
            path = _normalize_citation_path(
                citation.get("file_source")
                or citation.get("path")
                or citation.get("source")
                or citation.get("file_path")
            )
            if path and path in seen_paths:
                continue
            if path:
                seen_paths.add(path)

            # Fallback dedupe: content
            content = _get_content(citation)
            if content and content in seen_contents:
                continue
            if content:
                seen_contents.add(content)

            result.append(citation)

    return result


def build_multi_query_metadata(
    enabled: bool,
    triggered: bool,
    trigger_reason: str,
    rewrite_count: int,
    error: str | None = None,
    candidate_count_before_dedupe: int = 0,
    candidate_count_after_dedupe: int = 0,
) -> dict[str, Any]:
    """Build multi-query metadata dict for query_scope.

    All fields are present regardless of whether multi-query was triggered,
    to ensure consistent observability.
    """
    meta: dict[str, Any] = {
        "multi_query_enabled": enabled,
        "multi_query_triggered": triggered,
        "multi_query_trigger_reason": trigger_reason,
        "multi_query_rewrite_count": rewrite_count,
        "multi_query_error": error,
        "multi_query_candidate_count_before_dedupe": candidate_count_before_dedupe,
        "multi_query_candidate_count_after_dedupe": candidate_count_after_dedupe,
    }
    return meta


async def generate_rewrites_via_bypass(
    client: Any,
    query: str,
    rewrite_count: int,
) -> list[str]:
    """Generate rewrite queries using LightRAG /query in bypass mode.

    Sends the original query to LightRAG in bypass mode with a prompt asking
    for rewrite variants. Parses the response to extract rewrite query strings.

    Parsing strategy (graceful fallback):
    1. Try to parse response as JSON array of strings.
    2. Try to parse response as newline-separated query strings.
    3. If both fail, return empty list (caller should fail-open).

    Returns a list of rewritten query strings (up to rewrite_count).
    """
    rewrite_prompt = (
        "Generate exactly "
        + str(rewrite_count)
        + " variant queries that capture the same information need as the original query. "
        "Return only the variant queries, one per line. Do not include the original query."
    )

    try:
        result = await client.post_json(
            "/query",
            {
                "query": rewrite_prompt + "\n\nOriginal query: " + query,
                "mode": "bypass",
                "top_k": 1,
                "include_references": False,
                "include_chunk_content": False,
            },
        )
    except Exception:
        return []

    raw_response = result.get("response")
    if raw_response is None:
        return []
    response_text = str(raw_response).strip()
    if not response_text:
        return []

    # Try JSON array first
    try:
        parsed = json.loads(response_text)
        if isinstance(parsed, list):
            queries = [str(q).strip() for q in parsed if isinstance(q, str) and q.strip()]
            if queries:
                return queries[:rewrite_count]
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: newline-separated lines
    lines = [line.strip() for line in response_text.strip().split("\n") if line.strip()]
    # Filter out lines that look like the original query
    filtered = [line for line in lines if line.lower() != query.lower().strip()]
    return filtered[:rewrite_count]
