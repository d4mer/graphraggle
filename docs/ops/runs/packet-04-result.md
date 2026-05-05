# Result — Packet 04: Upload Intake Hardening

## Summary

Hardened the gateway `/upload` intake path so uploads now land under deterministic collision-safe internal names, preserve `original_filename` and optional `company` metadata, compute and store upload-time hashes, surface duplicate-content warnings before the worker runs, and return operator-focused response payloads for accept, warn, auto-split, and reject outcomes. Rejected uploads continue to persist document-state rows, but unsupported-extension rejects now avoid writing files under `/app/uploads`.

Deployed the updated gateway and worker files to `macmini.local`, rebuilt the `gateway-api` and `ingest-worker` containers, and verified live collision-safe paths, duplicate warning metadata, explicit company capture, and reject-without-write behavior.

## Files Changed

| File | Change |
|------|--------|
| `app/api.py` | Added deterministic stored filenames, optional `company` form capture, upload-time SHA-256/duplicate detection, structured reject payloads, and richer upload responses |
| `app/state_store.py` | Added `get_document_by_sha256()` for upload-time duplicate lookup |
| `app/worker.py` | Preserved existing `company`, `filename`, and `original_filename` when worker resubmits upload-backed rows |
| `scripts/install_rag_stack.sh` | Updated embedded `api.py`, `state_store.py`, and `worker.py` copies to match the live app changes |
| `docs/ops/runs/packet-04-result.md` | Added Packet 04 summary and deployment notes |
| `docs/ops/runs/packet-04-evidence.md` | Added verification commands and observed live outputs |

## What Changed and Why

### 1. Collision-safe stored paths

- Uploads now use `<document_id>--<sanitized-original-name><ext>` under `/app/uploads`.
- This keeps storage deterministic and prevents same-name uploads from overwriting earlier files.
- `filename` now reflects the internal stored basename for upload rows, while `original_filename` preserves the operator-facing client name.

### 2. Upload-time metadata capture

- `/upload` now accepts optional multipart form field `company`.
- Accepted and rejected upload rows persist `company`, `original_filename`, `sha256`, and `content_hash` at intake time.
- The worker now preserves these upload-time fields instead of overwriting them with `None` or the internal basename during submit-time upserts.

### 3. Reject-before-write behavior

- Unsupported extensions and empty-file rejects still use Packet 02 validation rules.
- Those rejects now return a structured intake payload and store a rejected document row without writing a file into `/app/uploads`.

### 4. Duplicate-content visibility at upload time

- After a supported upload is written to its temporary file and hashed, the API checks existing document rows by SHA-256.
- Duplicate uploads are marked `validation_state="warn"` immediately, with `error_code="duplicate_content"`, `duplicate_of` metadata in the response, and `warnings_json` persisted for operators.
- This keeps Packet 02 acceptance/reject/auto-split rules intact while surfacing duplicates earlier.

### 5. Operator-focused upload responses

- Upload responses now clearly include `document_id`, `source_type`, `company`, `filename`, `original_filename`, `path`, `validation_state`, `content_hash`, `sha256`, `byte_size`, and `duplicate_of` when present.
- Reject responses now include the same intake payload in `data`, instead of returning only an error envelope.

## Deployment

Updated files were copied to `~/rag-project/app/` and `~/rag-project/install_rag_stack.sh` on `macmini.local`, then `gateway-api` and `ingest-worker` were rebuilt with Podman.

## Outcome

1. Upload filename collisions no longer overwrite earlier uploads.
2. `original_filename` is preserved independently from the stored internal path.
3. Optional `company` capture works at upload time without changing retrieval scoping.
4. Duplicate content is visible in upload responses and persisted state before worker submission.
5. Unsupported-extension rejects avoid writing runtime upload files when the API can reject them up front.
