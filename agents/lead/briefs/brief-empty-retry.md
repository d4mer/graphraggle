# Brief: retry once when the answer stream comes back empty

**Branch:** `packet-23-empty-retry`, created from `packet-21-standalone-query` (NOT from `packet-22-ll-seed`; the seed is not wanted). Do not push. Do not touch `master`.

## Why

On the production host, oMLX sometimes ends the answer stream early: HTTP 200, zero answer words (seen once in 40 requests, after 641 s). `answer_ollama_bridge_direct` in `app/api.py` (the block after `client.proxy("POST", "/query/stream", ...)`, around line 560) then returns `""` and OpenWebUI shows a blank reply. A second attempt almost always works (the re-run of that request returned 523 words).

## What to build

1. `app/config.py`: add `bridge_empty_retries: int = Field(default=1, alias="BRIDGE_EMPTY_RETRIES")` next to the other `bridge_*` settings. `0` disables retrying.
2. `app/api.py`, `answer_ollama_bridge_direct`: after `extract_stream_response_parts`, if the stripped answer is empty AND the request did not raise, re-send the **same** `bridge_payload` to `/query/stream`, up to `settings.bridge_empty_retries` more times, stop at the first non-empty answer. Sequential, never concurrent. Re-run `extract_stream_response_parts` on each response; use the last response's `resp`/`raw_text` for everything after (citations, logging).
   - Do NOT retry on the exception path (that branch stays as is), and do NOT retry when the answer is non-empty but canned (`is_canned_failure`).
   - If still empty after the retries, keep today's behaviour: the honest-failure message when `kw.source == "fallback"`, otherwise `""`.
3. `app/observability.py`: add an integer log field `empty_retries_used` (0 when no retry happened) to the bridge log, with the docstring row. Counts and booleans only; never log text.
4. Check whether the bridge answer cache stores the answer. If it can store an empty answer, make sure an empty final answer is never cached. Report what you found even if no change was needed.

## Tests (new tests must FAIL on the old code)

In `tests/test_ollama_bridge.py`, following the existing fake-client pattern there:
- first response empty, second has an answer: the answer is returned, the client was called twice with identical payloads, `empty_retries_used == 1`.
- empty on every attempt with `BRIDGE_EMPTY_RETRIES=1`: exactly 2 calls, result unchanged from today (`""`, or the honest message on keyword fallback).
- `BRIDGE_EMPTY_RETRIES=0`: one call only.
- non-empty first answer: one call, `empty_retries_used == 0`.
- exception on the first call: not retried.
- canned (non-empty) answer: not retried.
- the log carries only an int for `empty_retries_used`.

## Acceptance

```
export RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621 SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db
.venv/bin/python -m pytest tests/ -q
```
Baseline is 435 passed on `packet-21-standalone-query`; expect those plus your new tests, all green. Show that the new tests fail when `app/api.py` is stashed. Commit in small steps (setting, wiring, tests). Touch only `app/config.py`, `app/api.py`, `app/observability.py` and the two test files. Hand back with `rig queue handoff` when done; do not close the row yourself.
