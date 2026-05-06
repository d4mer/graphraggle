# Packet 16: GraphRAG Adaptive Expansion

## Status: accepted

## Summary

Added adaptive GraphRAG expansion to the query pipeline so the system can enrich retrieved evidence with graph neighbors when first-pass evidence is weak. The feature is behind a feature flag (`GRAPH_EXPANSION_ENABLED`, default `false`) and fails open on any graph extraction or expansion error.

## Locked Requirements

1. **Feature flag** — `GRAPH_EXPANSION_ENABLED` defaults to `false`.
2. **LLM entity extraction** — Entities extracted from top 3 citations only via `/query` mode=bypass with strict JSON-array prompt.
3. **Adaptive hops** — Default 1 hop; escalate to 2 hops if any:
   - graph neighbors after 1-hop < 4, OR
   - scoped citation count after merge < 3, OR
   - complex query (>=20 words OR transcript-like) AND rerank top score < 0.15
4. **Max neighbors** — 10 total; 2-hop uses remaining budget only.
5. **Placement** — After base retrieval merge (single or multi-query), before company scope filter.
6. **Fail-open** — Any graph extraction/expansion failure preserves original merged citations.
7. **query_scope fields** — `graph_expansion_enabled`, `graph_expansion_applied`, `graph_expansion_error`, `graph_seed_count`, `graph_neighbor_count`, `graph_hops_used`.
8. **Preserved behavior** — Existing multi-query/rerank/fallback behavior unchanged.

## Files Changed

1. `app/config.py` — Added `GRAPH_EXPANSION_ENABLED` (bool, default False), `GRAPH_EXPANSION_HOPS` (int, default 1), `GRAPH_EXPANSION_MAX_NEIGHBORS` (int, default 10), `GRAPH_SEED_CITATION_COUNT` (int, default 3).
2. `app/graphrag.py` — New deep module with pure graph helper functions: `citation_to_text_for_entity_extraction`, `extract_entities_via_llm`, `expand_graph_neighbors`, `merge_graph_expansion`, `compute_adaptive_hops`, `build_graph_metadata`.
3. `app/api.py` — Import graphrag helpers, wire graph expansion into `/query` endpoint between merged_citations and scope filter, fail-open, metadata always populated.
4. `tests/test_graphrag.py` — 47 unit tests covering entity extraction, adaptive hops, neighbor caps, merge/dedupe, metadata, config defaults, and integration placement.
5. `docs/packets/16-graphrag-retrieval-expansion.md` — This packet documentation.
6. `docs/ops/runs/packet-16-evidence.md` — Evidence file.
7. `CHANGELOG.md` — Packet 16 entry.

## Pipeline Placement

```
1. execute_query_pass(hybrid/naive/etc.)  -> all_citations
2. Multi-query trigger? -> generate_rewrites -> retrieve -> merge_and_dedupe_citations
3. Fallback (weak signal + non-bypass) -> execute_query_pass(naive) -> re-merge
4. ★ GraphRAG expansion (if enabled): entity extraction -> 1-hop -> adaptive 2-hop -> merge
5. scope_query_citations(merged_citations, requested_company)  -> scoped_citations
6. rerank_citations(scoped_citations, ...)  -> reranked_citations (fail-open)
7. truncate_citations(reranked_citations, top_k=5)  -> final_citations
8. Build query_scope metadata (scope + rerank + multi-query + graph)
9. Return {answer, citations: final_citations, query_scope}
```

## Design Decisions

1. **Entity extraction via LLM bypass** — Uses LightRAG `/query` in bypass mode with a strict JSON-array prompt. Robust parsing: JSON array first, newline-separated fallback, always returns [] on failure.
2. **Graph neighbor expansion via bypass** — Best-effort generic POST to `/query` in bypass mode. Parses JSON array of neighbor objects, falls back to text pattern matching. Returns error string on failure.
3. **Deterministic dedup** — Neighbors deduped by (entity, related_entity, relationship) tuple (case-insensitive). Pseudo-citations deduped by path-first, content-fallback (same as multi-query merge).
4. **Adaptive hop escalation** — Conservative thresholds: neighbors < 4, scoped citations < 3, complex query + low rerank score < 0.15. Only escalates to 2 hops when configured_hops >= 2.
5. **Neighbor budget enforcement** — Max 10 total. 2-hop gets remaining budget (10 - 1-hop count). Budget-exhausted trim is deterministic (first-come, first-served).
6. **Fail-open everywhere** — Entity extraction returns [] on failure. Graph expansion returns error string. API-level try/except preserves merged_citations unchanged.
7. **Metadata always populated** — build_graph_metadata returns all 6 fields regardless of enabled/applied state.

## Testing

- 47 unit tests in `tests/test_graphrag.py` covering:
  - `citation_to_text_for_entity_extraction` (7 tests)
  - `extract_entities_via_llm` (11 tests)
  - `expand_graph_neighbors` (11 tests)
  - `merge_graph_expansion` (6 tests)
  - `compute_adaptive_hops` (14 tests)
  - `build_graph_metadata` (5 tests)
  - Feature flag / config defaults (8 tests)
  - Integration flow placement (6 tests)

## Validation

```bash
python3 -m py_compile app/api.py app/config.py app/graphrag.py
python3 -m unittest discover -s tests -p "test_*.py"
```

## Rollback

Disable `GRAPH_EXPANSION_ENABLED=false` (default) and restart. No data changes, no schema changes.
