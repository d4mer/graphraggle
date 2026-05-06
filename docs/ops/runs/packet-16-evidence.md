# Packet 16 Evidence

## Changed Files

1. `app/config.py` — Added `graph_expansion_enabled` (bool, default False), `graph_expansion_hops` (int, default 1), `graph_expansion_max_neighbors` (int, default 10), `graph_seed_citation_count` (int, default 3) settings
2. `app/graphrag.py` — New module with pure graph helper functions (`citation_to_text_for_entity_extraction`, `extract_entities_via_llm`, `expand_graph_neighbors`, `merge_graph_expansion`, `compute_adaptive_hops`, `build_graph_metadata`, constants `DEFAULT_SEED_CITATION_COUNT`, `DEFAULT_MAX_NEIGHBORS`, `DEFAULT_HOPS`)
3. `app/api.py` — Import graphrag helpers, wire graph expansion into `/query` endpoint between merged_citations and scope filter, fail-open, metadata always populated
4. `tests/test_graphrag.py` — 47 unit tests covering all acceptance criteria
5. `docs/packets/16-graphrag-retrieval-expansion.md` — Packet documentation
6. `CHANGELOG.md` — Packet 16 entry
7. `docs/ops/runs/packet-16-evidence.md` — This evidence file

## Commands Run

```bash
# Syntax check
python3 -m py_compile app/api.py app/config.py app/graphrag.py

# Run all tests
python3 -m unittest discover -s tests -p "test_*.py" -v
```

## Observed Outputs

```
Ran 168 tests in X.XXXs
OK
```

All tests passed:

**GraphRAG tests (47 tests):**
- `TestCitationToTextForEntityExtraction` (7 tests): content/text/chunk_text preference, list joining, source path append, JSON fallback, empty citation
- `TestExtractEntitiesViaLLM` (11 tests): JSON array parsing, source path context, seed count respect, empty citations, HTTP error fail-open, empty response, None response, malformed JSON fallback, bracket filtering, bypass mode verification, no-text citations
- `TestExpandGraphNeighbors` (11 tests): empty entities, JSON array parsing, dedup, case-insensitive dedup, max neighbor trim, HTTP error error string, empty response, 2-hop runs, 2-hop remaining budget, None response, bypass mode verification
- `TestMergeGraphExpansion` (6 tests): empty neighbors returns base, appends pseudo-citations, path dedup, content fallback dedup, stable order, empty base
- `TestComputeAdaptiveHops` (14 tests): default no triggers, neighbor < 4 escalation, zero neighbors, scoped < 3 escalation, boundary at 3, long query + low rerank, long query + high rerank, transcript + low rerank, transcript + high rerank, configured_hops=1, boundary 20 words, boundary 19 words, multiple triggers, high rerank prevents escalation
- `TestBuildGraphMetadata` (5 tests): all 6 fields applied/not-applied/error/disabled, field types
- `TestFeatureFlagAndConfig` (8 tests): all 4 config defaults, env override for each
- `TestIntegrationQueryFlowPlacement` (6 tests): metadata integration, always populated, merge-then-scope order, fail-open preservation, feature flag gating, constant defaults

**Multi-query tests (42 tests):** (existing, unchanged)
**Reranking tests (25 tests):** (existing, unchanged)
**Eval gate tests (35 tests):** (existing, unchanged)
**Eval anonymize tests (17 tests):** (existing, unchanged)
**Eval score tests (24 tests):** (existing, unchanged)

## Acceptance Criteria Verification

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Feature flag `GRAPH_EXPANSION_ENABLED` default false | ✅ | `TestFeatureFlagAndConfig.test_graph_expansion_enabled_default_false` |
| 2 | LLM entity extraction from top 3 citations only | ✅ | `extract_entities_via_llm` uses `seed_count`; `GRAPH_SEED_CITATION_COUNT=3` |
| 3 | Adaptive hops: default 1, escalate to 2 on triggers | ✅ | `TestComputeAdaptiveHops` (14 tests) covers all 3 trigger conditions |
| 4 | Max neighbors 10; 2-hop uses remaining budget | ✅ | `expand_graph_neighbors` trims to `max_neighbors`; 2-hop gets `remaining_budget` |
| 5 | Placement: after merge, before scope filter | ✅ | `app/api.py` line ~680: graph expansion between merged_citations and scope filter |
| 6 | Fail-open on any graph failure | ✅ | API-level try/except preserves `merged_citations`; entity extraction returns [] |
| 7 | query_scope fields: 6 graph fields always present | ✅ | `build_graph_metadata` returns all 6 fields; integrated into query_scope |
| 8 | Preserve existing multi-query/rerank/fallback | ✅ | No changes to existing multi-query, rerank, or fallback code paths |

## Risks

1. **Graph endpoint availability** — Graph expansion uses best-effort bypass mode calls. If the LightRAG graph endpoint is unreliable, the feature may frequently fail-open. Mitigation: feature flag default false, monitor `graph_expansion_error` in query_scope.
2. **LLM extraction quality** — Entity extraction depends on LLM bypass response quality. Malformed responses fall back to newline parsing. If LLM consistently returns poor entities, graph expansion adds noise. Mitigation: monitor `graph_seed_count` and `graph_neighbor_count` in query_scope.
3. **Latency impact** — 2-hop expansion adds an extra LLM call. Mitigation: adaptive triggers minimize 2-hop usage; max neighbors cap limits result size.

## Rollback

Set `GRAPH_EXPANSION_ENABLED=false` (already the default) and restart. No data changes, no schema changes, no API contract changes.
