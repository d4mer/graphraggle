# Packet 03: Schema And Status API Expansion
## Status

in_progress

## Objective

Expand persistent document state and status APIs so operators can understand validation, lifecycle, failures, and readiness from the gateway without digging into raw internals.

## Why This Packet Exists

Once lifecycle and validation models are defined, the repo needs schema and API support to persist and surface those models cleanly.

## In Scope

1. Expand document state schema.
2. Define migration strategy from the current schema.
3. Expand `/documents` response shape.
4. Expand `/documents/{id}` response shape.
5. Expand `/ingest/status` summary shape.
6. Define operator-friendly filtering and summary slices.

## Out Of Scope

1. Upload collision handling.
2. Worker retry semantics.
3. Company-scoped retrieval logic.
4. UI exposure work.

## Dependencies

1. Packet 00.
2. Packet 01.
3. Packet 02.

## Required Decisions

1. Which fields are nullable during migration.
2. Whether `query_ready` is derived or stored.
3. Which summary slices are mandatory in `/ingest/status`.

## Implementation Guidance

Target fields should include:

1. `document_id`
2. `path`
3. `source_type`
4. `company`
5. `filename`
6. `original_filename`
7. `content_hash`
8. `status`
9. `validation_state`
10. `error_stage`
11. `error_code`
12. `error_message`
13. `error_detail`
14. `warnings_json`
15. `detected_mime`
16. `detected_encoding`
17. `byte_size`
18. `page_count`
19. `retry_count`
20. `track_id`
21. `query_ready`
22. `source_uri`
23. `supersedes_document_id`
24. `created_at`
25. `updated_at`
26. `ingested_at`

Recommended summary slices:

1. by `status`
2. by `validation_state`
3. by `error_stage`
4. by `company`

## Expected Deliverables

1. Schema delta list.
2. Migration strategy.
3. Target example API JSON.
4. Summary slice definitions.

## Acceptance Criteria

1. Old rows remain readable.
2. New rows can carry richer metadata.
3. Operators can answer “what failed and why?” from API output.
4. Packet 04 and Packet 05 can build on this without inventing new fields.

## Verification Steps

1. Verify migration plan against current schema.
2. Produce example JSON for:
   1. accepted but not submitted doc
   2. rejected doc
   3. failed doc
   4. ingested query-ready doc
3. Verify summary slices can represent at least one example per category.

## Evidence Required

1. Packet summary
2. Schema delta
3. Migration note
4. Example API JSON
5. Summary slice design
6. Risks / open questions

## Review Checklist

1. New fields align with Packet 01 and Packet 02.
2. Migration path is safe for existing rows.
3. API output is operator-usable, not just internally complete.
4. The packet stays out of upload and worker behavior changes except where required for illustration.

## Risks / Open Questions

1. Derived vs stored `query_ready` may affect implementation complexity.
2. Some fields may remain unpopulated until later packets land.

## Execution Notes

Keep migration and API contract work ahead of behavior-changing packets so later implementation does not invent ad hoc fields.

## Result

Implementation complete. Schema expanded with 9 new nullable columns. API responses enhanced with all 26 target fields. Summary slices expanded with error_stage and company groupings. Migration strategy defined and idempotent. ADR 0007 created. Syntax validated. Awaiting review and deployment.

## Execution Notes

### Decisions Made
1. **Nullable columns**: All new columns are nullable to ensure backward compatibility with existing rows.
2. **sha256 kept**: Database keeps `sha256` as primary column; `content_hash` added as copy for API-level canonical naming.
3. **query_ready stored**: Remains stored (not derived) to avoid recomputation on every API call.
4. **byte_size populated immediately**: Available at upload/scan time, no deferred population needed.
5. **ingested_at populated immediately**: Set via `datetime('now')` when worker transitions to ingested status.
6. **Deferred fields**: `detected_mime`, `page_count`, `source_uri`, `warnings_json` remain NULL until later packets implement detection logic.

### Required Decisions Resolved
1. **Nullable during migration**: All new columns nullable — old rows remain readable.
2. **query_ready derived vs stored**: Stored (consistent with Packet 01 implementation).
3. **Summary slices mandatory**: by_status, by_validation_state, by_query_ready, by_error_stage, by_company.

### Migration Plan
Each new column added via `ALTER TABLE ADD COLUMN`. Existing columns checked via `PRAGMA table_info(documents)`. Idempotent — safe to run multiple times.
