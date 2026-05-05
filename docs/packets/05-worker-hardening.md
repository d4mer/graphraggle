# Packet 05: Worker Hardening

## Status
ready

## Objective

Make asynchronous ingestion resilient, content-aware, and operator-debuggable so documents do not get stuck, silently duplicate, or fail opaquely.

## Why This Packet Exists

The worker is where most real robustness problems surface:

1. retries are too coarse
2. failures can become terminal too early
3. dedupe is too path-centric
4. stale `track_id` handling is weak
5. worker behavior is not aligned with richer lifecycle and validation semantics

## In Scope

1. Harden dedupe behavior around content hash.
2. Distinguish transient vs terminal failures.
3. Define retry rules by stage and error type.
4. Define stale `track_id` replacement behavior.
5. Define resubmission behavior after failure.
6. Record worker-side validation and decode outcomes.
7. Define how duplicate candidates are treated.
8. Define interim behavior for `auto_split` candidates.

## Out Of Scope

1. UI changes
2. Final reindex controls
3. Company-scoped retrieval
4. LightRAG Web UI exposure
5. Code extraction from installer
6. Fully automated splitting

## Dependencies

1. Packet 03
2. Packet 04

## Required Decisions

1. Whether duplicate-content files are skipped or merely marked
2. What counts as transient poll failure
3. Retry thresholds for submit and poll failures
4. Whether content change invalidates previous track immediately
5. Which lifecycle state `auto_split` documents remain in

## Implementation Guidance

Recommended worker flow:

1. discover candidate
2. classify and validate
3. compute content hash
4. look up prior document state
5. decide skip, update, retry, resubmit, or block
6. submit to LightRAG if eligible
7. poll status intelligently
8. update lifecycle and error fields

Recommended duplicate behavior:

1. same hash plus already ingested -> skip or mark duplicate
2. same hash at new path -> operator-visible duplicate candidate
3. different hash at same path -> treat as new content version

## Expected Deliverables

1. Worker decision matrix
2. Retry classification note
3. Stale `track_id` policy
4. Duplicate-content handling note
5. Status and error mappings for worker actions

## Acceptance Criteria

1. Worker behavior is content-aware, not just path-aware.
2. Transient and terminal failures are distinguishable.
3. Stale `track_id` handling is explicit.
4. Duplicate-content handling is deterministic.
5. `auto_split` docs do not get blindly submitted.
6. Worker-updated document state is intelligible to operators.

## Verification Steps

1. Repeat scan with same content at same path.
2. Same content at different path.
3. Changed file at same path.
4. Transient upstream or poll failure scenario.
5. Stale track plus content-change scenario.
6. Large transcript with `auto_split`.

## Evidence Required

1. Packet summary
2. Decision matrix
3. Retry policy
4. Track policy
5. Verification commands
6. Observed outputs
7. Remaining risks

## Review Checklist

1. Dedupe is content-aware.
2. Retries are stage-aware enough to be useful.
3. Stale-track issues are distinguishable from real upstream failures.
4. `auto_split` documents are blocked clearly.
5. Packet scope did not drift into reindex implementation.

## Risks / Open Questions

1. Retry tuning may need live-stack adjustment.
2. Duplicate-content semantics may need later versioning refinement.

## Execution Notes

This packet should align worker behavior with the contracts defined in Packets 01-03, not invent new state semantics.

## Result

Pending execution and review.
