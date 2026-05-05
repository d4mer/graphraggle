# Packet 02: Validation Gate Design
## Status

in_progress

## Objective

Define the pre-ingest validation system that decides whether a document should be accepted, accepted with warnings, rejected, or flagged for auto-split before it reaches LightRAG.

## Why This Packet Exists

The current robustness gaps mostly appear before LightRAG ingestion: file type ambiguity, encoding surprises, oversized inputs, duplicate content, and opaque operator outcomes.

## In Scope

1. Define validation inputs.
2. Define file classification rules.
3. Define verdict rules.
4. Define warning rules.
5. Define duplicate-content policy.
6. Define initial size and page heuristics.
7. Define encoding handling policy.
8. Define operator-visible validation output.

## Out Of Scope

1. Full SQLite migration.
2. Worker retry logic.
3. Query gating.
4. Company retrieval behavior.
5. LightRAG Web UI exposure.

## Dependencies

1. Packet 00.
2. Packet 01.

## Required Decisions

1. Where validation starts: upload-time, worker-time, or both.
2. Whether MIME mismatch is always reject or sometimes warn.
3. How duplicates are represented.
4. How aggressive `auto_split` should be.
5. How extensionless text files are handled.

## Implementation Guidance

Recommended architecture:

1. `intake validation` at upload/discovery time
2. `pre-submit validation` in worker before LightRAG submission

Recommended validation verdicts:

1. `accept`
2. `warn`
3. `reject`
4. `auto_split`

Recommended file classes:

1. `text_like`
2. `binary_office`
3. `unsupported`

Recommended decode order for text-like files:

1. `utf-8`
2. `utf-8-sig`
3. `cp1252`
4. `latin-1`

## Expected Deliverables

1. Validation rules table.
2. Verdict matrix.
3. Duplicate policy note.
4. Threshold recommendations.
5. Example validation output JSON.

## Acceptance Criteria

1. Every supported file class has a defined validation path.
2. Unsupported classes have a defined rejection path.
3. Duplicate-content handling is defined.
4. Encoding fallback handling is defined.
5. Size and page split-risk handling is defined.
6. Packet 03 can implement schema support without policy ambiguity.

## Verification Steps

Walk at least these scenarios through the design:

1. clean UTF-8 `.txt`
2. `.txt` requiring `cp1252`
3. unsupported extension
4. large transcript `.txt`
5. duplicate content at different path
6. PDF over chosen threshold

## Evidence Required

1. Packet summary
2. Validation matrix
3. Threshold proposals
4. Duplicate policy
5. Encoding policy
6. Example JSON
7. Open questions

## Review Checklist

1. Validation rules are deterministic.
2. `warn` is useful rather than vague.
3. `auto_split` is clearly distinct from `reject`.
4. Design reflects the known long-transcript timeout issue.
5. Extensionless and ambiguous files are handled explicitly.

## Risks / Open Questions

1. Threshold values may need tuning after live testing.
2. Duplicate semantics may need refinement when logical versioning is added.

## Execution Notes

Recommended policy posture:

1. reject clearly invalid or unsupported files
2. warn on uncertain but workable files
3. auto-split large risky files
4. avoid over-rejecting initially

## Implementation Notes

Packet 02 implementation (2026-05-04):

1. Created `app/validation.py` with deterministic validation gate:
   - `classify_file()` – extension-based classification (text_like, binary_office, unsupported)
   - `validate_upload()` – lightweight upload-time validation (no duplicate check)
   - `validate_file()` – full worker-time validation with duplicate detection
   - `ValidationVerdict` – immutable verdict with structured meta
   - Size thresholds: `WARN_SIZE_THRESHOLD=100KB`, `AUTO_SPLIT_THRESHOLD=500KB`
   - Encoding fallback detection (utf-8, utf-8-sig, cp1252, latin-1)

2. Updated `app/api.py` (in installer heredoc):
   - Replaced inline extension check with `validate_upload()` call
   - Upload endpoint now returns validation verdict in response metadata
   - Empty files rejected with `error_code=empty_file`
   - Large text files get `auto_split` or `warn` verdicts
   - Import: `from .validation import SUPPORTED_EXTENSIONS, validate_upload`

3. Updated `app/worker.py` (in installer heredoc):
   - `submit_file()` now calls `validate_file()` before LightRAG submission
   - Reject verdict → document set to `rejected` with validation error fields
   - Auto_split verdict → document set to `auto_split`, not submitted
   - Warn verdict → document set to `warn`, proceeds to submission
   - Import: `from .validation import validate_file`
   - Removed redundant `validation_state="accept"` from `handle_candidate()` (handled by submit_file)

4. Created ADR 0006 (Validation Gate Design) in `docs/decisions/`

5. Verdict matrix implemented:
   - unsupported extension → reject
   - empty file (0 bytes) → reject
   - duplicate SHA-256 → warn
   - text_like >= 500KB → auto_split
   - text_like >= 100KB → warn
   - fallback encoding → warn
   - everything else → accept

## Result

in_progress — implementation complete, awaiting review.
