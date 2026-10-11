# Packet 25 — submit source_docs files via LightRAG scan (fix v1.5.7 upload 409)

Branch: `packet-25-scan-submit` (already created from origin/master in the lead's worktree; create your own:
`git worktree add ../graphraggle-p25 -b packet-25-scan-submit origin/master` if that branch is not checked out for you, or check it out in a worktree. Do NOT switch the shared checkout).
Tests: `RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621 SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db .venv/bin/python -m pytest tests/ -q` (467 pass today). No live services on this box: everything is tested with mocked `client`.

## Problem (proven on prod, 2026-10-11)
Prod LightRAG is v1.5.7 started with `--input-dir /data/source_docs`, the SAME host folder the ingest worker scans.
`app/worker.py` `submit_file` (the `else:` branch around line 472) does `client.post_file("/documents/upload", path)`
for non-text files (pdf/docx/pptx/xlsx/...). LightRAG v1.5.7 `/documents/upload` refuses any file whose canonical
basename already exists in its input dir (document_routes.py ~L5300: HTTP 409 "Input directory already contains a file
with the same canonical basename"). The file the worker is uploading IS in that dir, so every Office/PDF file from
source_docs 409s -> `failed / submit_terminal_failed`. Proven with a canary .docx (docs/ops/runs/rc1-upload-test.md on
the Thinkpad). `.txt/.md` work because they use `/documents/text`.

## Fix (option 1, operator-approved)
For files that already live inside LightRAG's input dir (everything under SOURCE_DOCS_DIR), do not upload. Trigger
LightRAG's own scan, which parses files in its input dir natively:

- `POST /documents/scan` -> `{status, message, track_id}`. Global (scans the whole input dir), returns ONE track_id.
  It may answer `status: "scanning_skipped_pipeline_busy"` (no track_id / no scan started) when the pipeline is busy;
  may also return HTTP 429/409/503. Treat all of these as TRANSIENT: leave the document `accepted`/pending, no failure
  row, retry next scan cycle.
- A scan also performs LightRAG's "exclusive FAILED reset" (pulls failed LightRAG docs back in). That is acceptable
  and expected; note it in the docstring.
- `GET /documents/track_status/{track_id}` returns `{documents: [{file_path, status, ...}, ...]}` for ALL files in
  that scan.

Implementation requirements:
1. New client helper `LightRAGClient.post_scan()` (POST /documents/scan, no body) in `app/lightrag_client.py`.
2. In `submit_file`, when the file is not sent via `/documents/text` AND resolves under SOURCE_DOCS_DIR: do not call
   `post_file`. Instead record the doc as submitted with the scan's `track_id`. To avoid one scan per file, scan at most
   once per `scan_once()` cycle: gather the files needing native submission during the cycle, call `post_scan()` once
   after the loop, and write the returned track_id to each of those rows (status `submitted`, same fields the upload
   path sets today). If the scan is skipped/busy/transient-fails, those rows stay retryable (no terminal failure).
   Choose the least invasive structure that fits the current code (e.g. defer-and-flush in `scan_once`); explain it in the PR notes.
3. Files under UPLOADS_DIR (not in LightRAG's input dir) keep using `/documents/upload` unchanged. Do not touch the text path.
4. `poll_track` currently treats ANY `FAILED` doc in the track as failure of THIS row and any PROCESSED as success.
   With a shared scan track_id that is wrong. When a row's path is from the scan path, `poll_track` must match
   `documents[]` entries to the row by canonical basename of `file_path` (compare `Path(file_path).name`; LightRAG may
   strip/normalise the name — use the basename as stored by `normalize` semantics: exact basename match first), and
   decide status from THAT entry only. If the scan's `documents` has no entry for this file yet, keep status
   `processing`/`submitted` (not `stale_track_id`) until the track is older than a bounded number of polls
   (reuse any existing stale-track policy; do not invent a new timeout without a config flag defaulting to the
   existing behaviour). Track ids that other rows share must not be marked stale because one file is absent.
5. Feature flag: `WORKER_SCAN_SUBMIT_ENABLED` (config.py, default **true**) so ops can fall back to the old upload path.
6. Dedupe/hash logic from packet 18 (`mark_duplicate_candidate` etc.) must be unchanged.
7. Logging: one `warn()`/info line per scan trigger with number of files covered and the returned track_id or the skip reason.

## Acceptance criteria (tests, all offline with a fake client)
- docx under SOURCE_DOCS_DIR: no `/documents/upload` call; exactly one `/documents/scan` call per cycle for N files;
  every row gets the same track_id and status `submitted`.
- scan returns `scanning_skipped_pipeline_busy` / raises 429/503: rows remain retryable, no `failed` row, next cycle retries.
- poll_track with a shared track_id: 3 files in the track, statuses {A: PROCESSED, B: FAILED, C: PROCESSING} ->
  A ingested, B failed, C still processing. A file absent from `documents` stays processing; empty `documents` keeps
  the existing stale_track behaviour.
- Flag off: old upload path used, covered by a test.
- A file under UPLOADS_DIR still uses upload. `.txt` still uses `/documents/text`.
- Existing 467 tests pass, plus the new ones. Show full pytest output.

## Out of scope
Compose changes, LightRAG config, deleting the existing failed LightRAG stubs, anything on the gateway/bridge side.
Prod validation will be done afterwards by the ops agent with a canary docx; do not attempt it.

## Report back
Files changed, test count, any place where LightRAG's real response shape was an assumption (list them so the lead
can have them checked against the live API: scan response keys, `file_path` format in track_status documents).
