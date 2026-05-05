# Result - Packet 07: Company Attribution And Retrieval Scoping

## Summary

Implemented deterministic company attribution and explicit retrieval scoping metadata without expanding into full multi-tenancy, auth, or UI work.

The implementation stays inside Packet 07 scope. It builds on the accepted Packet 01-06 lifecycle, validation, readiness, and operator-state contracts while making company provenance visible and query scoping behavior predictable.

## Files Changed

| File | Change |
|------|--------|
| `app/state_store.py` | Added persistent company provenance fields, attribution helpers, batch path lookup, and `by_company_source` summary output |
| `app/api.py` | Persisted upload provenance, enriched query citations with company metadata, and returned explicit `query_scope` behavior |
| `app/worker.py` | Inferred filesystem company from the first `source_docs/` directory and refreshed provenance during scans |
| `docs/06-api-reference.md` | Documented Packet 07 attribution and query scoping behavior |
| `docs/ops/runs/packet-07-result.md` | Added Packet 07 summary |
| `docs/ops/runs/packet-07-evidence.md` | Added Packet 07 evidence and QA commands |
| `CHANGELOG.md` | Recorded Packet 07 changes |

## What Changed And Why

### 1. Company attribution is now deterministic and explainable

- Uploads keep Packet 04 optional `company` input, but now persist whether the value came from `request.company` or remained unscoped.
- Filesystem ingestion now infers `company` from the first directory under `source_docs/`, matching ADR 0004 and the Packet 07 recommended rule.
- The worker preserves explicit upload attribution and only refreshes provenance automatically for filesystem rows.
- Operators can inspect both `company_source` and `company_source_detail` through the existing document and status APIs.

### 2. Provenance is visible without introducing a new tenancy model

- Document rows now persist `company_source` values of `explicit`, `path_inferred`, or `unscoped`.
- `/ingest/status` now includes `by_company_source` so operators can quickly see how much content is explicitly attributed versus inferred versus unscoped.
- Existing rows get provenance refreshed during worker scans, so filesystem content can pick up Packet 07 attribution without a separate migration job.

### 3. Query scoping is explicit and predictable

- `/query` already accepted optional `company`; Packet 07 now makes that field operational at the gateway response layer.
- Company-scoped queries only return citations whose persisted `company` exactly matches the requested value.
- Unscoped documents are excluded from company-scoped query responses and remain visible only in unscoped queries.

### 4. The API is explicit about the scoping limit

- Query responses now include `query_scope` with the applied mode, policy, citation counts, and excluded citations.
- Returned citations are enriched with `document_id`, `company`, `company_source`, `company_source_detail`, `source_type`, `query_ready`, and `readiness_reason`.
- The response warns that Packet 07 scopes gateway-returned citations but does not claim hard LightRAG isolation internally.

## Outcome

1. Company attribution follows one precedence rule across upload and filesystem ingest.
2. Operators can see whether company came from explicit input, path inference, or no scope at all.
3. Company-scoped query responses no longer silently mix in unscoped or other-company citations.
4. The stack is more predictable for shared-graph retrieval while staying honest about the lack of full hard isolation.
