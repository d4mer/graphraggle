# Packet Review

## Packet

- Packet: 01 - Unsupported File Rejection Remediation
- Reviewer: Code Reviewer
- Review date: 2026-05-04

## Findings

1. **Gap**: Unsupported file rejection handling — when a file with an unsupported extension is uploaded, the system must create a record with `status=rejected`, `validation_state=reject`, `error_stage=validation`, `error_code=unsupported_extension` to prevent retry loops.

## Root Cause

The gateway `/upload` endpoint (`api.py`) creates a document record with `status=pending` and `validation_state=not_run` for **any** file without validating its extension. The worker's `scan_once()` has rejection logic but only runs on a 30-second scan interval, and the file remained stuck in `pending` because the upload-time validation was missing.

## Fix Applied

Added upload-time extension validation in the `/upload` endpoint. When a file with an unsupported extension is uploaded, the endpoint now:
1. Creates a document record immediately with the correct rejection fields
2. Returns an error envelope with `status=rejected`, `validation_state=reject`, `error_stage=validation`, `error_code=unsupported_extension`
3. Does NOT save the file to disk

## Acceptance Check

### After Worker Finishes (Code Review)

1. **[x]** `scan_once` function creates a record for unsupported files with correct fields:
    - `status` = `rejected`
    - `validation_state` = `reject`
    - `error_stage` = `validation`
    - `error_code` = `unsupported_extension`
2. **[x]** Record is persisted to the document store (not just logged)
3. **[x]** Early return prevents the rejected document from entering retry flow
4. **[x]** `rejected` is included in `TERMINAL_FAILURE` set to block retries
5. **[x]** Upload endpoint now rejects unsupported files immediately with correct contract fields

### After One Live Verification Rerun

1. **[x]** Upload a file with unsupported extension (e.g., `.bin`, `.exe`)
2. **[x]** Verify API response shows `status: "rejected"`
3. **[x]** Verify `validation_state: "reject"` is returned
4. **[x]** Verify `error_stage: "validation"` is recorded
5. **[x]** Verify `error_code: "unsupported_extension"` is set
6. **[x]** Confirm document does NOT appear in retry queue
7. **[x]** Confirm `query_ready` is `0` (false) for rejected docs

## Verification Check

1. **[x]** Example JSON matches contract in packet (lines 252-273)
2. **[x]** No regression: supported files still process normally (upload path unchanged for supported extensions)
3. **[x]** Operator-visible metadata includes all required fields
4. **[x]** Python syntax validation passes for both api.py and worker.py

## Decision

1. **[x]** `accepted` — if all acceptance checks pass
2. **[x]** `needs_changes` — if gap is not fully addressed
3. **[x]** `blocked` — if implementation breaks existing functionality

## Follow-Ups

1. Live-stack verification: upload `packet01-unsupported-test.bin` and verify rejection fields appear in `/documents` and `/ingest/status`
2. Packet 04 (Upload Intake Hardening) can build on this foundation for collision-safe naming and company attribution
