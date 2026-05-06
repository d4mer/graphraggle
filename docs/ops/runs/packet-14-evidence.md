# Packet 14 Evidence

## Changed Files

1. `app/config.py` — Added `multi_query_enabled` (bool, default False), `multi_query_long_query_words` (int, default 20), `multi_query_rewrite_count` (int, default 2), `multi_query_transcript_keywords` (str, default "transcript,workshop,speaker,meeting minutes,recording") settings
2. `app/multi_query.py` — New module with pure multi-query helper functions (`should_trigger_multi_query`, `parse_keywords_csv`, `has_weak_answer_signal`, `generate_rewrites_via_bypass`, `merge_and_dedupe_citations`, `build_multi_query_metadata`, `_normalize_citation_path`, constants `DEFAULT_TRANSCRIPT_KEYWORDS`, `WEAK_ANSWER_MARKERS`)
3. `app/api.py` — Import multi-query helpers, wire multi-query into `/query` endpoint: first-pass retrieval, trigger decision, rewrite generation via bypass mode, per-rewrite retrieval, stable merge + dedupe, then scope -> rerank -> top-5, fail-open with metadata
4. `tests/test_multi_query.py` — 42 unit tests covering all acceptance criteria
5. `docs/packets/14-multi-query-expansion.md` — Packet documentation
6. `CHANGELOG.md` — Packet 14 entry
7. `docs/ops/runs/packet-14-evidence.md` — This evidence file

## Commands Run

```bash
# Syntax check
python3 -m py_compile app/api.py app/config.py app/multi_query.py

# Run all tests
python3 -m unittest discover -s tests -p "test_*.py" -v
```

## Observed Outputs

```
Ran 97 tests in 0.347s
OK
```

All 97 tests passed:

**Multi-query tests (42 tests):**
- `TestParseKeywordsCsv` (7 tests): comma-separated parsing, whitespace stripping, lowercasing, empty exclusion, multi-word keywords, default keywords
- `TestShouldTriggerMultiQuery` (12 tests): long query, short query, transcript keyword, case insensitive, empty/whitespace query, weak signal, boundary word counts, multi-word keyword, no-trigger
- `TestHasWeakAnswerSignal` (4 tests): weak marker detection, case insensitive, good answers, empty
- `TestMergeAndDedupeCitations` (12 tests): stable merge order, path dedupe, content fallback, no dedupe for different paths/content, empty groups, path normalization, field extraction, three-group order, metadata preservation
- `TestGenerateRewritesViaBypass` (8 tests): JSON array parsing, newline parsing, original query filtering, rewrite count limit, malformed JSON fallback, HTTP error, empty response, None response, bypass mode verification
- `TestBuildMultiQueryMetadata` (4 tests): all 7 fields present when triggered/not-triggered, error field, zero counts
- `TestFailOpenBehavior` (3 tests): empty rewrites single-query path, partial failure preserves what was got, fail-open metadata set
- `TestNormalizeCitationPath` (6 tests): lowercasing, whitespace stripping, dot segments, double-dot segments, None/empty handling, complex paths
- `TestIntegrationQueryScopeMetadata` (5 tests): all fields present, types correct, dedupe count reflection, rewrite count config, default count
- `TestIntegrationMergeFlow` (2 tests): realistic three-group merge, field preservation
- `TestConfigSettings` (6 tests): all 4 config defaults, env var overrides

**Reranking tests (25 tests):** All still pass (Packet 13 tests unchanged)

## Risks Remaining

1. **LLM dependency for rewrites**: Rewrite generation calls LightRAG `/query` in bypass mode. If the LLM is slow, unresponsive, or returns consistently malformed output, the fail-open path ensures the query still returns results but with single-query recall.
2. **Latency**: Multi-query adds up to 2 additional retrieval passes (original + 2 rewrites). Each pass is bounded by `REQUEST_TIMEOUT_SECONDS`. With `MULTI_QUERY_REWRITE_COUNT=2`, worst case is 3x the retrieval time before merge.
3. **Citation explosion before dedupe**: Three queries could produce many overlapping citations. The top-5 truncation after rerank bounds the final output, but the intermediate merge step could be memory-intensive for very large result sets.
4. **Bypass mode prompt quality**: The rewrite prompt is a simple text prompt. LLM output quality for rewrite generation depends on the downstream model's ability to produce useful query variants.

## Rollback Note

To rollback Packet 14:
1. Remove `app/multi_query.py`
2. Remove the multi-query import and wire from `app/api.py` (restore pre-Packet-14 query endpoint)
3. Remove `multi_query_enabled`, `multi_query_long_query_words`, `multi_query_rewrite_count`, `multi_query_transcript_keywords` from `app/config.py`
4. Remove `tests/test_multi_query.py`
5. Revert `CHANGELOG.md` entry
6. Remove `docs/packets/14-multi-query-expansion.md`

All changes are additive (feature flag defaults to off), so disabling `MULTI_QUERY_ENABLED` effectively rolls back behavior without code changes.
