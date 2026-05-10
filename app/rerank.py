"""Reranking helpers for citation reordering.

Pure functions that can be imported without pulling in the full gateway dependency chain.
This module has no dependencies on FastAPI, auth, config, or state store.
"""

from __future__ import annotations

import json
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - test environments may patch this module directly
    class _MissingHttpx:
        AsyncClient = None

    httpx = _MissingHttpx()

RERANK_TOP_K = 5
RERANK_TIMEOUT_SECONDS = 10


def build_rerank_url(rerank_host: str) -> str:
    base = rerank_host.rstrip("/")
    if base.endswith("/rerank"):
        return base
    if base.endswith("/v1"):
        return f"{base}/rerank"
    return f"{base}/v1/rerank"


def citation_to_text(citation: dict[str, Any]) -> str:
    """Extract a human-readable text snippet from a citation dict for reranking."""
    for key in ("content", "text", "chunk_text", "body"):
        value = citation.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:4000]
        if isinstance(value, list):
            parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
            if parts:
                return "\n\n".join(parts)[:4000]
    return json.dumps(citation, sort_keys=True, default=str)


def truncate_citations(citations: list[dict[str, Any]], top_k: int = RERANK_TOP_K) -> list[dict[str, Any]]:
    """Truncate a citation list to the top_k most relevant citations."""
    return citations[:top_k]


async def call_rerank_endpoint(
    query: str,
    documents: list[str],
    rerank_host: str,
    rerank_api_key: str | None = None,
    rerank_model: str | None = None,
) -> list[dict[str, Any]] | None:
    """Call an external rerank endpoint and return sorted results or None on failure.

    Returns a list of {"index": int, "relevance_score": float} dicts sorted by
    relevance_score descending, or None if the call fails.
    """
    indexed_docs = [(i, doc) for i, doc in enumerate(documents) if doc.strip()]
    if not indexed_docs:
        return None

    payload = {
        "query": query,
        "documents": [doc for _, doc in indexed_docs],
    }
    if rerank_model:
        payload["model"] = rerank_model

    try:
        headers: dict[str, str] = {}
        if rerank_api_key:
            headers["Authorization"] = f"Bearer {rerank_api_key}"
        async with httpx.AsyncClient(timeout=RERANK_TIMEOUT_SECONDS) as http_client:
            resp = await http_client.post(
                build_rerank_url(rerank_host),
                json=payload,
                headers=headers or None,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None

    results = data.get("results", [])
    if not isinstance(results, list):
        return None

    mapped: list[dict[str, Any]] = []
    for r in results:
        idx = r.get("index")
        score = r.get("relevance_score", 0.0)
        if idx is not None and 0 <= idx < len(indexed_docs):
            mapped.append({"index": indexed_docs[idx][0], "relevance_score": float(score)})

    mapped.sort(key=lambda x: x["relevance_score"], reverse=True)
    return mapped


async def rerank_citations(
    query: str,
    citations: list[dict[str, Any]],
    rerank_host: str | None,
    rerank_api_key: str | None = None,
    rerank_model: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rerank citations by relevance to the query.

    Returns (reranked_citations, metadata_dict).
    If rerank_host is absent or the call fails, returns (original_citations, error_meta).
    """
    metadata = {
        "rerank_enabled": rerank_host is not None,
        "rerank_applied": False,
        "rerank_error": None,
        "rerank_input_count": len(citations),
        "rerank_output_count": 0,
    }

    if not rerank_host:
        return citations, metadata

    documents = [citation_to_text(c) for c in citations]

    results = await call_rerank_endpoint(
        query,
        documents,
        rerank_host,
        rerank_api_key=rerank_api_key,
        rerank_model=rerank_model,
    )

    if results is None:
        metadata["rerank_error"] = "rerank_unavailable"
        return citations, metadata

    ranked_citations: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for r in results:
        idx = r["index"]
        if idx not in seen_indices and 0 <= idx < len(citations):
            ranked_citations.append(citations[idx])
            seen_indices.add(idx)
    for i, c in enumerate(citations):
        if i not in seen_indices:
            ranked_citations.append(c)
            seen_indices.add(i)

    metadata["rerank_applied"] = True
    metadata["rerank_output_count"] = len(ranked_citations)
    return ranked_citations, metadata
