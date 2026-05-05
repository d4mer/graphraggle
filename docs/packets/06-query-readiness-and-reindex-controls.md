# Packet 06: Query Readiness And Reindex Controls

## Status
ready

## Objective

Make queryability a real explicit concept and replace the placeholder reindex flow with operator-meaningful controls.

## Why This Packet Exists

The current system stores documents but does not cleanly answer whether they are actually ready for retrieval. Reindex is also just a stub.

## In Scope

1. Define `query_ready` behavior.
2. Define readiness rules per lifecycle state.
3. Expose readiness through document and status APIs.
4. Define behavior for not-query-ready documents.
5. Replace `/ingest/reindex` placeholder behavior with real semantics.
6. Define reindex scope options.
7. Define safe requeue and resubmission rules.

## Out Of Scope

1. Full company retrieval behavior
2. LightRAG Web UI exposure
3. Custom admin UI
4. Code extraction from installer

## Dependencies

1. Packet 05

## Required Decisions

1. Whether `query_ready` is derived or stored
2. Whether `/query` itself should enforce readiness
3. Whether reindex immediately resubmits or marks for worker handling
4. Whether rejected docs can be reindexed only with force behavior
5. What reindex does to failed docs with exhausted retries

## Implementation Guidance

Recommended `query_ready` rule:

1. `status == ingested`
2. not superseded by a newer active logical version
3. not blocked by validation or split-required state

Recommended `readiness_reason` values:

1. `ingested`
2. `pending_validation`
3. `validation_rejected`
4. `awaiting_submit`
5. `processing_upstream`
6. `upstream_failed`
7. `split_required`
8. `superseded`

Recommended reindex behavior:

1. mark explicit state for worker handling
2. do not bypass worker logic
3. support scoped requeueing

## Expected Deliverables

1. Query-ready rule definition
2. Readiness reason list
3. Reindex request and response contract
4. Reindex worker interaction note
5. Example API JSON for ready vs not-ready docs

## Acceptance Criteria

1. `query_ready` is explicitly defined.
2. Operators can tell why a doc is not queryable.
3. `/ingest/reindex` has real behavior semantics.
4. Reindex does not conflict with worker dedupe and retry rules.
5. Failed or stale docs can be intentionally requeued.

## Verification Steps

1. Verify an ingested doc reports `query_ready=true`.
2. Verify a validation-rejected doc reports `query_ready=false` with a useful reason.
3. Verify a processing doc reports `query_ready=false`.
4. Verify reindex behavior for failed docs.
5. Verify forced reindex path for blocked or rejected docs if supported.

## Evidence Required

1. Packet summary
2. Readiness rules
3. Reindex semantics
4. Example request and response
5. Verification commands
6. Observed outputs
7. Remaining risks

## Review Checklist

1. Readiness is simple and explicit.
2. Operators can answer “why is this not searchable?”
3. Reindex semantics are consistent.
4. Reindex delegates through worker semantics rather than bypassing them.

## Risks / Open Questions

1. Derived vs stored readiness may affect implementation complexity.
2. Forced reindex semantics must stay compatible with validation policy.

## Execution Notes

Avoid broad query-engine changes in this packet. Focus on status truth and controlled operator actions.

## Result

Pending execution and review.
