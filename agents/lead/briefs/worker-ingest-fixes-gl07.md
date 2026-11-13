# Worker brief — three ingest fixes from the GL-07/08 report (Plane GRAG-62/63)

Base: `git fetch && git rev-list --left-right --count HEAD...origin/master` must read `0 0`; branch from current `origin/master` (a8f04bb or later), own
branch/worktree, do not push. You do NOT read corpus or LightRAG content; synthetic/invented fixtures only. Bare full suite must pass:
`.venv/bin/python -m pytest tests/ -q`. Hand back a branch + patch path + short report with test counts.

## Why
Hermes ingested two synthetic companies on stack2 (measured, 2026-11-12). Three defects showed up in `app/worker.py` behaviour; each is small but each leaves
junk rows or failed documents in every company folder.

## Fix 1 — `manifest.json` must not be ingested as a document
Every synthetic company folder has a `manifest.json` (a test-harness file). `.json` is in `SUPPORTED`/`TEXT_EXTENSIONS`, so the worker submits it and it always fails
(LightRAG: "Chunk token length 3431 exceeds chunk_token_size 3060"; error_stage track_poll or submit). Add an explicit, configurable skip rule (default list e.g.
`manifest.json`, via a config setting with an env alias, matched on basename, case-insensitive). A skipped file should leave a terminal `skipped` state row with a reason
(see fix 2), not a failure. Do not stop real `.json` documents being ingested.

## Fix 2 — a skipped duplicate must reach a terminal status
With `COMPANY_AWARE_DEDUPE` off, `mark_duplicate_candidate` (skip_duplicate_content) leaves the state row `accepted` forever: the skip happens before submit, and the worker only
polls submitted tracks, so the row never terminates (seen: 1 stuck row in Northwind, 2 in Contoso). Give those rows a terminal status (`skipped`, with `duplicate_of` kept in
warnings_json as today). Check every consumer that treats `accepted` as in-progress (stale-track sweeps, status counts, the gateway's company join, lifecycle cleanup) and
keep their behaviour correct: a `skipped` duplicate must still count as "this company has this file" where the gateway join uses `duplicate_of`/match_paths, and must not be
retried or swept as stale. Add a migration/backfill note for existing `accepted` rows that already carry `dedupe_action=skip_duplicate_content` (do not run it against anything).

## Fix 3 — PDF chunk-token overflow (505 > 481)
Synthetic integration-analysis PDFs fail in LightRAG with "Chunk token length 505 exceeds chunk_token_size 481", the same family as the production S/4 PDF (see
`pdf-split-diagnosis.md`). Diagnose first, on invented PDF-like fixtures: which path sends text to LightRAG for PDFs when `WORKER_PDF_EXTRACT_ENABLED` /
structure chunking apply, where a piece larger than LightRAG's chunk_token_size gets through, and whether that limit (481) is a LightRAG setting the worker should respect
or whether the structure-chunker budget (`STRUCTURE_CHUNKING_MAX_PIECE_TOKENS`, 7500) is the wrong cap for this path. Fix it so no piece the worker submits can exceed the
limit LightRAG will enforce, without changing chunk boundaries for documents that already ingest correctly (the 8 transcript + DOCX byte-exact results must hold).
If the right fix needs a design decision (change a limit vs. split pieces), stop and report the options instead of choosing.

## Tests
Each fix gets tests with invented fixtures: manifest skip (and a real `.json` still ingested), duplicate row reaches `skipped` and is not swept/retried, no submitted
piece over the limit for an adversarial PDF-like input. Report test counts before/after.

## Hand-back
`rig queue create --destination orch-lead@graphraggle --summary "..."` with branch, patch path, per-fix result, anything you decided not to do and why.
