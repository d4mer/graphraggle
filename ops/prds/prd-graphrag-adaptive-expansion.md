## Problem Statement

The current retrieval pipeline is strong for direct semantic matches, but mission-critical questions can still miss connected context that lives in related nodes/documents. For high-stakes operational questions, this can reduce grounded-answer quality when relevant evidence is one relationship away from directly retrieved citations.

## Solution

Add adaptive GraphRAG expansion to the existing query pipeline so the system can enrich retrieved evidence with graph neighbors when first-pass evidence is weak. The user still interacts with the same `/query` API, but answers gain relationship-aware context while preserving safety, bounded latency, and fail-open reliability.

## User Stories

1. As an operator, I want the system to include related graph evidence for weak first-pass retrieval, so that answers are more complete and grounded.
2. As an operator, I want graph expansion to be bounded, so that response latency remains predictable.
3. As an operator, I want GraphRAG behind a feature flag, so that rollout and rollback are low risk.
4. As an operator, I want graph expansion to fail open, so that query availability is preserved if graph services fail.
5. As an operator, I want metadata explaining whether graph expansion was applied, so that I can debug and trust results.
6. As an operator, I want LLM-based entity extraction from top citations, so that seed quality is high for mission-critical use.
7. As a tenant-scoped user, I want company filtering and rerank safeguards preserved after graph enrichment, so that scoping behavior remains consistent.
8. As an evaluator, I want measurable quality/latency gates for GraphRAG, so that production enablement is evidence-based.
9. As a maintainer, I want deterministic adaptive triggers for 2-hop escalation, so that behavior is testable and auditable.
10. As a maintainer, I want strict neighbor caps and timeouts, so that graph expansion cannot explode prompt/citation size.

## Implementation Decisions

1. Graph expansion seeds are extracted from top retrieved citations only.
2. Entity extraction uses LLM-based extraction (mission-critical requirement), scoped to top 3 citations.
3. Adaptive hop policy:
   - default 1 hop,
   - escalate to 2 hops only when weak-evidence triggers fire.
4. Weak-evidence triggers for 2-hop escalation:
   - graph neighbors after 1-hop < 4, or
   - scoped citation count after merge < 3, or
   - complex query (>=20 words or transcript-like) and low rerank top confidence (<0.15).
5. Expansion caps:
   - max 10 neighbors total per query,
   - 2-hop can only use remaining neighbor budget,
   - bounded timeout per hop.
6. Pipeline placement:
   - retrieval merge (single/multi-query) -> graph expansion -> company scope filter -> rerank -> top-5 citations.
7. Reliability posture:
   - fail open on graph extraction/expansion errors,
   - preserve existing non-graph path output.
8. Feature control:
   - dedicated flag `GRAPH_EXPANSION_ENABLED` (default false),
   - tunable config for hops and neighbor limits.
9. Query observability metadata fields:
   - `graph_expansion_enabled`
   - `graph_expansion_applied`
   - `graph_expansion_error`
   - `graph_seed_count`
   - `graph_neighbor_count`
   - `graph_hops_used`
10. Production gate targets (vs Packet 15 baseline):
   - grounded-quality lift >= 5%
   - p95 latency increase <= 25%
   - parse/rubric failure increase <= 3%

## Testing Decisions

1. Tests validate external behavior through query-flow semantics and metadata, not internal implementation details.
2. Graph module tests cover:
   - LLM entity extraction parse success/fail-open,
   - 1-hop and adaptive 2-hop trigger behavior,
   - neighbor budget/cap enforcement,
   - stable merge + dedupe behavior,
   - metadata correctness.
3. API-flow tests cover:
   - placement before scope/rerank,
   - fail-open preservation of baseline retrieval path,
   - compatibility with multi-query and rerank toggles.
4. Eval-gate tests validate threshold, borderline, and rollback recommendation behavior against Packet 15 harness.

## Out of Scope

1. Full graph schema redesign.
2. Unbounded multi-hop traversal beyond adaptive 2-hop policy.
3. Agentic retrieval orchestration.
4. Automatic baseline promotion.
5. UI changes in Open WebUI or LightRAG Web UI.

## Further Notes

1. Adaptive thresholds are intentionally conservative for first rollout and can be tuned after evaluation data is collected.
2. LLM extraction quality and graph endpoint reliability should be monitored via query metadata and Packet 15 eval reports.
3. If GraphRAG fails production gates, rollback is feature-flag based (disable graph expansion and rerun gate).
