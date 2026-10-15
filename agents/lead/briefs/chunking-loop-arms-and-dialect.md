# Brief: real transcript dialect + per-request chunking arms in the loop (branch `chunking`)

Work ONLY in `/home/maverick/work/graphraggle-chunking` (branch `chunking`, base
`origin/chunking` = `b535792`). Same test command as the previous chunking brief (shared
venv, bare env, confirm `app.__file__` is the worktree). Baseline 726 passed. Do not
commit or push; hand back the diff.

## What Hermes found (content-free, from the real corpus; report on the Thinkpad)
1. **Every real transcript uses one speaker form: a parenthesised label prefix**,
   `(Label) text` at the start of a line. No colons after names, **no timestamps at all**,
   no VTT/SRT, no Q/A, no roles or emails inside labels. Your detector (colon and
   timestamp dialects) very likely does NOT recognise this. That is the single most
   important fix: with the flag on, the real corpus would currently be skipped.
2. Turns are long-tailed: p50 12-18 tokens, p90 65-155, max 2201; **40-51% of turns are
   under 10 tokens** (filler, backchannel). One degenerate file has a single 7,661-token
   turn across two lines.
3. File names end in `-transcript.txt`; some carry a date, some a `Title_` prefix, one is
   a plain English sentence. Title must come from the stem, cleaned (strip `-transcript`
   suffix and separators), never invented; date only if the name carries one.
4. **LightRAG 1.5.7 takes a per-request chunker**: `POST /documents/text` (and `/texts`)
   accept `"chunking": {"strategy": "fixed_token" | "recursive_character" |
   "semantic_vector" | "paragraph_semantic" | "custom", "params": {...}}`; omitted =
   fixed-token default (1200/100). Unknown or ill-typed params give a synchronous HTTP
   422. The per-strategy `params` key names are being read from the image by Hermes and
   will be in `~/rag-project/docs/ops/runs/chunker-experiments.md`; until then do NOT
   guess param names, pass `params` through untouched from the experiment file.

## Build
1. **Dialect `(Label) text`** in `app/transcript_prep.py`: detection, turn merging
   (consecutive lines of one label are one turn, and a label-less continuation line
   belongs to the previous turn), header insertion as before. Fixtures are synthetic: label
   shapes such as `(Ana)`, `(Speaker 1)`, `(Dr Smith)`, parentheses used in normal prose
   (`(see above)`, `(1)`) must NOT be treated as speakers; require the same label to
   repeat at the start of several lines before accepting. Keep every previous guarantee
   (words in order, idempotent, flag-off identity, hash on original bytes).
2. **Tiny turns**: do not emit a header for every sub-10-token turn. Headers go at the
   token cadence (`TRANSCRIPT_PREP_HEADER_EVERY_TOKENS`), not per turn; very short
   consecutive turns may be grouped into one block under one header while keeping each
   `(Label) text` line intact. Test with a synthetic file where 50% of turns are under 10
   tokens: header count stays near total_tokens/cadence.
3. **Degenerate long turn**: a single 7,000+ token turn with no sentence terminators and
   few line breaks must still get headers inside it (the earlier "never broken" rule
   leaves it header-free: change that for this case only, by breaking at the nearest
   whitespace, never inside a word) and the word-order test must still pass.
4. **Loop key `"chunking"`** in `scripts/overnight_loop.py`: an experiment may carry
   `"chunking": {"strategy": ..., "params": {...}}`, added to every `/documents/text`
   body that experiment posts, validated only structurally (strategy in the enum above,
   params an object). Default absent = body unchanged (test byte-identical). Combine with
   `"transcript_prep": true`.
5. **Parameterise the target** so the loop can run against a second LightRAG and gateway
   without touching production: `--lightrag-url`, `--gateway-url`, `--lightrag-container`,
   `--gateway-container`, `--env-file`, defaulting to today's values. The driver today
   assumes `lightrag-server`, `rag-gateway-api`, the production `.env`, and clears the
   store it points at: the defaults must behave exactly as now, and a test must prove the
   clear step uses the passed URL, not the constant. Add a refusal: if
   `--lightrag-url` equals the production URL and `--allow-production` is not given AND
   the experiments file contains `"reingest": true`, exit before any request with a clear
   message ("this would wipe the production store").
6. **Logs**: replace the `transcript_prep <filename>` log lines by an index
   (`doc 3/6`) so loop logs carry no file names.
7. Write `docs/ops/loop/experiments-chunking.json` with arms: `C0` baseline (no keys),
   `P1` `transcript_prep: true`, `C1` recursive, `C2` semantic_vector, each with
   `"reingest": true`, params left `{}` (defaults) until Hermes' report names the keys.
   Note in the file header that params must be filled from the report.

## Hard constraints
- Do not read corpus or transcript content; synthetic fixtures only.
- No change to production code paths with the flags off; no schema change; no change to
  dedupe or lifecycle code; production worker default unchanged.

## Hand-back
Diff, suite result, the before/after of the real dialect on a synthetic file, header
counts for the 50%-tiny-turns and the 7,000-token-turn fixtures, and the guard test for
the production-wipe refusal.
