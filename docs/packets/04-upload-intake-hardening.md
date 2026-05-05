# Packet 04: Upload Intake Hardening

## Status
ready

## Objective

Make the gateway `/upload` path safe, deterministic, and operator-informative before the worker touches a file.

## Why This Packet Exists

The current upload flow is too trusting:

1. filename collisions can overwrite prior uploads
2. upload-time validation is minimal
3. company attribution is dropped
4. operators do not get enough intake metadata
5. bad files can enter the pipeline and only fail later

## In Scope

1. Prevent filename collisions in `/app/uploads`.
2. Preserve `original_filename` separately from stored path and display name.
3. Define deterministic stored-file naming behavior.
4. Run first-pass upload-time validation.
5. Capture upload-time metadata.
6. Support explicit company attribution in upload requests.
7. Define clear API responses for accepted, warned, and rejected uploads.
8. Define how upload-time state is written into document storage.

## Out Of Scope

1. Worker retry policy
2. Full duplicate lifecycle resolution
3. Query gating
4. Company-scoped retrieval
5. LightRAG Web UI exposure
6. Code extraction from installer

## Dependencies

1. Packet 01
2. Packet 02
3. Packet 03

## Required Decisions

1. Internal stored filename pattern
2. Duplicate upload response semantics
3. Whether rejected-after-write uploads still get a document row
4. Whether `company` is accepted immediately in upload requests
5. Whether `source_uri` is accepted now or later

## Implementation Guidance

Recommended upload flow:

1. receive file
2. capture original filename and request metadata
3. store to a collision-safe internal path
4. compute content hash
5. run intake validation
6. upsert document state
7. return structured upload response

Recommended internal naming pattern:

1. `<document_id>--<sanitized-original-name><ext>`

## Expected Deliverables

1. Upload request contract
2. Upload response contract
3. Stored filename strategy
4. Upload-time validation behavior summary
5. Duplicate-upload handling note
6. Operator examples for clean, warned, rejected, and duplicate uploads

## Acceptance Criteria

1. Uploads no longer overwrite prior files by filename collision.
2. Original filename is preserved in metadata.
3. Explicit company can be captured at upload time.
4. Upload-time validation produces clear outcomes.
5. Operators can tell whether a file is uploaded, warned, or rejected.
6. `/upload` response is structured enough for later admin UX work.

## Verification Steps

1. Upload two files with the same original filename but different content.
2. Upload a clean supported `.txt`.
3. Upload an unsupported extension.
4. Upload duplicate content under a different name.
5. Upload a file with explicit `company`.
6. Inspect resulting `/documents` or `/ingest/status` output.

## Evidence Required

1. Packet summary
2. Changed request contract
3. Changed response contract
4. Collision strategy
5. Validation outcomes
6. Verification commands
7. Observed outputs
8. Risks / follow-ups

## Review Checklist

1. Uploads are collision-safe.
2. Operator-facing filename is preserved separately from storage path.
3. Upload validation is useful but not overreaching.
4. Explicit company capture is supported cleanly.
5. Duplicate uploads are visible rather than silently mishandled.

## Risks / Open Questions

1. Exact duplicate-upload HTTP semantics may need refinement.
2. Batch tooling may need follow-up if request shape changes.

## Execution Notes

Keep this packet focused on upload intake only. Do not pull in worker retry or reindex behavior.

## Result

Accepted via `docs/ops/runs/packet-04-result.md` and `docs/ops/runs/packet-04-evidence.md`.
