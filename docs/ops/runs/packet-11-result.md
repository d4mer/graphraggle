# Packet 11 Result

## Status: ACCEPTED

## Summary

Packet 11 creates a regression test and verification harness covering the happy path, validation gates, company scoping, reindex, and LightRAG Web UI accessibility. All tests verified against live stack on macmini.local.

## Live Stack Test Results (2026-05-05)

| Test | Expected | Observed | Status |
|------|----------|----------|--------|
| Upload → ingest → query | status=ingested, query_ready=true, citations returned | status=ingested, validation_state=accept, query_ready=true, answer with citations | PASS |
| <100KB file | validation_state=accept | validation_state=accept | PASS |
| >500KB file | validation_state=auto_split | validation_state=auto_split, error_code=large_text_file | PASS |
| Duplicate content | validation_state=warn, error_code=duplicate_content | validation_state=warn, error_code=duplicate_content | PASS |
| Company field upload | company_source=explicit | company=Acme Corp, company_source=explicit, company_source_detail=request.company | PASS |
| Path inference (source_docs/GSK/) | company=GSK, source=path_inferred | company=GSK, company_source=path_inferred | PASS |
| Scoped query (company=Acme) | unscoped docs excluded | excluded_citation_count=20, included_citation_count=0 | PASS |
| Reindex rejected doc | queued=true | queued=true, worker_action=rescan_and_resubmit | PASS |
| LightRAG Web UI | HTTP 200/307 | HTTP 307 | PASS |

## Script Fixes Applied During Verification

- `smoke_test.sh`: grep `"state"` → `"status"`, `"completed"` → `"ingested"`
- `validation_gate_test.sh`: endpoint `/ingest/upload` → `/upload`, added auth check, fixed duplicate test

## Acceptance Criteria

| Criterion | Status |
|-----------|--------|
| Smoke test on healthy stack | PASS — verified on live stack |
| Tests independently runnable | PASS |
| Tests documented | PASS |
| Test output indicates pass/fail | PASS |
| Validation gate behavior | PASS — auto_split at 500KB threshold confirmed |
| Duplicate detection | PASS — SHA-256 detection confirmed |
| Company attribution | PASS — explicit and path-inference both confirmed |
| Scoped query filtering | PASS — unscoped docs excluded |
| Reindex endpoint | PASS — rejected doc requeued |
| LightRAG Web UI | PASS — reachable at port 9622 |