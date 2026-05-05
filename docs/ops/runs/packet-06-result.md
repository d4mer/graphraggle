# Result - Packet 06: Query Readiness And Reindex Controls

## Summary

Implemented an explicit query-readiness contract in the gateway API and replaced the `/ingest/reindex` placeholder with scoped operator requeue semantics that flow back through the existing worker lifecycle.

The implementation stays inside Packet 06 scope. It does not add query-time company filtering, UI work, or a new ingestion subsystem. It builds on the accepted Packet 01-05 lifecycle, validation, retry, and worker-state contracts.

## Files Changed

| File | Change |
|------|--------|
| `app/models.py` | Added checked-in request/response models and expanded `ReindexRequest` selectors |
| `app/state_store.py` | Added canonical readiness derivation, readiness-reason annotation, scoped reindex selection, and readiness summary slice |
| `app/api.py` | Exposed readiness reason in API payloads and replaced `/ingest/reindex` stub with real queued/blocked semantics |
| `app/worker.py` | Honored explicit reindex markers so worker resubmits through the normal flow instead of returning early |
| `docs/06-api-reference.md` | Documented readiness fields and reindex request/response usage |
| `docs/03-admin-operations.md` | Added operator commands for readiness inspection and reindex control |
| `docs/ops/runs/packet-06-result.md` | Added Packet 06 summary |
| `docs/ops/runs/packet-06-evidence.md` | Added Packet 06 evidence and QA commands |
| `CHANGELOG.md` | Recorded Packet 06 changes |

## What Changed And Why

### 1. Query readiness is now explicit and explainable

- `query_ready` is now derived from the Packet 01 and ADR 0003 contract instead of trusting the stored flag alone.
- Each document response now includes `readiness_reason` so operators can answer why a row is not searchable without reading worker logs.
- `/ingest/status` now includes `by_readiness_reason` for quick readiness triage.

### 2. Readiness stays compatible with the current state model

- No new lifecycle state was introduced.
- Readiness is computed from existing `status`, `validation_state`, and supersession metadata.
- Stored `query_ready` remains in place for backward compatibility with the current worker writes and schema.

### 3. `/ingest/reindex` now has operator-meaningful behavior

- The endpoint now requires at least one selector and supports `document_id`, `path`, `status`, `validation_state`, `company`, and `source_type`.
- Matching rows are either queued for worker-handled resubmission or returned as blocked with a reason and message.
- Non-forced reindex protects already-query-ready, rejected, split-required, and in-flight rows from accidental churn.

### 4. Reindex routes through the worker instead of bypassing it

- Reindex marks rows back to `pending`, clears active track state, resets retries, and records explicit worker-visible reindex metadata.
- The worker now honors that marker and bypasses its normal same-content early returns exactly once, then clears the marker and resubmits through existing validation and LightRAG submission logic.
- This preserves Packet 05 retry and dedupe behavior instead of creating a separate ad hoc ingestion path.

## Outcome

1. Operators can see whether a document is query-ready and why.
2. Readiness semantics are consistent across `/documents`, `/documents/{id}`, and `/ingest/status`.
3. Reindex can intentionally requeue failed or stale rows while remaining inside the current worker model.
4. Forced reindex stays explicit for blocked or already-ingested rows.
