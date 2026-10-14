# Hermes brief: same-content second submit across companies

**Status: APPROVED by the operator 2026-10-14 for sending to Hermes.**
Writes two small synthetic documents to the production LightRAG store. Delete
them afterwards (see Cleanup). Nothing else is authorised.

## Why
The gateway currently marks a document as a duplicate of any active row with the
same sha256, whichever company owns it, so a company-B upload of company-A's
bytes is never submitted. We want to fix that, but cannot tell offline what
LightRAG v1.5.7 does with the same content submitted twice under different
filenames/companies: index it again, return no track, or report PROCESSED without
indexing. Three outcomes, three different gateway behaviours.

## Procedure (LightRAG only, status-only, no corpus content read)
The question is purely what LightRAG does, so the gateway is NOT involved:
**do not start `rag-ingest-worker`, do not use gateway `/upload`, do not change
any setting or restart anything.** Leave the worker stopped.

Use synthetic text, NOT corpus data: two ~300-byte bodies X and Y, each a random
token sentence generated for this probe ("probe-<uuid> ..."), different from each
other. Use the LightRAG API key from `.env` via a shell variable, never echo it.
Submit to LightRAG `POST /documents/text`, one at a time, recording each full JSON
response and the time:
1. X with `file_source` = `dedupeprobe-a.txt`.
2. Wait until its `track_status` is PROCESSED or FAILED.
3. X again (identical bytes) with `file_source` = `dedupeprobe-b.txt`.
4. Y (different bytes) with `file_source` = `dedupeprobe-a.txt`
   (same filename as step 1, different content).
5. For every returned `track_id`, GET `/documents/track_status/<id>` until
   terminal (max 10 min) and record the full body (status, file_path, error text,
   chunk counts). Also record `GET /documents` counts before the probe and after
   each step.
Do not query the corpus and do not read any document other than the probe's own.

## Report (counts and shapes only)
- For steps 1, 3 and 4, per submission: HTTP status, response JSON, whether a new
  track_id was returned, and the track_status body. State plainly which of:
  (1) step 3 was indexed as a second document, (2) step 3 returned no track or an
  error, (3) step 3 reported PROCESSED but only one document exists.
- Whether step 4 (same filename, different content) returns HTTP 409.
- `GET /documents` (LightRAG) count before and after: how many doc entries the
  probe created.

## Cleanup (required)
Delete every probe document from LightRAG (`/documents/delete_document` by doc
id; only ids whose file_path is `dedupeprobe-*`). Confirm `GET /documents` counts
return to the pre-probe numbers. Do not touch any other document.

## Out of scope
No config changes, no restarts, no image rebuilds, no reading of any corpus
document, no enabling of any new gateway setting.
