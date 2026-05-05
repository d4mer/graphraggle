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

## Verification

- All 6 test scripts exist and are executable
- Scripts target http://localhost:8000 for gateway API
- LightRAG Web UI check targets http://macmini.local:9622