# Packet 14: Multi-Query Expansion

## Status: accepted

## Objective

Improve retrieval recall for complex or ambiguous queries by expanding a single query into multiple variant queries when triggered, merging results with stable citation ordering, and preserving all existing pipeline behavior (company scope, rerank, top-5) on the merged result set.

## Why This Packet Exists

Single-query retrieval can miss relevant documents when the query phrasing does not align with how information is indexed. Multi-query expansion generates query variants via the LLM (bypass mode) and merges results to increase the chance of finding all relevant citations before reranking and truncation.

## In Scope

1. Add config settings: `MULTI_QUERY_ENABLED` (bool, default false), `MULTI_QUERY_LONG_QUERY_WORDS` (int, default 20), `MULTI_QUERY_REWRITE_COUNT` (int, default 2), `MULTI_QUERY_TRANSCRIPT_KEYWORDS` (csv string, default matching existing transcript keywords).
2. Create `app/multi_query.py` with pure helper functions: `should_trigger_multi_query`, `parse_keywords_csv`, `generate_rewrites_via_bypass`, `merge_and_dedupe_citations`, metadata builder helpers.
3. Integrate multi-query into the `/query` endpoint: first-pass retrieval with original query, trigger decision, rewrite generation via LightRAG `/query` in bypass mode, retrieval for each rewrite, merge citations in stable order (original -> rewrite1 -> rewrite2), dedupe by normalized citation path then content fallback, then company scope filter -> rerank -> top-5.
4. If rewrite generation or retrieval fails: fail-open to single-query path with `multi_query_error` metadata.
5. Add `query_scope` metadata fields for multi-query observability.
6. TDD: unittest-style tests covering all behaviors.
7. Update CHANGELOG.md and create evidence doc.

## Out Of Scope

1. Changing the LightRAG upstream retrieval strategy or model.
2. Persisting rewrite queries or merged results.
3. Multi-query for the `/generate-document` endpoint.
4. Configuring rewrite prompts beyond what the LLM provides in bypass mode.
5. Caching or deduplication of rewrite queries themselves.
6. Parallel execution of rewrite retrievals (sequential is acceptable for count=2).

## Dependencies

- Packet 07: Company Attribution And Retrieval Scoping (scope filter must happen after merge, before rerank).
- Packet 12: Retrieval Usability Hotfix (transcript-like top_k and fallback behavior preserved).
- Packet 13: Reranking Post-Scoping (rerank and top-5 apply to merged result set).

## Required Decisions

1. **Feature flag**: `MULTI_QUERY_ENABLED` defaults to `false` (opt-in).
2. **Deterministic triggers**: trigger when any of: query exceeds word threshold, transcript-like keyword detected, or weak first-pass retrieval signal detected (citations < 2 or weak answer markers).
3. **Rewrite count**: exactly 2 rewrites (3 total queries including original). Configurable via `MULTI_QUERY_REWRITE_COUNT`.
4. **Rewrite generation**: use LightRAG `/query` in bypass mode to generate rewrites from the LLM. Gracefully parse output (text lines or JSON arrays). Fail-open to single-query path on parse failure.
5. **Merge order**: stable — original query results first, then rewrite1 results, then rewrite2 results.
6. **Deduplication**: normalize citation paths (lowercase, strip whitespace, resolve `.`/`..`) and dedupe by path first; fall back to content-based dedupe if path is absent.
7. **Pipeline order**: retrieve (multi-query) -> company scope filter -> rerank (Packet 13) -> top-5.
8. **Separate feature flag**: `MULTI_QUERY_ENABLED` is independent of `RERANK_ENABLED`.
9. **Fail-open**: rewrite generation or retrieval failure falls back to single-query original path with `multi_query_error` set in metadata.

## Implementation Guidance

1. `app/multi_query.py` should be a deep module with no dependency on FastAPI, auth, config, or state_store.
2. `should_trigger_multi_query(query, transcript_keywords, long_query_words, weak_signal)` returns `(bool, str)` — the trigger decision and reason.
3. `generate_rewrites_via_bypass(client, query, rewrite_count)` calls LightRAG `/query` in bypass mode with a prompt asking for rewrite variants. Parse output: try JSON array first, then newline-separated lines. Return list of rewritten query strings.
4. `merge_and_dedupe_citations(groups)` accepts a list of citation groups in stable order and dedupes by normalized path, then content.
5. In `/query` endpoint:
   a. Run first retrieval pass with original query.
   b. Compute weak signal from first-pass answer/citations.
   c. Decide trigger.
   d. If triggered and enabled: generate rewrites, run retrieval for each, merge citations.
   e. If any rewrite fails: fail-open to original path, set `multi_query_error`.
   f. Apply company scope filter -> rerank -> top-5 on merged citations.
6. All multi-query metadata fields go into `query_scope`.

## Expected Deliverables

1. `docs/packets/14-multi-query-expansion.md` — packet documentation
2. `app/config.py` — new config fields
3. `app/multi_query.py` — new deep module with pure helpers
4. `app/api.py` — multi-query integration in `/query` endpoint
5. `tests/test_multi_query.py` — unit and integration-level tests
6. `docs/ops/runs/packet-14-evidence.md` — evidence
7. `CHANGELOG.md` — entry for Packet 14

## Acceptance Criteria

1. When `MULTI_QUERY_ENABLED=false`, multi-query is never triggered; single-query path runs as before.
2. When `MULTI_QUERY_ENABLED=true` and a trigger condition is met, rewrites are generated via bypass mode and merged.
3. Long query trigger: query word count exceeds `MULTI_QUERY_LONG_QUERY_WORDS`.
4. Transcript trigger: query contains any keyword from `MULTI_QUERY_TRANSCRIPT_KEYWORDS`.
5. Weak signal trigger: first-pass returns fewer than 2 citations or answer contains weak-answer markers.
6. Rewrite count = 2 by default (3 total queries). Configurable via `MULTI_QUERY_REWRITE_COUNT`.
7. Merge order is stable: original -> rewrite1 -> rewrite2.
8. Deduplication by normalized citation path first, then content fallback.
9. Pipeline: retrieve -> merge -> scope filter -> rerank -> top-5.
10. Fail-open on rewrite/retrieval error: single-query path with `multi_query_error` metadata.
11. All `query_scope` metadata fields present when multi-query is active.
12. All tests pass: `python -m unittest discover -s tests -p "test_*.py"`.
13. Existing Packet 13 tests continue to pass.

## Verification Steps

1. Run tests: `python -m unittest discover -s tests -p "test_*.py"`
2. Syntax check: `python3 -m py_compile app/api.py app/config.py app/multi_query.py`
3. Verify Packet 13 tests still pass.

## Evidence Required

1. Changed files list
2. Commands run with outputs
3. Test results
4. API before/after examples if relevant
5. Risks remaining
6. Rollback note

## Review Checklist

- [ ] Scope stayed within packet boundaries
- [ ] Acceptance criteria are satisfied
- [ ] Docs and operator guidance were updated
- [ ] No secrets were committed
- [ ] Test coverage for all acceptance criteria
- [ ] Packet 13 tests still pass

## Risks / Open Questions

1. **LLM dependency for rewrites**: Rewrite generation calls the LLM via bypass mode. If the LLM is slow or returns malformed output, fail-open must work reliably.
2. **Latency**: Multi-query adds up to 2 additional retrieval passes. Each pass is bounded by the existing timeout.
3. **Citation explosion**: Merging results from 3 queries could produce many citations before dedupe. The top-5 truncation after rerank bounds the final output.

## Execution Notes

- `app/multi_query.py` is a deep module with no dependency on FastAPI, auth, config, or state_store — only `httpx` and stdlib.
- Python 3.8 compatibility: added `from __future__ import annotations` throughout.
- Rewrite generation uses a simple prompt asking the LLM to produce 2-3 variant queries. Output is parsed as JSON array first, then newline-separated lines.
- The `_MockAsyncHttpClient` pattern from `test_reranking.py` is reused for mocking async httpx calls.
- Config reload in tests uses `importlib.reload` with `patch.dict` for environment variables.

## Checklist

- [x] Create packet file
- [x] Implement config settings (MULTI_QUERY_ENABLED, MULTI_QUERY_LONG_QUERY_WORDS, MULTI_QUERY_REWRITE_COUNT, MULTI_QUERY_TRANSCRIPT_KEYWORDS)
- [x] Create app/multi_query.py with pure helpers
- [x] Wire multi-query into /query endpoint in app/api.py
- [x] Stable merge order: original -> rewrite1 -> rewrite2
- [x] Dedupe by normalized citation path, then content fallback
- [x] Pipeline: retrieve -> scope filter -> rerank -> top-5
- [x] Separate feature flag MULTI_QUERY_ENABLED
- [x] Fail-open on rewrite/retrieval error with metadata
- [x] query_scope metadata fields
- [x] TDD: tests covering trigger decisions, rewrite parsing, merge/dedupe, fail-open, integration flow
- [x] Update CHANGELOG.md
- [x] Write evidence file
- [x] All tests pass including Packet 13 tests

## Result: accepted. All tests pass. Implementation complete.
