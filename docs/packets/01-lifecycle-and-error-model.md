# Packet 01: Lifecycle And Error Model

## Status
accepted

## Objective

Define the canonical ingestion lifecycle, error taxonomy, and operator-visible metadata model before any behavioral hardening work begins.

## Why This Packet Exists

The current stack only has coarse states and generic failure handling. Later packets depend on a stable contract for status, validation verdicts, readiness, and error reporting.

## In Scope

1. Define canonical document lifecycle states.
2. Define allowed state transitions.
3. Define error-stage taxonomy.
4. Define a first-pass error-code taxonomy.
5. Define operator-visible metadata fields.
6. Map current behavior to the future model.

## Out Of Scope

1. Full validation logic.
2. Company retrieval scoping.
3. LightRAG Web UI exposure.
4. Code extraction out of installer.

## Dependencies

1. Packet 00.

## Required Decisions

1. Whether `warn` is a lifecycle `status` or only a `validation_state`.
2. Whether `accepted` remains a visible lifecycle state.
3. Exact definition of `query_ready`.

## Implementation Guidance

Recommended model:

1. `status` for lifecycle/progress.
2. `validation_state` for validation verdict.
3. `error_stage` for where a failure occurred.
4. `error_code` for the specific classified reason.

Recommended lifecycle states:

1. `pending`
2. `validating`
3. `accepted`
4. `submitted`
5. `processing`
6. `ingested`
7. `failed`
8. `rejected`
9. `superseded`
10. optional `quarantined`

Recommended validation states:

1. `not_run`
2. `accept`
3. `warn`
4. `reject`
5. `auto_split`

Recommended error stages:

1. `upload`
2. `validation`
3. `classification`
4. `decode`
5. `split`
6. `submit`
7. `track_poll`
8. `query_readiness`
9. `reindex`

Recommended initial error codes:

1. `unsupported_extension`
2. `decode_failed`
3. `lightrag_processing_failed`
4. `track_status_failed`
5. `ingest_execution_failed`

## Expected Deliverables

1. Lifecycle state table.
2. Transition table.
3. Error-stage table.
4. Initial error-code table.
5. Target `/documents` and `/ingest/status` field list.
6. Example JSON for ingested, rejected, and failed docs.

## Lifecycle Transition Table

Allowed transitions for the current contract:

1. `pending -> validating`
2. `validating -> accepted`
3. `accepted -> submitted`
4. `submitted -> processing`
5. `submitted -> failed`
6. `processing -> ingested`
7. `processing -> failed`
8. `failed -> validating`
9. `ingested -> superseded`

Explicitly out of scope for this packet:

1. `rejected -> submitted`
2. `quarantined` runtime behavior
3. `auto_split` lifecycle handling

## Current Target Fields

Packet 01 minimally encodes these fields in generated runtime state:

1. `document_id`
2. `path`
3. `source_type`
4. `company`
5. `filename`
6. `sha256`
7. `status`
8. `validation_state`
9. `error_stage`
10. `error_code`
11. `error_message`
12. `error_detail`
13. `query_ready`
14. `retry_count`
15. `track_id`
16. `last_error`
17. `created_at`
18. `updated_at`

Later packets expand this further.

## Acceptance Criteria

1. `status` and `validation_state` are clearly separated.
2. `query_ready` is explicitly defined.
3. Retryable vs terminal failures are distinguishable in the model.
4. Later packets can build on the contract without inventing new fields ad hoc.

## Verification Steps

1. Capture one current `/ingest/status` response as a baseline.
2. Map at least three scenarios through the new model:
   1. successful `.txt`
   2. unsupported file
   3. long transcript timeout
3. Produce example target JSON for:
   1. ingested doc
   2. rejected doc
   3. failed doc

## Evidence Required

1. Packet summary
2. State model
3. Error model
4. Proposed fields
5. Changed files if any
6. Example JSON
7. Risks / open questions

## Review Checklist

1. Lifecycle and validation are separated cleanly.
2. `query_ready` is defined clearly.
3. Status transitions are sane for future retry and reindex behavior.
4. Model is compatible with company-aware scoping later.

## Risks / Open Questions

1. Whether `accepted` should stay visible or collapse into `submitted` later.
2. Whether `quarantined` is needed immediately or can wait.

## Execution Notes

Recommended baseline decisions unless blocked:

1. `warn` belongs in `validation_state`, not primary `status`.
2. `accepted` remains visible.
3. `query_ready = status == ingested` unless superseded or blocked.

Implemented in generated runtime code:

1. Added schema fields for `validation_state`, `error_stage`, `error_code`, `error_message`, `error_detail`, and `query_ready`.
2. Added lightweight schema migration logic for those fields.
3. Expanded summary output to include status, validation-state, and readiness counts.
4. Updated upload records to surface `validation_state` and `query_ready`.
5. Updated worker lifecycle flow to use `validating -> accepted -> submitted`.
6. Updated worker failure paths to record `error_stage`, `error_code`, and `error_message`.
7. Added `rejected` to `TERMINAL_FAILURE` set for terminal lifecycle state.
8. Added explicit `rejected` early-return in `handle_candidate` to prevent retry loops.
9. Added unsupported-file reject handling in `scan_once`: creates record with `status=rejected`, `validation_state=reject`, `error_stage=validation`, `error_code=unsupported_extension`.
10. Added `warn()` function and fallback-encoding signal in `read_text_with_fallbacks` (returns `tuple[str, str]`) for minimal operator-visible warning path.

Example target JSON for an ingested document:

```json
{
  "document_id": "1234",
  "path": "/app/uploads/example.txt",
  "source_type": "upload",
  "company": null,
  "filename": "example.txt",
  "sha256": "abc123",
  "status": "ingested",
  "validation_state": "accept",
  "error_stage": null,
  "error_code": null,
  "error_message": null,
  "error_detail": null,
  "query_ready": 1,
  "retry_count": 0,
  "track_id": "track-1",
  "last_error": null
}
```

Example target JSON for a failed document:

```json
{
  "document_id": "1234",
  "path": "/app/uploads/example.txt",
  "source_type": "upload",
  "company": null,
  "filename": "example.txt",
  "sha256": "abc123",
  "status": "failed",
  "validation_state": "accept",
  "error_stage": "track_poll",
  "error_code": "track_status_failed",
  "error_message": "Failed to poll LightRAG track status",
  "error_detail": "timeout",
  "query_ready": 0,
  "retry_count": 1,
  "track_id": "track-1",
  "last_error": "timeout"
}
```

Example target JSON for a future rejected document contract:

```json
{
  "document_id": "1234",
  "path": "/app/uploads/unsupported.bin",
  "source_type": "upload",
  "company": null,
  "filename": "unsupported.bin",
  "sha256": "pending",
  "status": "rejected",
  "validation_state": "reject",
  "error_stage": "validation",
  "error_code": "unsupported_extension",
  "error_message": "File extension is not supported",
  "error_detail": null,
  "query_ready": 0,
  "retry_count": 0,
  "track_id": null,
  "last_error": "File extension is not supported"
}
```

## Result

accepted

### Remediation Artifacts

Packet 01 remediation evidence (upload-time extension validation fix) is captured in `docs/ops/runs/`:

- [packet-01-unsupported-file-rejection-review.md](../../ops/runs/packet-01-unsupported-file-rejection-review.md) — review with acceptance/verification checks
- [packet-01-evidence.md](../../ops/runs/packet-01-evidence.md) — evidence report (changed files, commands, before/after API, risks, rollback)
- [packet-01-result.md](../../ops/runs/packet-01-result.md) — final result summary
