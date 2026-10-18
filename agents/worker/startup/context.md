# worker — session context (rewritten 2026-10-17 18:10 local)

## Queue state

`rig queue list --owned` → `[]`. Nothing in my inbox to claim. The last row,
**`qitem-20261017170723-9123db5e`** ("Structure-aware pre-chunker on context-chunking
branch") is **handed-off to `orch-lead@graphraggle`** (terminal for me, since 18:06Z).
Full handoff note is in the row (`rig queue show qitem-20261017170723-9123db5e --full --json`).
Watchdog `parked-owner-consumer` job `01M4AYEG0WK885GGJPDGRNVGD5` may still fire; on a wake,
check `rig queue list --owned` / `inbox-pending dev-worker@graphraggle` and if empty, report
that the row is handed off and wait.

## Where the work is

- **Worktree:** `/home/maverick/work/graphraggle-ctx`, branch **`context-chunking`** (from
  `chunking` @ `4642925`). Left **uncommitted on purpose** — the lead commits and pushes; I
  never push. `git status`: M `app/config.py`, M `app/worker.py`, M `scripts/overnight_loop.py`,
  M `scripts/chunk_embed_bench.py`, M `docs/ops/loop/chunk-embed-bench-configs.json`,
  M `tests/test_overnight_loop.py`, NEW `app/structure_chunker.py` (1164 lines),
  NEW `tests/test_structure_chunker.py` (~800 lines), NEW `docs/ops/loop/README.md`.
- Main checkout `/home/maverick/work/graphraggle` is on `master`; research/plan docs and this
  file live there. Venv is the main checkout's `.venv`.
- Snapshot of every touched file: `/tmp/ctx_snap/` (aggregate sha `3ba6e30e9525…`), used to
  prove the sabotage controls were restored byte-identical.

## Test status

Bare env, socket guard live:
`.venv/bin/python -m pytest tests/ -q` → **944 passed, 2 warnings, 134 subtests in ~4.8s**.
Branch baseline at `4642925` was 889 passed / 69 subtests; I added 55 tests (+65 subtests).
Control round: 13 in-place reversions of the new behaviour → **50 failed, 290 passed**, then
restored sha256-identical and green again.

## What shipped in this item (so I can answer follow-ups without re-reading 1200 lines)

`app/structure_chunker.py` is pure (no I/O/clock/random/settings) and imports only stdlib +
`app.transcript_prep` (`_parse_turn`, `_leading_parenthetical`, `_classify_paren_label`,
`_is_noise`, `_metadata_from_name`, `_HEADER_RE`) so there is exactly ONE transcript parser.
`chunk_document(text, filename=, budget_tokens=350, header_tokens=25,
headers=True|False|"shuffled", kind="auto") -> ChunkPlan`; `render_for_lightrag(plan) ->
(text, chunking_params)` with `{"strategy":"fixed_token","params":{"chunk_token_size":budget,
"split_by_character":<from DELIMITER_CANDIDATES, \x1e first>,"split_by_character_only":True}}`
(names verified against the 1.5.7 wheel; real bool, never 1).
Rules: transcript turns whole → sentences → clauses → words; headings (`#`, `1.2.3`, short
ALL-CAPS) → section path; tables (pipe/tab/columnar) whole → by row repeating header+rule →
by cell repeating the row key → a cell only by words; captions on the first piece; lists with
lead-in; furniture = short fragment + no sentence punctuation + not a turn/heading/bullet/table
line + ≥3 repeats (counted in `stats.dropped_running_lines`); plain = paragraphs → sentences →
words. Header `[Doc: … | Date: … | Section: A / B | Part k/n]`, drop order path → date →
speakers → title, part marker always survives. Budget covers header (reserved before packing).
Token count: tiktoken if importable else conservative overcount `max(chars/3.6, ws-units)` —
tiktoken is NOT installed here, so all local numbers are overcounts (safe direction).

Integration: `STRUCTURE_CHUNKING_{ENABLED,BUDGET_TOKENS,HEADER_TOKENS,HEADERS}` in
`app/config.py`; `prepare_structure_payload()` in `app/worker.py` runs **after**
`prepare_transcript_payload()`, adds the `chunking` key only when enabled, keeps the dedupe
hash on original bytes, records provenance as counts/names in `warnings_json`, falls back to
untouched text + reason on failure. Loop: per-arm `"structure_chunking": {...}`
(`ol.structure_chunking_spec` validator, refused together with `"chunking"`, no whitelist
change needed since nothing new goes into `.env`, `--probes-only` untouched). Bench:
`"chunker": {"strategy":"structure", ...}` importing the shipped module (test asserts
byte-identical pieces), grid arms `structure-350`, `structure-350+rerank`,
`structure-350-nohdr`, `structure-350-shuffled` (recursive-350 already existed).

Two hazards the sabotage round found and that are now fixed + tested: (a) the block scan must
always consume a line (a disabled branch made it loop forever on `# Heading`) —
`test_no_input_shape_can_stall_the_scan`; (b) furniture vs headings: structure lines are exempt,
otherwise a repeated heading deleted a section path.

## Open threads for the lead (do not act on these unasked)

1. The **stack2 endpoint contradiction**: `docs/ops/loop/experiments-chunk350.json` `_readme`
   uses 127.0.0.1:9622 / gateway 8021 / project `rag-chunk2` / container `lightrag-server2`,
   while `experiments-chunking.json` and `docs/ops/second-instance.md` describe 9631/8030 /
   project `stack2`. Flagged twice; needs one decision.
2. Unmeasured assumption: that LightRAG cuts ONLY at the delimiter (`split_by_character_only`)
   and that the store ends up with exactly my pieces — needs one real ingest + a chunk count.
3. No `experiments-structure-chunking.json` loop file yet (bench grid has the A/B/C/D arms);
   one JSON away if the lead wants ingest-arms.
4. Bench grid is now 21 configs and the bench has no per-config selector — only a `--configs`
   file. A trimmed file is the cheap way to run just the structure arms.

## Habits that keep this seat honest

Claim before starting; stay inside the brief; one atomic `python - <<'PY'` patch with
`assert s.count(old) == 1` guards; snapshot before sabotage controls and verify the restore
by sha256; run the suite in a bare env (`env -i PATH=$PATH HOME=$HOME`); patch `ol.Ops`
(FakeOps) before ever calling `ol.main()` in a test; never send a bool as `1` in LightRAG
params; never let a test touch 9621/8020 or docker.
