# Follow-up: ingested -> rejected must stay legal (TODO #2 review)

Your TODO #2 hand-back reviewed OK overall: I re-ran bare env 664 passed and the
pre-fix control 20 failed / 2 passed. One regression found, please fix in the
same uncommitted change (same checkout, master, 0 0 vs origin/master).

## The regression
`submit_file` (app/worker.py ~L370) writes `status="rejected"` via
`update_document_state` when a source file's content changed on disk and now
fails validation (empty, unsupported content, etc.). With LEGAL_TRANSITIONS the
row is `ingested`, and `ingested -> rejected` is denied, so the write is dropped:
the row stays `ingested`/query_ready with the OLD sha256, and every scan
re-validates, re-rejects and re-drops it with a warning. Before your change it
settled as rejected with the new sha. That is a behaviour change nobody asked for.

## Asked
1. Make `ingested -> rejected` legal. A changed file that no longer validates is
   a real transition. Keep ingested denied to `processing` and `failed` ONLY if
   you can show no caller needs them; if `submit_file`'s remaining non-CAS
   writes (post-HTTP failure after a re-submit of a changed file) can legitimately
   go ingested -> failed, they are the same class of bug. Trace each write in
   submit_file and handle consistently. The late-poll protection is the CAS, not
   this table, so you do not lose it.
2. Tests: changed-and-now-invalid file over an ingested row ends `rejected` with
   the new sha, query_ready false, stable across a second scan (not re-written).
   Show it fails on your current diff.
3. Update your list of "not fixed" items in the hand-back: tell me if you think
   `submit_file`'s post-HTTP writes need CAS now, with a one-line reason; do not
   expand scope otherwise. No dead-code removal, no docs entry in this item.

Same constraints and done-criteria as the original brief. Do not commit.
