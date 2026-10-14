# Brief: upload vs source-doc ingestion semantics (TODO near-term #3)

Base: current `origin/master` (224cea9 or later). Work in
/home/maverick/work/graphraggle on `master`. First:
`git fetch && git rev-list --left-right --count HEAD...origin/master` must be
`0 0`. Leave the change uncommitted; lead commits and pushes.

## Intent
A document reaches the gateway two ways: `POST /upload` (app/api.py, source_type
"upload", lands in UPLOADS_DIR) and the worker's filesystem scan of
SOURCE_DOCS_DIR (source_type "filesystem"). Both end up as rows in the same
`documents` table and flow through the same worker. The same file should get the
same treatment (dedupe verdict, skip rules, validation_state, readiness)
whichever door it came through. Today the two doors grew separately and differ in
places where nothing says they should.

## Candidate divergences (found by reading; verify each, some may be fine)
1. **Two dedupe implementations.** `/upload` calls
   `state_store.get_document_by_sha256`: the most recent row with that sha of ANY
   status (rejected, failed, superseded, even a `duplicate_content` row), and
   marks the new upload `duplicate_of` it. The worker uses
   `pick_duplicate_candidate`: only ACTIVE_DUPLICATE_STATUSES, never a
   `duplicate_content` row, deterministic ranking (hardened in 2f74255). So an
   upload can be flagged a duplicate of a rejected or already-duplicate row, which
   the worker would never do, and the worker then re-decides at scan time.
2. **Skip rules apply only to "filesystem".** `should_skip_path` returns False for
   uploads, so nothing excludes metadata files or temp files there. Check the
   upload temp file `.{document_id}.part` written into UPLOADS_DIR while a stream
   is in flight: can a concurrent worker scan pick it up (unsupported extension
   -> a `rejected` row written at worker.py ~L1089 for a path that then vanishes
   on rename)? Reproduce it with a test before changing anything.
3. **Validation entry points.** `/upload` uses `validate_upload` (filename+size),
   the worker uses `validate_file` (content). Confirm that an upload accepted at
   the door and then rejected by the worker ends in a state that is coherent for
   the caller of `/documents/{id}` (status, validation_state, error_*), and that
   `auto_split` is handled equivalently on both sides.
4. **Company attribution.** Uploads take company from the form only; filesystem
   rows infer it from the first path segment and are re-inferred on rescan
   (`refresh_company_attribution`). Only note any case where the two disagree
   about the same content; ADR 0004 is the policy, do not change it.

## What I want
- A short written list in the hand-back: for each item above, "real bug",
  "intended asymmetry (why)" or "can't tell, need operator". Evidence = the test
  or the code path, not an opinion.
- Fix the ones that are clear bugs where the fix is local, preferring ONE shared
  implementation over two copies (e.g. the upload path reusing the worker's
  candidate-selection logic) over patching each separately.
- Do NOT change request/response shapes of /upload or /documents, status
  vocabulary, or the DB schema without stopping and asking me first.

## Constraints
- Offline only; no live LightRAG/OpenWebUI; no corpus content.
- All 667 current tests stay green and unedited. If one encodes an asymmetry you
  think is a bug, stop and tell me, don't edit it.
- No new dependencies. Smaller diff beats a cleaner rewrite.

## Done when
- `env -i PATH=$PATH HOME=$HOME .venv/bin/python -m pytest tests/ -q` is green in
  a bare env.
- Every behaviour change in app/ has a test that FAILS on the pre-change code
  (stash the app change, run, restore) and you show that.
- Hand-back: the per-item verdict list above, each app/ behaviour change in
  plain words, and anything you saw but deliberately did not fix.
