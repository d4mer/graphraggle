# Brief: lifecycle cleanup (deferred items from TODO #2 review)

## Precondition: do NOT start until TODO #3 is committed
You are working TODO #3 (upload vs source-doc semantics) in this same checkout
and it touches app/worker.py and app/api.py. This item overlaps those files. Start
only when `git status --short` shows no modified tracked files AND
`git fetch && git rev-list --left-right --count HEAD...origin/master` is `0 0`
(i.e. lead has committed and pushed #3). If the tree is dirty with #3 work, do not
touch it: tell lead you are waiting and stop. Leave this change uncommitted.

## Intent
TODO #2 landed the lifecycle table and compare-and-set write-backs (224cea9). You
listed five things you saw and chose not to fix. They are small and belong
together; close them so the model is honest and fully enforced.

## Items
1. **submit_file post-HTTP writes** (app/worker.py). The writes after
   `post_file` / the submit response are not CAS'd on track_id/status like
   poll_track's are. Decide per write whether a stale read can corrupt a newer
   submission; where it can, CAS it the same way. If a write is safe because the
   row cannot have moved (say why), leave it and say so. Do not restructure
   submit_file.
2. **increment_retry** (state_store.py, called at worker.py ~L1048-1050). It is an
   unconditional `retry_count+1`. Make it not count a retry against a row that
   has since moved to a different track_id or status where the caller can tell
   (pass the expected values if cheap); otherwise leave it and document why.
3. **update_status()** has no app/ caller (only your tests). Remove it and move
   its test coverage onto `update_document_state`, or keep it and justify. Your
   call; prefer removal.
4. **`superseded`**. Nothing in app/ writes it as a stored status; supersession is
   derived at read time (`supersedes_document_id`, `derive_readiness`). Find out
   which is true, then make the code and the table say the same thing. If it is
   derived-only, either drop it from the writable set or mark it clearly as
   read-derived. Do NOT add a new writer, and do not change what
   `derive_readiness` returns.
5. **Docs.** Add the transition table and the CAS rule to
   docs/decisions/0001-lifecycle-model.md (or a new numbered ADR if that reads
   better): the legal edges, why ingested can go to rejected but not to
   failed/processing, and what a dropped write looks like in the log. Plain
   language; an operator should be able to read it.

## Constraints
- Offline only. No schema change. No new statuses or new status transitions that
  change behaviour; this is about making existing guards complete and honest.
- All current tests stay green. Edit a test only if it covers something you
  removed (item 3), and say so.
- Smaller diff beats a cleaner rewrite.

## Done when
- `env -i PATH=$PATH HOME=$HOME .venv/bin/python -m pytest tests/ -q` green in a
  bare env.
- Each behaviour change in app/ has a test that fails on the pre-change code
  (show it); pure removals/docs need no new test.
- Hand-back: one line per item - changed / left alone (with the reason) - plus
  anything new you noticed and didn't fix.
