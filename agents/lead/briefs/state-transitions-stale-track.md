# Brief: SQLite status transitions and stale track_id replacement (TODO near-term #2)

Base: current `origin/master` (5990a0c or later). Work in
/home/maverick/work/graphraggle on branch `master`. First run
`git fetch && git rev-list --left-right --count HEAD...origin/master`; it must
read `0 0`. Leave your change uncommitted; lead commits and pushes.

## Intent
A document's row in state.db should move through its lifecycle only along valid
edges, and a poll result for a LightRAG track_id must never be applied to a row
that has since moved on to a different track_id. Today nothing enforces either,
so a slow poll, a retry, or a re-scan can silently downgrade or corrupt a row.
Nobody sees it until a doc is stuck "processing" or shows query_ready wrongly.

## What I saw (starting points, verify, don't trust)
All in app/state_store.py and app/worker.py.
- `update_document_state` is an unconditional `UPDATE ... WHERE path=?`. There is
  no compare-and-set on `track_id` or `status`. The worker's poll path
  (`track_status/{track_id}` handling, ~worker.py:658-800) reads the row, awaits
  an HTTP call, then writes. If the row was resubmitted meanwhile (new track_id),
  the late result overwrites the new state, including `track_id=None`.
- `update_status` sets `query_ready=(status=="ingested")` but does not touch
  `track_id`, `error_*`, or `ingested_at`, and accepts any transition (e.g.
  `superseded` or `ingested` back to `processing`, `rejected` to `submitted`).
- `upsert_document` ON CONFLICT(path) overwrites `status`, `track_id`,
  `query_ready` and error fields wholesale and leaves `retry_count` alone. A
  re-scan of an unchanged, already-ingested file should not reset it.
- Stale handling clears `track_id=None` then resubmits
  (`stale_track_worker_updates`, `track_id_policy: clear_and_resubmit`). Check
  the retry_count and error-field bookkeeping across that round trip.

## What I want
1. A small, explicit definition of the legal status edges, in one place, with the
   current behaviour of the code respected (don't invent new statuses). Illegal
   transitions are rejected or ignored with a log line, not silently applied.
2. Poll/writeback results are applied only if the row's track_id (and status)
   still match what the poller read. A mismatch is a no-op with a log line.
3. Stale track_id replacement is atomic and idempotent: replaying the same stale
   event twice leaves the same row, and does not burn extra retries.
4. `ingested` rows are not downgraded by a re-scan of an unchanged file.

You decide the shape (a guard clause, a CAS `WHERE`, a transitions table).
Smaller is better; keep the public function signatures the API/worker use
unless a change is clearly needed, and say so if you change one.

## Constraints
- Offline only. No live LightRAG/OpenWebUI. No corpus or LightRAG content.
- Existing behaviour covered by the 642 current tests must stay green. If a
  current test encodes an illegal transition, stop and tell me rather than
  editing it silently.
- No new dependencies. No schema migration unless you can show it is needed; if
  you think one is, stop and ask first.

## Done when
- `env -i PATH=$PATH HOME=$HOME .venv/bin/python -m pytest tests/ -q` is all
  green (bare env; tests/conftest.py supplies defaults).
- New tests cover: late poll after resubmit is ignored; illegal edge rejected;
  stale replay idempotent; re-scan of ingested unchanged file keeps it ingested.
- Show the new tests FAIL on pre-fix code (stash the app change, run, restore).
- Hand-back lists each behaviour change in app/ in plain words, any signature
  change, and anything you found that you chose NOT to fix.
