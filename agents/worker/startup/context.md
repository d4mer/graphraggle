# Handoff — worker

Last updated: 2026-10-07 (qitem-20261007103353-e2fafb7b delivered to lead)

## Current task

`qitem-20261007103353-e2fafb7b` — "Harden worker dedupe" (TODO near-term #1).
Claimed, implemented, tested, **handed off to orch-lead@graphraggle**. Not
committed, not pushed (lead integrates). Queue state after handoff: done on my
side; awaiting lead review.

## State

- Branch `master`, HEAD `05ff985` (unchanged; I never push).
- Modified: `app/worker.py` (+56 / -5).
- New: `tests/test_worker_dedupe.py` (14 tests).
- Untracked rig scaffolding left alone: `.openrig/`, `AGENTS.md`, `CLAUDE.md`,
  `agents/`, `rig.yaml`.
- Test result: **287 passed in 0.28s** (`.venv/bin/python -m pytest tests/ -q`)
  = 273 pre-existing + 14 new. Pre-fix the new file was **7 failed, 7 passed**.

## What changed in app/worker.py

1. `pick_duplicate_candidate`: rows with `error_code == "duplicate_content"` are
   no longer eligible as a `duplicate_of` target (kills the mutual-duplicate trap).
2. `handle_candidate`: the early `return` for duplicate rows is replaced by a
   `duplicate_row` flag. Each scan re-runs `pick_duplicate_candidate`; if a live
   original remains it returns with **no DB writes** (idempotent), otherwise it
   clears the duplicate markers (status `validating`, error fields cleared,
   `duplicate_of` + dedupe worker keys dropped) and falls through to submission
   with `retry_reason="duplicate_original_removed"`.
3. `handle_candidate` poll block: dedented the two unreachable lines after the
   second `return`, so a poll that lands in a non-terminal/non-submitted state
   continues into retry bookkeeping.
4. No schema change, no unrelated refactor.

## Extra verification done (throwaway scripts, deleted)

Real-sqlite end-to-end runs confirmed: one ingested + one `duplicate_content`
stable over 3 scans; duplicate re-ingested when the original's content changes,
its row is deleted, or it is rejected; failed original retried instead of
re-marked; 3 further scans produce zero extra submits.

## Next step

Wait for the lead. If review comes back, the open thread is whether the lead
wants the stale-row-on-deleted-file behaviour (scan_once never prunes rows for
files removed from disk) addressed — that is TODO near-term #2, out of this brief.
