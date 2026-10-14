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

## Procedure (status-only, no corpus content read)
Use a synthetic file, NOT corpus data: a ~300-byte text file whose body is a
random token sentence generated for this probe ("probe-<uuid> ..."). Names:
`dedupeprobe-<uuid>.txt`.
1. Upload it via the gateway `/upload` with `company=ProbeCoA`. Wait for
   `status=ingested`.
2. Upload the identical bytes again with `company=ProbeCoB`. Do NOT pre-delete.
   (The CURRENT gateway will mark this one `duplicate_content` and skip it. That
   is the point: so also do step 3.)
3. Submit the same bytes straight to LightRAG `/documents/text` twice with two
   different `file_source` values, e.g. `dedupeprobe-a.txt` and
   `dedupeprobe-b.txt`, capturing each full JSON response.
4. For each returned `track_id`, GET `/documents/track_status/<id>` and record the
   full body (status, file_path, any error text, chunk counts). These bodies
   contain only the probe file's own fields.
5. Ask the (already-running) gateway-side bridge nothing; do not query the corpus.

## Report (counts and shapes only)
- For step 3, per submission: HTTP status, response JSON, whether a new track_id
  was returned, and the track_status body. State plainly which of: (1) both
  indexed, (2) second returned no track/an error, (3) second reported PROCESSED
  but only one document exists.
- Whether a same-`file_source` second submit returns HTTP 409.
- `GET /documents` (LightRAG) count before and after: how many doc entries the
  probe created.

## Cleanup (required)
Delete every probe document from LightRAG (`/documents/delete_document` by doc
id) and the gateway rows `dedupeprobe-*` / `rejected/*dedupeprobe*` if present.
Confirm counts return to the pre-probe numbers. Do not touch any other document.

## Out of scope
No config changes, no restarts, no image rebuilds, no reading of any corpus
document, no enabling of any new gateway setting.
