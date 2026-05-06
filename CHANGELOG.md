# Changelog

## 2026-05-06

### Packet 12: Retrieval Usability Hotfix

1. Updated gateway query guidance to reflect default `hybrid` mode when omitted and one-time fallback to `naive` on weak retrieval.
2. Added transcript-query operator guidance recommending speaker/date anchors plus short quote fragments.
3. Added worker-side minimal transcript normalization before `/documents/text` submission (newline collapse, safe timestamp-noise cleanup, whitespace trimming).
4. Added executable helper script `scripts/reindex_sct_docs.sh` for dry-run/apply reindex of matching SCT/workshop GSK docs via `/ingest/status` plus `/ingest/reindex`.
5. Updated API/admin/operator/OpenWebUI/quick-reference docs and script docs for Packet 12 operations.

## 2026-04-24

### Added

1. Initial GraphRAG implementation repository structure
2. Full operator documentation set under `docs/`
3. Installer script under `scripts/install_rag_stack.sh`
4. Top-level repository README
5. Top-level TODO list for follow-up hardening work

### Validated

1. Stack startup with Podman on Mac mini M4
2. LightRAG image tag `ghcr.io/hkuds/lightrag:v1.4.15`
3. oMLX LLM endpoint at `192.168.1.190:1234/v1`
4. oMLX embedding endpoint at `192.168.1.180:1234/v1`
5. oMLX rerank endpoint at `192.168.1.180:1234/v1/rerank`
6. Full text retrieval from `rag-smoke.txt`

### Fixed

1. Incorrect LightRAG image tag without `v` prefix
2. Incorrect embedding model name `mxbai-embed-large`
3. LightRAG container command duplication issue
4. Worker ingestion path for text files by switching to `POST /documents/text`
5. Worker duplication of internal `source_docs/__enqueued__` files
6. Worker text decoding by adding fallbacks for non-UTF-8 text files like Windows-1252 transcripts
7. Unsupported file upload returning `pending` instead of `rejected` — added upload-time extension validation so unsupported files are immediately rejected with `status=rejected`, `validation_state=reject`, `error_stage=validation`, `error_code=unsupported_extension`
8. Pre-ingest validation gate (Packet 02) — added `app/validation.py` with deterministic file classification, verdict assignment (`accept`, `warn`, `reject`, `auto_split`), duplicate detection, size thresholds, and encoding fallback detection; integrated into upload endpoint and worker pre-submission path

### Packet 03: Schema And Status API Expansion (2026-05-04)

1. Added 9 new nullable columns to `documents` table: `content_hash`, `detected_mime`, `detected_encoding`, `byte_size`, `page_count`, `source_uri`, `supersedes_document_id`, `ingested_at`, `warnings_json`
2. Added 9 corresponding migration entries in `MIGRATIONS` dict (idempotent, skip-if-exists)
3. Enhanced `upsert_document` and `update_document_state` to handle new columns
4. Expanded `/ingest/status` summary with `by_error_stage` and `by_company` slices
5. Enhanced upload endpoint response to include all 26 target fields
6. Worker now sets `ingested_at` when document reaches `ingested` status
7. Worker now passes `byte_size` in upsert calls for filesystem-scanned files
8. Created ADR 0007 documenting schema expansion decisions

### Packet 06: Query Readiness And Reindex Controls (2026-05-05)

1. Added canonical derived `query_ready` and `readiness_reason` logic to document/status API output
2. Expanded `/ingest/status` summary with `by_readiness_reason`
3. Added `app/models.py` to check in the API request and response contract, including scoped `ReindexRequest` selectors
4. Replaced `/ingest/reindex` stub with worker-routed requeue semantics using explicit operator scope filters and `force` gating
5. Updated worker scan logic to honor operator reindex requests without bypassing validation or submit/poll behavior
6. Updated operator docs and Packet 06 evidence with readiness and reindex QA commands

### Packet 07: Company Attribution And Retrieval Scoping (2026-05-05)

1. Added persistent `company_source` and `company_source_detail` provenance fields to document state plus `by_company_source` summary output
2. Standardized company attribution precedence as explicit request value, then filesystem path inference, then unscoped null
3. Implemented filesystem inference from the first directory under `source_docs/` and backfilled that attribution during worker scans
4. Expanded upload and query API payloads to expose company provenance directly to operators
5. Added explicit `/query` scoping metadata and exact-match company citation filtering for company-scoped requests without claiming hard LightRAG multi-tenancy
6. Updated Packet 07 ops docs and API reference with provenance and scoping policy, verification commands, and QA guidance

### Packet 08: LightRAG Web UI Exposure (2026-05-05)

1. Added dedicated port mapping `9622:9621` to `compose.yml` for intentional LightRAG Web UI exposure on trusted LAN
2. Updated ADR 0005 with exposure method decision (dedicated port) and auth decision (deferred to first pass)
3. Added LightRAG Web UI section to admin operations doc with access URL, intended use, and security note
4. Added LightRAG Web UI URL to known-good config
5. Added LightRAG Web UI entry to operator cheat sheet with purpose distinction
6. Added step 6b (LightRAG Web UI admin/debug) to daily workflow
7. Added three-surface distinction table to Open WebUI guide clarifying when to use each surface
8. Updated Packet 08 packet file with accepted decisions and implementation result
