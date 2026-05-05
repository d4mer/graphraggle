# ADR 0007: Persistent Document State Schema Expansion

## Status

accepted

## Context

Packet 01 defined the lifecycle state model and Packet 02 defined the validation verdict model. Both packets added database columns and API behavior, but the schema lacked several fields needed for operator-friendly document introspection. Operators need to answer "what failed and why?" from API output without digging into raw internals.

The target field list from Packet 03 includes 26 fields, of which 10 are new and need migration support. The system must remain backward-compatible: old rows must remain readable, and new rows must carry richer metadata.

## Decision

1. Add 9 new nullable columns to the `documents` table:
   - `content_hash TEXT` — alias for `sha256`, exposed in API responses as the canonical field
   - `detected_mime TEXT` — MIME type detected during validation (deferred population to later packets)
   - `detected_encoding TEXT` — Encoding detected during text file validation
   - `byte_size INTEGER` — File size in bytes, populated at upload/scan time
   - `page_count INTEGER` — Page count for PDFs (deferred population to later packets)
   - `source_uri TEXT` — Source URI for non-file-based documents (deferred population to later packets)
   - `supersedes_document_id TEXT` — Document ID this version replaces
   - `ingested_at TEXT` — Timestamp when document reached `ingested` status
   - `warnings_json TEXT` — Structured warning data from validation

2. Migration strategy:
   - Use `ALTER TABLE ADD COLUMN` with nullable defaults for each new column
   - Existing rows get NULL for all new columns (safe, backward-compatible)
   - New rows populated with available data; deferred fields remain NULL until later packets implement detection
   - Migration is idempotent: columns already present are skipped by `PRAGMA table_info` check

3. API response expansion:
   - `/documents` returns full document records with all target fields
   - `/documents/{id}` returns the same enriched document record
   - `/ingest/status` returns summary slices by `status`, `validation_state`, `query_ready`, `error_stage`, and `company`
   - Upload endpoint response includes all new fields (populated where available, NULL otherwise)

4. `query_ready` remains stored (not derived):
   - It is set explicitly by the worker at each lifecycle transition
   - Derivation logic (status == "ingested" AND not superseded AND not blocked) can be added later without schema change
   - Keeping it stored avoids recomputation on every API call

5. `content_hash` as API-level alias:
   - Database keeps `sha256` as the primary column (avoids migration complexity)
   - API responses include `content_hash` set to the `sha256` value
   - New column `content_hash` added to database as a copy for future decoupling

## Consequences

1. Operators can answer "what failed and why?" from API output via `error_stage`, `error_code`, `error_message`, `error_detail`, and `validation_state`.
2. Summary slices by `error_stage` and `company` enable quick operational dashboards.
3. Existing rows remain readable — all new columns are nullable with no NOT NULL constraints.
4. Deferred fields (`detected_mime`, `page_count`, `source_uri`, `warnings_json`) remain NULL until later packets implement detection logic.
5. `byte_size` and `ingested_at` are populated immediately by current code changes.

## Alternatives Considered

1. Rename `sha256` to `content_hash` in the database.
   Rejected because it requires a complex migration (create new, copy, drop old) and breaks Packet 01/02 deployed rows.

2. Derive `query_ready` from `status` in the API layer.
   Rejected because it would require recomputation on every query and doesn't align with the stored approach used since Packet 01.

3. Populate all new fields at upload time.
   Rejected because MIME detection, page count, and source URI require external tools (file, poppler, etc.) that are out of scope for this packet.

## Affected Packets

1. Packet 04 (Upload Intake Hardening) — can build on `byte_size`, `content_hash`, and `warnings_json`
2. Packet 05 (Worker Hardening) — can use `ingested_at` for timing metrics
3. Packet 06 (Query Readiness And Reindex Controls) — can use `supersedes_document_id`
4. Packet 10 (Source Extraction Cleanup) — can populate `detected_mime` and `page_count`

## Open Follow-Ups

1. When to start populating deferred fields (`detected_mime`, `page_count`, `source_uri`, `warnings_json`)
2. Whether to add indexes on `error_stage`, `company`, and `status` for summary query performance
3. Whether `supersedes_document_id` needs a foreign key constraint or remains loose reference
