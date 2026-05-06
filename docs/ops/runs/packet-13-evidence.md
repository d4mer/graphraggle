# Packet 13 Evidence

## Changed Files

1. `app/config.py` — Added `rerank_enabled` (bool, default False) and `rerank_binding_host` (Optional[str], default None) settings
2. `app/rerank.py` — New module with pure rerank helper functions (`citation_to_text`, `call_rerank_endpoint`, `rerank_citations`, `truncate_citations`, constants `RERANK_TOP_K`, `RERANK_TIMEOUT_SECONDS`)
3. `app/api.py` — Import rerank helpers, wire rerank into `/query` endpoint after company scoping, add rerank metadata to `query_scope`
4. `tests/__init__.py` — New test package init
5. `tests/test_reranking.py` — 25 unit tests covering all acceptance criteria
6. `docs/packets/13-reranking-post-scope.md` — Packet documentation
7. `CHANGELOG.md` — Packet 13 entry
8. `docs/ops/runs/packet-13-evidence.md` — This evidence file

## Commands Run

```bash
# Syntax check
python3 -m py_compile app/api.py app/rerank.py app/config.py

# Run tests
python3 -m unittest discover -s tests -p "test_*.py" -v
```

## Observed Outputs

```
Ran 25 tests in 0.174s
OK
```

All 25 tests passed:
- `TestCitationToText` (3 tests): content/text field preference, JSON fallback
- `TestTruncateCitations` (4 tests): top-5 truncation, order preservation, below-top-k, empty list
- `TestRerankCitationsDisabled` (1 test): None host returns original order
- `TestRerankCitationsApplied` (2 tests): reordering by scores, partial results
- `TestRerankCitationsFailOpen` (3 tests): HTTP error, timeout, malformed response
- `TestRerankFeatureFlagOffPreservesOrder` (1 test): disabled flag skips rerank
- `TestTop5Truncation` (2 tests): after rerank, after failure
- `TestCompanyFilterBeforeRerank` (2 tests): out-of-scope not sent to rerank, input count matches scope
- `TestQueryScopeMetadata` (2 tests): all 5 fields present enabled/disabled
- `TestConfigSettings` (2 tests): default values
- `TestIntegrationQueryEndpointFlow` (2 tests): full flow reorder+truncate, full flow fail-open+truncate
- `TestRerankTopKConstant` (1 test): constant value is 5

## Risks Remaining

1. **Rerank endpoint availability**: The implementation calls a hypothetical rerank endpoint at `{RERANK_BINDING_HOST}/rerank`. No such endpoint exists in the stack yet. The release gate is docs-only.
2. **Python 3.8 compatibility**: Existing gateway code (`auth.py`, `models.py`) uses `str | None` without `from __future__ import annotations`. The new `app/rerank.py` module uses `from __future__ import annotations` to avoid this issue.
3. **Latency**: Rerank adds an extra HTTP call bounded by a 10-second timeout.

## Rollback Note

To rollback Packet 13:
1. Remove `app/rerank.py`
2. Remove the rerank import and wire from `app/api.py` (restore pre-Packet-13 query endpoint)
3. Remove `rerank_enabled` and `rerank_binding_host` from `app/config.py`
4. Remove `tests/test_reranking.py` and `tests/__init__.py`
5. Revert `CHANGELOG.md` entry
6. Remove `docs/packets/13-reranking-post-scope.md`

All changes are additive (feature flag defaults to off), so disabling `RERANK_ENABLED` effectively rolls back behavior without code changes.
