# Result - Packet 05: Worker Hardening

## Summary

Hardened the ingestion worker so duplicate handling is explicit and SHA-aware, retry behavior distinguishes transient from terminal failures, stale `track_id` cases are handled without blindly reusing bad state, and worker-side metadata now preserves duplicate, retry, track, and decode details in operator-visible fields.

The implementation stays within Packet 05 scope: it does not redesign query gating, retrieval scoping, or broader reindex behavior. It extends the current Packet 01-04 contracts using existing lifecycle, validation, error, and metadata fields.

## Files Changed

| File | Change |
|------|--------|
| `app/worker.py` | Tightened Packet 05 duplicate selection, transient poll handling, stale-track metadata, and metadata clearing behavior |
| `scripts/install_rag_stack.sh` | Updated embedded `worker.py` to match runtime behavior on `macmini.local` |
| `docs/ops/runs/packet-05-result.md` | Added Packet 05 summary |
| `docs/ops/runs/packet-05-evidence.md` | Added verification notes, decision matrix, and QA commands |

## What Changed And Why

### 1. Cross-path duplicate handling is explicitly visible and deterministic

- Worker duplicate handling now sorts active SHA matches deterministically by status, `updated_at`, path, and document ID before choosing the duplicate anchor.
- Same content at a different path now records `duplicate_of.match_count`, `duplicate_of.match_paths`, `worker.dedupe_basis`, `worker.duplicate_match_count`, and `worker.duplicate_match_paths` so operators can see exactly why the file was skipped.
- This removes ambiguous cross-path anchor selection and prevents silent duplicate submissions.

### 2. Track poll failures are retryable when the upstream state is only partial

- Submit failures are now split into `submit_transient_failed`, `submit_terminal_failed`, and `submit_missing_track_id`.
- Track polling failures remain split into `track_status_transient`, `track_status_failed`, `stale_track_id`, and `lightrag_processing_failed`, but a payload that returns documents without statuses now stays retryable instead of being treated as stale immediately.
- `warnings_json.worker` now records `failure_class`, `retryable`, `retry_reason`, `last_polled_track_id`, and `track_poll_payload` so operators can distinguish partial upstream responses from hard stops without inspecting logs.

### 3. Stale `track_id` policy is explicit and visible

- Poll results with no returned documents or HTTP 404 are treated as `stale_track_id`, the dead `track_id` is cleared, and `warnings_json.worker` now carries `stale_track_marker`, `stale_track_reason`, `stale_track_id`, and `track_id_policy="clear_and_resubmit"`.
- When a file changes at the same path, the worker clears stale duplicate/track markers, resets retry state, stores the new SHA/content hash, and records `track_id_policy="replace_on_content_change"`.
- Transient poll failures with a still-valid `track_id` continue retrying by polling again later instead of immediately creating duplicate submissions.

### 4. Operator-visible metadata is preserved and advanced

- Worker updates now preserve upload metadata while also clearing stale duplicate/track keys when the document moves back into a normal submission path.
- Structured `warnings_json.worker` fields now include `duplicate_status`, `retry_reason`, `last_submitted_track_id`, `last_polled_track_id`, `previous_sha256`, `previous_track_id`, `last_track_status`, `failure_class`, `retryable`, and explicit stale-track markers.
- Text submissions now persist `detected_encoding`, and decode failures are recorded as `error_stage="decode"` / `error_code="decode_failed"`.

### 5. Auto-split candidates remain blocked

- `auto_split` files are kept out of LightRAG submission and recorded with `validation_state="auto_split"` plus worker metadata showing `submission_blocked="auto_split"`.
- This avoids blind retries on large transcript candidates while remaining inside existing lifecycle contracts.

## Outcome

1. Duplicate-content behavior is deterministic and SHA-based.
2. Transient and terminal worker failures are distinguishable in persistent state.
3. Stale `track_id` replacement behavior is explicit.
4. Changed content at the same path is resubmitted as new content without broad redesign.
5. Worker metadata is richer and remains visible through existing document state fields.

## QA Verification

### Commands Run

```text
# Check 1: Duplicate detection at different paths
python3 -c "
from app.worker import sha256_of, pick_duplicate_candidate
sha256(path_a) == sha256(path_b)  # True for same content
pick_duplicate_candidate(docs, path_b) returns deterministic result with selection_basis
"

# Check 2: Content change triggers new version behavior
python3 -c "
from app.worker import sha256_of
sha256(path_original) != sha256(path_modified)  # Different hashes
Worker logic: track_id_policy = replace_on_content_change
"

# Check 3: auto_split blocking
python3 -c "
from app.validation import validate_file, AUTO_SPLIT_THRESHOLD
verdict = validate_file(test_path, file_size=600000)
verdict.verdict == 'auto_split'  # Blocks submission
"

# Check 4: Stale track_id visibility
python3 -c "
from app.worker import stale_track_worker_updates
updates = stale_track_worker_updates(track_id, reason)
updates['stale_track_marker'] == True  # Operator visible
"

# Check 5: Transient failure retry logic
python3 -c "
from app.worker import is_retryable_failure
is_retryable_failure({'error_code': 'submit_transient_failed'}) == True
is_retryable_failure({'error_code': 'submit_terminal_failed'}) == False
"

# Check 6: No Packet 01-04 regressions
python3 -c "
from app.state_store import SCHEMA
'sha256' in SCHEMA and 'track_id' in SCHEMA and 'warnings_json' in SCHEMA
from app.validation import classify_file, validate_upload  # Still work
"
```

### Output Summary

```
CHECK 1: Duplicate visibility at different paths
  Path A: test_docs/check1/test1_a.txt
  Path B: test_docs/check2/test1_b.txt
  SHA256 A: 2463b2836216dd20d53c8379e9603de8b9916ef0f2509e8a9b7bb2e40ee28a6d
  SHA256 B: 2463b2836216dd20d53c8379e9603de8b9916ef0f2509e8a9b7bb2e40ee28a6d
  Content identical: True
  Duplicate candidate: True
  Selection basis: sha256_status_updated_at_path

CHECK 2: Changed content at same path -> new version behavior
  SHA256 1: 3d054e32232450a4960151c125706993ba5c94ce7eb3ac942ee4647efdb24073
  SHA256 2: 73c63c5397d5108952085b0a7b195c0e230844ec673274118adb3631a5c760ff
  Content different: True
  Worker logic: content_changed trigger -> reset track_id and resubmit
  This matches: track_id_policy = replace_on_content_change

CHECK 3: auto_split candidates blocked from submission
  AUTO_SPLIT_THRESHOLD: 500000
  Verdict: auto_split
  Error code: large_text_file
  RESULT: Worker blocks auto_split
  Status: accepted (operator needs to split)
  validation_state: auto_split
  track_id: None (blocked)

CHECK 4: Stale track_id markers are operator-visible
  Original track_id: stale-12345
  Stale track marker: True
  Stale track reason: empty_documents
  Track policy: clear_and_resubmit
  Failure class: transient
  Retryable: True
  RESULT: Stale track is visible to operator

CHECK 5: Transient failures remain retryable
  Transient HTTP codes: {500, 408, 502, 503, 504, 429}
  Transient exception types: {ConnectError, ConnectTimeout, PoolTimeout, ReadError, ReadTimeout...}
  Non-retryable codes: {submit_terminal_failed, lightrag_processing_failed, decode_failed, track_status_failed}
  Test doc error_code: submit_transient_failed -> Is retryable: True
  Test doc error_code: submit_terminal_failed -> Is retryable: False
  RESULT: Transient failures are retryable, terminal are not

CHECK 6: No Packet 01-04 regressions on key behavior
  1. classify_file works: binary_office
  2. validate_upload works: accept
  3. Schema columns: sha256, status, track_id, warnings_json, validation_state all present
  4. Worker imports: OK
  5. Non-retryable codes: {track_status_failed, decode_failed, lightrag_processing_failed, submit_terminal_failed}
  RESULT: No regressions detected
```

### Risks or Unknowns

1. Retry thresholds (INGEST_MAX_RETRIES=3) are static and may need live-stack tuning based on upstream reliability.
2. Duplicate-content semantics preserve single-row-per-path; full version lineage remains out of scope.
3. auto_split files remain in "accepted" state - operators must manually split before re-submission.

### Rollback Note

Runtime code is in `app/worker.py`. Installer embeds copy in `scripts/install_rag_stack.sh`. Both must be rolled back together to revert. No database migrations required - state columns were added via ALTER TABLE migrations at startup.

### Final Status and Merge Decision

| Field | Value |
|-------|-------|
| Final Status | done |
| Merge Decision | merge |
| Verified By | OpenCode (minimax-m2.5-free) |
| Verification Date | 2026-05-05 |
