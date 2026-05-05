# Packet 07: Company Attribution And Retrieval Scoping

## Status
ready

## Objective

Add reliable company-aware provenance and define predictable retrieval scoping behavior in the shared graph model.

## Why This Packet Exists

Company exists in the schema conceptually but is not used meaningfully in the live flow. Shared-graph retrieval can therefore cross company boundaries unexpectedly.

## In Scope

1. Define company attribution source precedence.
2. Persist company attribution through upload and filesystem ingest.
3. Define folder-based inference rules.
4. Define behavior for missing company.
5. Define query scoping semantics.
6. Define cross-company retrieval policy.
7. Define operator-facing provenance visibility.

## Out Of Scope

1. Full multi-tenant hard isolation
2. LightRAG internal redesign
3. External auth or RBAC
4. LightRAG Web UI exposure

## Dependencies

1. Packet 03
2. Packet 05
3. Packet 06
4. ADR 0004

## Required Decisions

1. Canonical folder inference rule
2. Whether unscoped docs are allowed in company-scoped queries
3. Whether unscoped docs are allowed in unscoped queries
4. Whether `company_source` is stored
5. Whether uploads require company immediately or keep it optional

## Implementation Guidance

Recommended source precedence:

1. explicit request/company value
2. filesystem path inference
3. null / unscoped

Recommended first filesystem rule:

1. first directory under `source_docs/` is the company key

Recommended initial retrieval policy:

1. company-scoped queries return matching company only
2. unscoped queries may include unscoped docs
3. unscoped document behavior must be explicit in docs and APIs

## Expected Deliverables

1. Company attribution policy
2. Folder inference rule
3. Query scoping rule set
4. Provenance field additions
5. Example request and response payloads

## Acceptance Criteria

1. Company attribution is captured consistently.
2. Company source is explainable.
3. Scoped queries behave predictably.
4. Unscoped docs are not silently mixed into scoped results unless explicitly allowed.
5. Operators can inspect company provenance via API output.

## Verification Steps

1. Upload with explicit company.
2. Filesystem ingest with folder-inferred company.
3. Unscoped upload.
4. Company-scoped query.
5. Unscoped query.

## Evidence Required

1. Packet summary
2. Attribution policy
3. Query scoping policy
4. Example requests and responses
5. Verification commands
6. Observed outputs
7. Remaining risks

## Review Checklist

1. Company attribution is deterministic.
2. Company source is visible.
3. Cross-company behavior is explicit.
4. Unscoped docs are handled intentionally.

## Risks / Open Questions

1. Unscoped-doc policy may need tightening later.
2. Company-scoped query behavior may eventually need stricter enforcement.

## Execution Notes

This packet should improve provenance and scoping without pretending to be full hard multi-tenancy.

## Result

Pending execution and review.
