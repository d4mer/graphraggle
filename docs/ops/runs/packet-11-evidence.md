# Packet 11 Evidence

## Files Created

| File | Purpose |
|------|---------|
| scripts/smoke_test.sh | Full happy path: upload → ingest → query → verify citations |
| scripts/validation_gate_test.sh | Tests size thresholds, encoding fallback, duplicate rejection |
| scripts/company_scoping_test.sh | Tests company upload field and scoped query filtering |
| scripts/reindex_test.sh | Tests /ingest/reindex endpoint with failed document |
| scripts/lightrag_webui_check.sh | Verifies LightRAG Web UI is reachable at port 9622 |
| scripts/run_all_tests.sh | Runs all test scripts, reports pass/fail |

## Test Approach

- All scripts are shell-based using curl against the gateway API
- Use RAG_API_KEY env var for Bearer token authentication
- Tests are idempotent and independently runnable
- Exit 0 = pass, non-zero = fail
- Scripts use `set -euo pipefail`
- Override ENDPOINT for remote testing: `ENDPOINT=http://macmini.local:8000`

## Live Stack Verification (macmini.local)

Executed 2026-05-05 against deployed stack at http://macmini.local:8000.

### Test 1: Full Happy Path
```
Upload small text file → curl POST /upload
Ingest → waited for status=ingested
Query → POST /query with citations
```
**Result: PASS** — file ingested (status=ingested, validation_state=accept, query_ready=true), query returned grounded answer with citations.

### Test 2: Validation Gate - Accept (<100KB)
```
Upload 51200-byte zero file → POST /upload
```
**Result: PASS** — response showed `validation_state: "accept"`.

### Test 3: Validation Gate - Auto-Split (>500KB)
```
Upload 614400-byte zero file → POST /upload
```
**Result: PASS** — response showed `validation_state: "auto_split"`, `error_code: "large_text_file"`, `readiness_reason: "split_required"`.

### Test 4: Duplicate Detection (SHA-256)
```
Upload "Duplicate test content" → POST /upload (first time)
Upload "Duplicate test content" → POST /upload (second time)
```
**Result: PASS** — second upload got `validation_state: "warn"`, `error_code: "duplicate_content"`, `duplicate_of` referencing first document.

### Test 5: Company Upload Field
```
Upload with company="Acme Corp" → POST /upload
```
**Result: PASS** — response showed `company: "Acme Corp"`, `company_source: "explicit"`, `company_source_detail: "request.company"`.

### Test 6: Company Path Inference
```
Source file at source_docs/GSK/packet07/gsk-fs.txt
```
**Result: PASS** — document has `company: "GSK"`, `company_source: "path_inferred"`, `company_source_detail: "source_docs.first_directory"`.

### Test 7: Scoped Query Filtering
```
Query with company="Acme Corp" → POST /query
```
**Result: PASS** — `query_scope.mode: "company"`, `included_citation_count: 0` (pending doc), unscoped docs correctly excluded (`excluded_citation_count: 20`).

### Test 8: Reindex Rejected Document
```
POST /ingest/reindex with rejected doc document_id + force=true
```
**Result: PASS** — `queued: true`, `worker_action: "rescan_and_resubmit"`, `previous_validation_state: "reject"`.

### Test 9: LightRAG Web UI Reachability
```
curl http://macmini.local:9622
```
**Result: PASS** — HTTP 307 (redirect, service responding).

## Script Fixes Applied

- `smoke_test.sh`: Fixed grep pattern `state` → `status`, completion check `completed` → `ingested`
- `validation_gate_test.sh`: Fixed endpoint `/ingest/upload` → `/upload`, added RAG_API_KEY check, fixed duplicate test logic