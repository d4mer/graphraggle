# Brief — seed `ll_keywords` with the user's original wording (GRAG-41 follow-up 1)

**To:** dev-worker@graphraggle   **From:** orch-lead@graphraggle
**Branch:** create `packet-22-ll-seed` from `packet-21-standalone-query` (HEAD `376af5e`).
Do NOT commit to `master` or to `packet-21-standalone-query`. Do NOT push; the lead pushes.
**Tests:** `.venv/bin/python -m pytest tests/ -q` with these env vars exported first
(the suite needs them to import `app.config`):
`RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621 SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db`
**Baseline today: 435 passed.** No live services exist here; everything is offline unit tests.

## Why

The bridge (`app/api.py`, `answer_ollama_bridge_direct`) can rewrite a follow-up into a
standalone question (`app/rewrite.py`). When it does, the rewritten text becomes the
retrieval `query` and the input to the keyword step (`app/keywords.py`), so **the user's
own wording never reaches retrieval** (it only reaches the answer step via `user_prompt`).
In the live comparison one case lost documents: the rewrite narrowed the query and cited
documents dropped (5/8 down to 4/8). Fix: when a rewrite happened, also add the user's
original wording to `ll_keywords`, so entity search still sees what the user typed.
Context: LightRAG joins the supplied keywords and embeds them for entity search; we are
only widening that input, not changing `query`.

## The change (all four parts)

1. **Setting.** In `app/config.py`, next to its neighbours
   (see line ~134, `bridge_rewrite_no_think`), add
   `bridge_rewrite_seed_ll: bool = Field(default=False, alias="BRIDGE_REWRITE_SEED_LL")`
   with a one-line comment. Default **False**: production stays byte-identical until the
   result is measured on the Thinkpad (not your job).
2. **Pure helper.** In `app/keywords.py` add
   `seed_ll_keywords(ll: list, original: str, max_items: int) -> list`:
   - `seed = " ".join(original.split())` (collapse whitespace), truncated to 200 chars.
   - If `seed` is empty, return `list(ll)` unchanged.
   - If `seed.lower()` equals any existing `ll` item (case-insensitive), return `list(ll)`.
   - Otherwise return `ll[:max_items - 1] + [seed]` when `len(ll) >= max_items`, else
     `ll + [seed]`. The seed is always kept and always last; the result never exceeds
     `max_items`. Never mutate the input list. No I/O, never raises.
3. **Wiring.** In `app/api.py`, right after the existing block that sets
   `bridge_payload["ll_keywords"] = kw.ll` (around line 523), add: if
   `settings.bridge_rewrite_seed_ll` and `rw is not None and rw.source == "rewritten"`
   and `kw is not None and kw.source in ("llm", "llm_unwrapped", "fallback")`, then
   `bridge_payload["ll_keywords"] = seed_ll_keywords(kw.ll, prompt, settings.bridge_keyword_max_items)`
   and remember `ll_seeded = True`. `prompt` is the user's original message (the variable
   already used above). Do **not** change `hl_keywords`, `query`, `user_prompt`, the rewrite
   step, the keyword step, or any other behaviour. In every other case the payload must be
   exactly what it is today.
4. **Log field.** Add `rewrite_ll_seeded` (bool, default False) to the bridge log line,
   following exactly how commit `765a4ab` added `rewrite_changed`: the docstring field table
   in `app/observability.py`, the `emit_bridge_log` keyword argument (~line 172) and the
   emitted dict (~line 218), and `_rewrite_log_fields` in `app/api.py` (pass the flag in; the
   `rw is None` branch returns False). The log must contain the boolean only, never the seed
   text or any keyword.

## Tests to add (offline, fake clients; mirror the style of `tests/test_ollama_bridge.py`
around `test_keyword_step_sees_rewritten_query` and `tests/test_keywords.py`)

- `seed_ll_keywords`: appends last; empty/whitespace original returns copy unchanged;
  duplicate (case-insensitive) not added; cap respected when `ll` is full (seed kept, an
  earlier item dropped); long original truncated to 200 chars; input list not mutated.
- Bridge, flag **on** and rewrite `rewritten`: payload `ll_keywords` ends with the original
  wording; `query` is still the rewritten text; `hl_keywords` unchanged.
- Bridge, flag **off**: payload identical to today (`ll_keywords == kw.ll`).
- Flag on but rewrite `unchanged`, `fallback`, `skipped_no_history`, `off`, `error`: no seed.
- Flag on, rewritten, but keyword supply off: no `ll_keywords` at all (no seeding alone).
- Log line contains `rewrite_ll_seeded` true/false and contains none of the seed text
  (assert on a distinctive string).

## Acceptance (the lead will check each)

1. Full suite passes: 435 baseline plus your new tests, zero failures, zero skips added.
2. Your new tests must **fail on the old code**: after finishing, run
   `git stash` of only the `app/` changes (or `git checkout packet-21-standalone-query -- app/`
   in a scratch copy) and show the new bridge tests failing. Report the command and output.
3. Diff touches only: `app/config.py`, `app/keywords.py`, `app/api.py`,
   `app/observability.py`, and test files. Nothing else.
4. Commits are small with clear messages; end each with
   `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` only if the harness
   reminder tells you to; otherwise leave attribution as the session reminder says.
5. Hand back with `rig queue update <id> --state done` including: branch name, commit
   hashes, test counts, and the failing-on-old-code evidence. If anything is unclear or the
   existing code differs from this brief, stop and say what you found; do not guess.

## Out of scope

Deploying anything (production is the Thinkpad, run by another agent); measuring retrieval
quality; changing the rewrite prompt or validator; any live-service work.
