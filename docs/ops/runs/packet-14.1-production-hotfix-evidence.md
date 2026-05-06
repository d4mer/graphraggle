# Packet 14.1 Production Hotfix Evidence

## Scope

Production activation hotfix for Packet 14 multi-query and Packet 13 rerank paths.

## Changed Files

1. `app/multi_query.py`
2. `app/rerank.py`
3. `app/config.py`
4. `app/api.py`
5. `tests/test_multi_query.py`
6. `tests/test_reranking.py`
7. `CHANGELOG.md`

## Root Causes

1. Rewrite generation failure: upstream bypass query returned HTTP 500 for `top_k=0`.
2. Rerank fail-open: rerank request contract/auth gaps plus oversized transcript citation payloads.

## Fixes Applied

1. Multi-query rewrite generation now uses `top_k=1`.
2. Rerank gateway path now supports `RERANK_BINDING_API_KEY` and `RERANK_MODEL`.
3. Rerank text extraction now joins list-based `content` and caps text size per citation.

## Verification

Local tests:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

Observed:

1. `Ran 98 tests ... OK`

Production checks (`macmini.local`):

1. `/health` returns `ok: true`.
2. `/query` metadata confirms:
   - `multi_query_enabled: true`
   - `multi_query_triggered: true`
   - `multi_query_rewrite_count: 2`
   - `multi_query_error: null`
   - `rerank_enabled: true`
   - `rerank_applied: true`
   - `rerank_error: null`

## Rollback

1. Disable features via env flags:
   - `MULTI_QUERY_ENABLED=false`
   - `RERANK_ENABLED=false`
2. Rebuild/restart compose stack.
