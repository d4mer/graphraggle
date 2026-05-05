# ADR 0006: Validation Gate Design

## Status

accepted

## Context

The ingestion pipeline had no pre-ingest validation logic. Unsupported file
extensions were caught only after the worker scanned the filesystem (30-second
interval), files were submitted to LightRAG without size or encoding checks,
and all validation outcomes collapsed to a single `accept` verdict before
submission. This led to opaque timeouts, wasted ingestion cycles on empty or
duplicate files, and no operator visibility into why a file was rejected or
flagged.

ADR 0002 established the `validation_state` field with five values
(`not_run`, `accept`, `warn`, `reject`, `auto_split`) but left the actual
verdict logic undefined. Packet 02 fills this gap.

## Decision

Implement a deterministic pre-ingest validation gate in `app/validation.py`
that classifies files and assigns one of four verdicts before LightRAG
submission.

### File Classification Rules

| Extension set | Class | Examples |
|---|---|---|
| `.txt`, `.md`, `.html`, `.htm`, `.json`, `.csv` | `text_like` | Plain text, structured text |
| `.pdf`, `.docx`, `.pptx`, `.xlsx` | `binary_office` | PDF, Office docs |
| Any other extension | `unsupported` | `.bin`, `.exe`, `.zip` |

### Verdict Rules (deterministic, ordered)

1. **reject** – Unsupported extension (`error_code: unsupported_extension`)
2. **reject** – Empty file, 0 bytes (`error_code: empty_file`)
3. **warn** – Duplicate SHA-256 hash (`error_code: duplicate_content`)
4. **auto_split** – Text-like file >= 500 KB (`error_code: large_text_file`)
5. **warn** – Text-like file >= 100 KB (`error_code: large_text_file`)
6. **warn** – Fallback encoding used (`error_code: fallback_encoding`)
7. **accept** – Everything else

### Size Thresholds

| Threshold | Value | Behavior |
|---|---|---|
| `warn` | 100 KB | Accept with warning; alert operator |
| `auto_split` | 500 KB | Flag for splitting; do not submit as-is |

These thresholds are tunable constants. They may need tuning after live
testing per the risk noted in Packet 02 design.

### Duplicate Detection

Uses an in-process SHA-256 hash set (`_seen_hashes`). Works for files scanned
multiple times within the same worker process run. Cross-process deduplication
will be handled by Packet 03 when the database supports persistence.

### Encoding Policy

When validating text-like files, the module attempts decoding in this order:
1. `utf-8`
2. `utf-8-sig`
3. `cp1252`
4. `latin-1`

If any encoding other than UTF-8 variants is needed, the verdict is `warn`
with `error_code: fallback_encoding`.

### Two-Phase Validation

- **Upload-time** (`validate_upload`): Lightweight check at `/upload`. No
  duplicate detection (database unavailable). Returns verdict for immediate
  operator feedback.

- **Worker pre-submission** (`validate_file`): Full check before LightRAG
  submission. Includes duplicate detection via SHA-256. Used in `worker.py`
  `submit_file()` before the LightRAG API call.

## Alternatives Considered

1. **Reject large text files outright instead of auto_split.**
   Rejected because the Packet 02 execution notes recommend "avoid over-rejecting
   initially." Auto_split preserves the file for later splitting.

2. **Use MIME type sniffing instead of extension-based classification.**
   Rejected because the existing codebase uses extension-based classification
   (SUPPORTED_EXTENSIONS in api.py, SUPPORTED in worker.py). Adding MIME
   sniffing is scope for Packet 04 (upload intake hardening).

3. **Persist duplicate hashes in the database immediately.**
   Rejected because the database schema and persistence mechanism are defined
   in Packet 03. Process-scoped dedup is sufficient for the current worker
   deployment model (single process).

## Consequences

1. Unsupported files are rejected at upload time with clear error fields.
2. Empty files are rejected before reaching LightRAG.
3. Large text files are flagged auto_split, preventing opaque LightRAG timeouts.
4. Duplicate content is detected and surfaced as a warning.
5. Encoding fallbacks are detected and surfaced as warnings.
6. The validation logic is deterministic and documented in ADR 0006.
7. Later packets can build on the `ValidationVerdict` class and verdict constants
   without redefining the policy.

## Affected Packets

1. Packet 02 (this ADR)
2. Packet 03 (will persist validation metadata in database)
3. Packet 04 (can extend classification to MIME-based)
4. Packet 10 (can consolidate duplicate extension constants)

## Open Follow-Ups

1. Threshold tuning after live testing (noted in Packet 02 design doc)
2. Persistent duplicate tracking (Packet 03)
3. MIME-based classification (Packet 04)
4. Actual auto_split implementation (can be Packet 05 or standalone)
