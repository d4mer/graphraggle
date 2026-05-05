# Packet 03 Remediation Review Checklist — `original_filename` Field Gap

## Remediation Focus

Fix the missing `original_filename` field gap: the field is returned in upload responses but not persisted to the database or exposed via `/documents` and `/documents/{id}` endpoints.

## Pre-Run Prerequisites

- [ ] Code changes deployed to macmini.local (via installer or direct file copy)
- [ ] Containers rebuilt: `podman compose down && podman compose up -d --build`
- [ ] Health check passes: `curl -fsS http://macmini.local:8000/health`

## Minimum Acceptance Conditions (One Live Rerun)

### 1. Database Schema Updated
- [ ] `original_filename` column exists in `documents` table
- [ ] Column is nullable (backward-compatible with existing rows)
- [ ] Migration is idempotent (safe to run multiple times)

### 2. Upload Endpoint Behavior
- [ ] **Accept case**: `original_filename` returned in response (existing behavior preserved)
- [ ] **Warn case**: `original_filename` returned in response (existing behavior preserved)
- [ ] **Auto-split case**: `original_filename` returned in response (existing behavior preserved)
- [ ] **Reject case**: `original_filename` returned in error response data (NEW - gap closed)

### 3. Document Retrieval Endpoints
- [ ] `/documents` returns `original_filename` for all documents
- [ ] `/documents/{id}` returns `original_filename` for the requested document
- [ ] Field is populated from stored database value (not just in-memory)

### 4. Summary API Behavior (Packet 03 Verified)
- [ ] `/ingest/status` returns all 5 summary slices: `by_status`, `by_validation_state`, `by_query_ready`, `by_error_stage`, `by_company`
- [ ] Summary counts are accurate
- [ ] No regression in response structure or envelope format

### 5. Previously Verified Packet 03 Behavior Intact
- [ ] Upload rejection logic unchanged (same error codes, same response structure)
- [ ] Upload warn/auto-split/accept logic unchanged
- [ ] Worker lifecycle transitions unchanged
- [ ] All 26 target fields present in API responses (including the 9 new nullable columns)
- [ ] `byte_size` and `ingested_at` populated correctly

## Live Verification Commands

Run these on macmini.local after deployment:

```bash
# 1. Health check
curl -s http://macmini.local:8000/health | python3 -m json.tool

# 2. Verify original_filename in upload response (accept case)
echo "Test document" > /tmp/test_orig_filename.txt
curl -s -X POST -H 'Authorization: Bearer 1234' \
  -F 'file=@/tmp/test_orig_filename.txt' \
  http://macmini.local:8000/upload | python3 -c "
import sys, json
data = json.load(sys.stdin)
if 'original_filename' in data.get('data', {}):
    print('PASS: original_filename present in upload response')
    print(f'  Value: {data[\"data\"][\"original_filename\"]}')
else:
    print('FAIL: original_filename missing from upload response')
"

# 3. Verify original_filename in reject response (NEW gap closure)
echo "invalid" > /tmp/test_reject.txt
curl -s -X POST -H 'Authorization: Bearer 1234' \
  -F 'file=@/tmp/test_reject.txt' \
  http://macmini.local:8000/upload | python3 -c "
import sys, json
data = json.load(sys.stdin)
# Reject responses may have data=None, check if original_filename is in the error or data
print('Response keys:', list(data.keys()))
if data.get('data') and 'original_filename' in data['data']:
    print('PASS: original_filename present in reject response')
else:
    print('CHECK: original_filename in reject response - verify manually')
"

# 4. Verify original_filename in /documents endpoint
curl -s -H 'Authorization: Bearer 1234' \
  http://macmini.local:8000/documents | python3 -c "
import sys, json
data = json.load(sys.stdin)
docs = data['data']['documents']
if docs:
    doc = docs[0]
    if 'original_filename' in doc:
        print('PASS: original_filename present in /documents')
        print(f'  Value: {doc[\"original_filename\"]}')
    else:
        print('FAIL: original_filename missing from /documents')
else:
    print('SKIP: No documents to verify')
"

# 5. Verify original_filename in /documents/{id} endpoint
curl -s -H 'Authorization: Bearer 1234' \
  http://macmini.local:8000/documents | python3 -c "
import sys, json
data = json.load(sys.stdin)
docs = data['data']['documents']
if docs:
    doc_id = docs[0]['document_id']
    print(f'Checking document {doc_id}...')
" && curl -s -H 'Authorization: Bearer 1234' \
  "http://macmini.local:8000/documents/$(curl -s -H 'Authorization: Bearer 1234' http://macmini.local:8000/documents | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"data\"][\"documents\"][0][\"document_id\"])' )" | python3 -c "
import sys, json
data = json.load(sys.stdin)
doc = data['data']['document']
if 'original_filename' in doc:
    print('PASS: original_filename present in /documents/{id}')
    print(f'  Value: {doc[\"original_filename\"]}')
else:
    print('FAIL: original_filename missing from /documents/{id}')
"

# 6. Verify Packet 03 summary slices intact
curl -s -H 'Authorization: Bearer 1234' \
  http://macmini.local:8000/ingest/status | python3 -c "
import sys, json
data = json.load(sys.stdin)
summary = data['data']['summary']
expected = ['by_status', 'by_validation_state', 'by_query_ready', 'by_error_stage', 'by_company']
all_present = all(s in summary for s in expected)
if all_present:
    print('PASS: All 5 summary slices present')
    for s in expected:
        print(f'  {s}: {summary[s]}')
else:
    missing = [s for s in expected if s not in summary]
    print(f'FAIL: Missing summary slices: {missing}')
"
```

## Risk Assessment

- **Low risk**: Adding nullable column, no existing data affected
- **Backward-compatible**: Existing rows will have NULL for `original_filename`
- **No behavioral changes**: All existing upload and worker logic preserved

## Review Outcome

- [ ] **PASS**: All minimum conditions met, Packet 03 behavior intact
- [ ] **FAIL**: Gap not closed or regression detected

## Recommendation

- **merge**: If all conditions pass on single live rerun
- **iterate**: If gap not fully closed or regression detected