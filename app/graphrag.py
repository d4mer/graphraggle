"""GraphRAG adaptive expansion helpers for citation enrichment.

Pure functions that can be imported without pulling in the full gateway dependency chain.
This module has no dependencies on FastAPI, auth, config, or state store.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Default number of top citations to use for entity extraction seeds
DEFAULT_SEED_CITATION_COUNT = 3
# Maximum total neighbors across all hops
DEFAULT_MAX_NEIGHBORS = 10
# Default hop count (1-hop base, adaptive escalation to 2)
DEFAULT_HOPS = 1

# Entity extraction prompt – strict JSON array response
_ENTITY_EXTRACTION_PROMPT = (
    "Extract up to 10 key entities (persons, organizations, locations, concepts, "
    "products, events) from the following text. Return ONLY a JSON array of "
    "string entity names. Do not include any explanation or other text. "
    "Example: [\"Alice\", \"Acme Corp\", \"Q3 revenue\"]\n\nTEXT:\n{text}"
)

# Graph neighbor query prompt for LightRAG-style API
_GRAPH_NEIGHBOR_PROMPT = (
    "Expand on the following entities by finding related concepts, "
    "associated documents, and contextual information. "
    "Return a JSON array of expansion objects with fields: "
    '"entity" (str), "related_entity" (str), "relationship" (str), '
    '"context" (str). Return only the JSON array.\n\nENTITIES:\n{entities}'
)


# ── citation_to_text_for_entity_extraction ──────────────────────────────────

def citation_to_text_for_entity_extraction(citation: dict[str, Any]) -> str:
    """Extract a human-readable text snippet from a citation dict for entity extraction.

    Prefers content-rich fields and caps length to avoid LLM token limits.
    Appends path as context hint when content is present.
    Returns a short fallback JSON repr if no text fields are present.
    """
    parts: list[str] = []
    content_found = False
    for key in ("content", "text", "chunk_text", "body"):
        value = citation.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip()[:2000])
            content_found = True
            break
        if isinstance(value, list):
            joined = "\n\n".join(
                item.strip() for item in value if isinstance(item, str) and item.strip()
            )
            if joined:
                parts.append(joined[:2000])
                content_found = True
                break
    # Append path as context hint only when content was found
    if content_found:
        for path_key in ("file_source", "path", "source", "file_path"):
            p = citation.get(path_key)
            if isinstance(p, str) and p.strip():
                parts.append(f"Source: {p.strip()}")
                break
    return "\n\n".join(parts) if parts else json.dumps(citation, sort_keys=True, default=str)


# ── extract_entities_via_llm ────────────────────────────────────────────────

async def extract_entities_via_llm(
    client: Any,
    citations: list[dict[str, Any]],
    seed_count: int = DEFAULT_SEED_CITATION_COUNT,
) -> list[str]:
    """Extract entities from the top seed_count citations via LLM bypass query.

    Calls /query in bypass mode with a strict JSON-array prompt.
    Robust parsing: JSON array -> newline-separated strings -> [] on failure.
    Always returns a list (never raises); empty list on any failure.
    """
    if not citations:
        return []

    # Take top N citations for entity extraction
    top_citations = citations[:seed_count]

    # Build combined text from top citations
    texts = []
    for citation in top_citations:
        text = citation_to_text_for_entity_extraction(citation)
        if text.strip():
            texts.append(text)

    if not texts:
        return []

    combined_text = "\n\n---\n\n".join(texts)
    prompt = _ENTITY_EXTRACTION_PROMPT.format(text=combined_text)

    try:
        result = await client.post_json(
            "/query",
            {
                "query": prompt,
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

    # Try JSON array first (strict requirement from PRD)
    try:
        parsed = json.loads(response_text)
        if isinstance(parsed, list):
            entities = [str(e).strip() for e in parsed if isinstance(e, str) and e.strip()]
            if entities:
                return entities
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: newline-separated entity names
    lines = [line.strip().lstrip("-*#").strip() for line in response_text.split("\n")]
    entities = [line for line in lines if line and not re.match(r"^\[|\]$", line)]
    if entities:
        return entities

    return []


# ── expand_graph_neighbors ──────────────────────────────────────────────────

async def expand_graph_neighbors(
    client: Any,
    entities: list[str],
    max_neighbors: int = DEFAULT_MAX_NEIGHBORS,
    hops: int = DEFAULT_HOPS,
) -> tuple[list[dict[str, Any]], str | None, int]:
    """Expand graph neighbors for the given entities via LightRAG API.

    Tries best-effort graph endpoint calls. Returns (neighbors, error, hops_used).
    Neighbors are deterministic: deduped by (entity, related_entity, relationship) tuple,
    trimmed to max_neighbors total.

    If the graph endpoint(s) are unavailable, returns ([], error_string, hops_used).
    """
    if not entities:
        return [], None, 0

    all_neighbors: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    error: str | None = None
    hops_used = 0

    def _add_neighbor(entity: str, related: str, relationship: str, context: str):
        key = (entity.lower(), related.lower(), relationship.lower())
        if key not in seen_keys:
            seen_keys.add(key)
            all_neighbors.append({
                "seed_entity": entity,
                "related_entity": related,
                "relationship": relationship,
                "context": context or "",
            })
        # Trim to max_neighbors deterministically
        if len(all_neighbors) >= max_neighbors:
            return True  # budget exhausted

    async def _query_graph_via_bypass(entity_text: str, hop_label: str) -> list[dict[str, Any]]:
        """Query graph neighbors via LightRAG bypass mode with entity prompt."""
        try:
            result = await client.post_json(
                "/query",
                {
                    "query": f"Find graph neighbors and relationships for: {entity_text}",
                    "mode": "bypass",
                    "top_k": 1,
                    "include_references": False,
                    "include_chunk_content": False,
                },
            )
        except Exception as exc:
            nonlocal error
            error = f"graph_query_{hop_label}_failed: {str(exc)}"
            return []

        raw = result.get("response")
        if raw is None:
            return []
        text = str(raw).strip()
        if not text:
            return []

        # Try JSON array of expansion objects
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                results = []
                for item in parsed:
                    if isinstance(item, dict):
                        entity = item.get("entity", "") or item.get("node", "") or ""
                        related = item.get("related_entity", "") or item.get("neighbor", "") or ""
                        rel = item.get("relationship", "") or item.get("relation", "") or ""
                        ctx = item.get("context", "") or item.get("description", "") or ""
                        if entity and related:
                            results.append({
                                "seed_entity": entity,
                                "related_entity": related,
                                "relationship": rel,
                                "context": ctx,
                            })
                return results
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: try to parse as generic text response with relationship hints
        results = []
        lines = text.split("\n")
        for line in lines:
            line = line.strip().lstrip("-*#").strip()
            if not line:
                continue
            # Try to extract entity pairs from common patterns
            # Pattern: "Entity A -rel-> Entity B" or "Entity A relates to Entity B"
            match = re.match(
                r"(.+?)\s*(?:->|relates? to|connected? to|associated with|linked to)\s*(.+)",
                line,
                re.IGNORECASE,
            )
            if match:
                results.append({
                    "seed_entity": match.group(1).strip(),
                    "related_entity": match.group(2).strip(),
                    "relationship": "related",
                    "context": line,
                })
        return results

    # 1-hop expansion
    hops_used = 1
    entity_batch = "\n".join(f"- {e}" for e in entities)

    batch_results = await _query_graph_via_bypass(entity_batch, "1hop")
    for neighbor in batch_results:
        if _add_neighbor(
            neighbor.get("seed_entity", entities[0]),
            neighbor.get("related_entity", ""),
            neighbor.get("relationship", ""),
            neighbor.get("context", ""),
        ):
            # Budget exhausted after this addition
            break

    # 2-hop expansion (only if hops >= 2 and budget remains)
    if hops >= 2:
        remaining_budget = max_neighbors - len(all_neighbors)
        if remaining_budget > 0:
            # Get 1-hop neighbors' related entities for 2-hop expansion
            hop1_related = []
            for n in all_neighbors:
                rel = n.get("related_entity", "")
                if rel and rel not in entities:
                    hop1_related.append(rel)
            # Limit 2-hop entities to prevent explosion
            hop1_related = hop1_related[:min(remaining_budget, 5)]

            if hop1_related:
                hop2_batch = await _query_graph_via_bypass(
                    "\n".join(f"- {e}" for e in hop1_related), "2hop"
                )
                for neighbor in hop2_batch:
                    if _add_neighbor(
                        neighbor.get("seed_entity", hop1_related[0]),
                        neighbor.get("related_entity", ""),
                        neighbor.get("relationship", ""),
                        neighbor.get("context", ""),
                    ):
                        break
                # Update hops_used if 2-hop produced results or was attempted
                if hop2_batch or len(all_neighbors) > 1:
                    hops_used = 2

    return all_neighbors, error, hops_used


# ── merge_graph_expansion ───────────────────────────────────────────────────

def merge_graph_expansion(
    base_citations: list[dict[str, Any]],
    graph_neighbors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge graph-derived pseudo-citations into base citations.

    Graph neighbors become pseudo-citations with structure derived from
    the neighbor data. Deduplication follows the same stable-order rules
    as merge_and_dedupe_citations (path-first, content-fallback).

    Returns a merged list with base citations first, then graph pseudo-citations.
    """
    if not graph_neighbors:
        return list(base_citations)

    pseudo_citations: list[dict[str, Any]] = []
    for neighbor in graph_neighbors:
        seed = neighbor.get("seed_entity", "")
        related = neighbor.get("related_entity", "")
        relationship = neighbor.get("relationship", "")
        context = neighbor.get("context", "")

        # Build a pseudo-citation from graph data
        pseudo = {
            "type": "graph_expansion",
            "seed_entity": seed,
            "related_entity": related,
            "relationship": relationship,
            "content": f"[Graph expansion: {seed} --{relationship}--> {related}] {context}".strip(),
            "path": f"graph://{seed}/{related}",
        }
        pseudo_citations.append(pseudo)

    if not pseudo_citations:
        return list(base_citations)

    # Merge using the same dedupe logic as multi_query
    from .multi_query import merge_and_dedupe_citations
    return merge_and_dedupe_citations([base_citations, pseudo_citations])


# ── compute_adaptive_hops ───────────────────────────────────────────────────

def compute_adaptive_hops(
    graph_neighbor_count: int,
    scoped_citation_count: int,
    query_word_count: int,
    is_transcript_like: bool,
    rerank_top_score: float,
    configured_hops: int = DEFAULT_HOPS,
) -> int:
    """Compute the adaptive hop count based on weak-evidence signals.

    Default is 1 hop. Escalates to 2 hops if ANY of these conditions hold:
    1. graph_neighbor_count after 1-hop < 4
    2. scoped_citation_count after merge < 3
    3. complex query (>=20 words OR transcript-like) AND rerank top score < 0.15

    configured_hops acts as a ceiling: if set to 1, never escalates.
    Returns 1 or 2 (never exceeds configured_hops ceiling).
    """
    if configured_hops < 2:
        return 1

    # Trigger 1: Not enough graph neighbors from 1-hop
    if graph_neighbor_count < 4:
        return 2

    # Trigger 2: Too few scoped citations after merge
    if scoped_citation_count < 3:
        return 2

    # Trigger 3: Complex query with low rerank confidence
    is_complex = query_word_count >= 20 or is_transcript_like
    if is_complex and rerank_top_score < 0.15:
        return 2

    return 1


# ── build_graph_metadata ────────────────────────────────────────────────────

def build_graph_metadata(
    enabled: bool,
    applied: bool,
    error: str | None = None,
    seed_count: int = 0,
    neighbor_count: int = 0,
    hops_used: int = 0,
) -> dict[str, Any]:
    """Build graph expansion metadata dict for query_scope.

    All fields are present regardless of whether expansion was applied,
    to ensure consistent observability.
    """
    meta: dict[str, Any] = {
        "graph_expansion_enabled": enabled,
        "graph_expansion_applied": applied,
        "graph_expansion_error": error,
        "graph_seed_count": seed_count,
        "graph_neighbor_count": neighbor_count,
        "graph_hops_used": hops_used,
    }
    return meta
