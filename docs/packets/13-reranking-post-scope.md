# Packet 13: Reranking Post-Scoping

## Status: accepted

## Objective

Improve grounded-answer quality by reranking in-scope citations returned from LightRAG retrieval, returning only the top 5 most relevant citations to the LLM.

## Why This Packet Exists

Citations returned from LightRAG retrieval are ordered by the upstream retrieval strategy (hybrid/local/global). This order may not always align with what the downstream LLM needs for the best grounded answer. A lightweight reranking step, applied after company-scope filtering, can reorder citations to maximize relevance for the given query before the answer is constructed.

## In Scope

1. Add config settings: `RERANK_ENABLED` (bool, default false) and `RERANK_BINDING_HOST` (optional URL).
2. Add a pure helper function to call a rerank endpoint (POST with query + documents) using httpx.
3. Apply reranking only to in-scope citations (after `scope_query_citations`).
4. Fail-open: if rerank errors/unavailable, preserve original citation order and continue.
5. Truncate citations to top 5 after rerank (or current order when disabled/fail).
6. Keep existing transcript-like `top_k` and fallback behavior unchanged.
7. Ensure fallback evaluation uses post-rerank citation count.
8. Add query_scope fields: `rerank_enabled`, `rerank_applied`, `rerank_error`, `rerank_input_count`, `rerank_output_count`.
9. TDD: unittest-style tests covering all behaviors.
10. Update CHANGELOG.md and create evidence doc.

## Out Of Scope

1. Remote deployment or release-gate automation (docs-only gates).
2. Changing the LightRAG upstream retrieval strategy.
3. Persistent storage of rerank rankings.
4. Reranking outside the gateway `/query` endpoint.
5. Model-specific reranker configuration beyond host URL.

## Dependencies

- Packet 07: Company Attribution And Retrieval Scoping (citation filtering must happen before rerank).
- Packet 12: Retrieval Usability Hotfix (fallback behavior must be preserved).

## Required Decisions

1. **Feature flag**: `RERANK_ENABLED` defaults to `false` (opt-in).
2. **Fail-open**: rerank errors never break the query path — preserve original order.
3. **Top-K after rerank**: always return at most 5 citations after rerank.
4. **Rerank endpoint contract**: POST JSON `{ "query": str, "documents": [str, ...] }` -> JSON `{ "results": [ { "index": int, "relevance_score": float }, ... ] }` sorted by score descending.
5. **Rerank host**: configurable via `RERANK_BINDING_HOST`; if absent, rerank is skipped.

## Implementation Guidance

1. Extract pure helper functions from `app/api.py` to enable testability:
   - `rerank_citations(query: str, citations: list[dict], rerank_host: str | None) -> tuple[list[dict], dict]` — pure-ish function that returns (sorted_citations, metadata).
   - `_apply_rerank(citations, query, rerank_host)` — internal async helper.
2. In the `/query` endpoint, after `scope_query_citations()`, call the rerank helper.
3. Always truncate to top 5 after reranking.
4. Append rerank metadata to `query_scope`.
5. If rerank is disabled (`RERANK_ENABLED=False`) or host is absent, skip rerank entirely — no metadata impact beyond `rerank_enabled=false`.

## Expected Deliverables

1. `docs/packets/13-reranking-post-scope.md` — packet doc
2. `app/config.py` — new config fields
3. `app/api.py` — rerank logic in `/query` + extracted helpers
4. `tests/test_reranking.py` — unit tests
5. `docs/ops/runs/packet-13-evidence.md` — evidence
6. `CHANGELOG.md` — entry for Packet 13

## Acceptance Criteria

1. When `RERANK_ENABLED=true` and `RERANK_BINDING_HOST` is set, citations are reranked and top 5 returned.
2. When `RERANK_ENABLED=false`, original citation order is preserved.
3. When rerank endpoint errors, original citation order is preserved (fail-open).
4. `query_scope` includes all five rerank metadata fields.
5. Out-of-scope citations (filtered by company) do not influence rerank results.
6. All tests pass: `python -m unittest discover -s tests -p "test_*.py"`.
7. Top 5 truncation is enforced in all code paths.

## Verification Steps

1. Run tests: `python -m unittest discover -s tests -p "test_*.py"`
2. Syntax check: `python3 -m py_compile app/api.py app/config.py`

## Evidence Required

1. Changed files list
2. Commands run with outputs
3. Test results
4. Risks remaining
5. Rollback note

## Review Checklist

- [ ] Scope stayed within packet boundaries
- [ ] Acceptance criteria are satisfied
- [ ] Docs and operator guidance were updated
- [ ] No secrets were committed
- [ ] Test coverage for all acceptance criteria

## Risks / Open Questions

1. Rerank endpoint availability — must fail-open gracefully.
2. Latency impact — rerank adds an extra HTTP call; must be bounded by timeout.
3. No existing rerank endpoint exists in the stack yet — the implementation calls a hypothetical endpoint; release gate is docs-only.

## Execution Notes

- Extracted rerank logic into `app/rerank.py` (deep module) to avoid pulling in gateway dependency chain during testing.
- `app/rerank.py` has no dependency on auth, config, or state_store — only `httpx`.
- Python 3.8 compatibility: added `from __future__ import annotations` and used `Optional[str]` instead of `str | None` in config.
- Test mocking required a custom `_MockAsyncHttpClient` class because `AsyncMock` in Python 3.8 does not correctly handle `async with` context manager patterns.
- `MagicMock` is callable, so `_setup_httpx_mock` must check `isinstance(mock, MagicMock)` before checking `callable()`.

## Checklist
- [x] Create packet file
- [x] Implement reranking in gateway
- [x] Add config settings (RERANK_ENABLED, RERANK_BINDING_HOST)
- [x] Extract pure helper functions to app/rerank.py
- [x] Apply reranking after company scope filtering
- [x] Fail-open behavior
- [x] Top-5 truncation
- [x] Keep existing transcript-like top_k and fallback behavior
- [x] query_scope fields: rerank_enabled, rerank_applied, rerank_error, rerank_input_count, rerank_output_count
- [x] TDD: 25 tests covering all acceptance criteria
- [x] Update CHANGELOG.md
- [x] Write evidence file
- [x] All tests pass: `python3 -m unittest discover -s tests -p "test_*.py"`

## Result: accepted. All 25 tests pass. Implementation complete.
