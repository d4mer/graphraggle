# Packet 03 Remediation Review Checklist — Upload HTTP 500 Regression

## Remediation Focus

Narrowly scoped verification: ensure the upload endpoint no longer returns HTTP 500 errors after the `original_filename` persistence gap fix. The fix added `original_filename` column, migration, upsert parameter, and ON CONFLICT UPDATE clause.

## Pre-Run Prerequisites

- [ ] Code changes deployed to macmini.local (via installer or direct file copy)
- [ ] Containers rebuilt: `podman compose down && podman compose up -d --build`
- [ ] Health check passes: `curl -fsS http://macmini.local:8000/health`

## Minimum Acceptance Conditions (One Live Rerun)

### 1. Supported Upload Works (No HTTP 500)
- [ ] HTTP 200 returned for supported file upload (.txt, .md, .pdf, .docx, .csv, etc.)
- [ ] Response contains `ok: true`
- [ ] Response contains `original_filename` field
- [ ] Document persisted to database with `original_filename` populated

### 2. Reject Upload Works (No HTTP 500)
- [ ] HTTP 200 (not 500) returned for unsupported file (.bin, .exe, etc.)
- [ ] Response contains `ok: false`
- [ ] Error envelope contains `error_code: unsupported_extension`
- [ ] Rejection record persisted with `original_filename`

### 3. original_filename Persistence Intact
- [ ] `original_filename` column exists in schema
- [ ] Upload response returns `original_filename` field
- [ ] `/documents` endpoint returns `original_filename` for uploaded docs
- [ ] `/documents/{id}` endpoint returns `original_filename` for uploaded docs

### 4. 5 Summary Slices Remain Intact
- [ ] `/ingest/status` returns `by_status`
- [ ] `/ingest/status` returns `by_validation_state`
- [ ] `/ingest/status` returns `by_query_ready`
- [ ] `/ingest/status` returns `by_error_stage`
- [ ] `/ingest/status` returns `by_company`

### 5. No Packet 01/02 Behavior Regressed
- [ ] Unsupported file rejection returns same error codes (`unsupported_extension`, `empty_file`)
- [ ] Supported file returns same validation flow (pending → validating → accepted → submitted)
- [ ] Worker lifecycle transitions unchanged
- [ ] All 26 target fields present in API responses

## Live Verification Commands

Run these on macmini.local after deployment:

```bash
# 1. Health check
curl -s http://macmini.local:8000/health | python3 -m json.tool

# 2. Supported upload (HTTP 500 regression test)
echo "Supported test document" > /tmp/test_supported.txt
curl -s -X POST -H 'Authorization: Bearer 1234' \
  -F 'file=@/tmp/test_supported.txt' \
  http://macmini.local:8000/upload | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('ok') == True and 'original_filename' in data.get('data', {}):
    print('PASS: Supported upload works (HTTP 200, original_filename present)')
else:
    print('FAIL: Supported upload broken')
    print(data)
"

# 3. Reject upload (HTTP 500 regression test)
echo "reject" > /tmp/test_reject.bin
curl -s -X POST -H 'Authorization: Bearer 1234' \
  -F 'file=@/tmp/test_reject.bin' \
  http://macmini.local:8000/upload | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('ok') == False and data.get('error', {}).get('code') == 'unsupported_extension':
    print('PASS: Reject upload works (HTTP 200, correct error)')
else:
    print('FAIL: Reject upload broken')
    print(data)
"

# 4. Verify original_filename in /documents endpoint
curl -s -H 'Authorization: Bearer 1234' \
  http://macmini.local:8000/documents | python3 -c "
import sys, json
data = json.load(sys.stdin)
docs = data['data']['documents']
uploaded = [d for d in docs if d.get('source_type') == 'upload']
if uploaded and 'original_filename' in uploaded[0]:
    print('PASS: original_filename persisted in /documents')
    print(f'  Value: {uploaded[0][\"original_filename\"]}')
else:
    print('FAIL: original_filename not persisted')
"

# 5. Verify 5 summary slices intact
curl -s -H 'Authorization: Bearer 1234' \
  http://macmini.local:8000/ingest/status | python3 -c "
import sys, json
data = json.load(sys.stdin)
summary = data['data']['summary']
expected = ['by_status', 'by_validation_state', 'by_query_ready', 'by_error_stage', 'by_company']
missing = [s for s in expected if s not in summary]
if not missing:
    print('PASS: All 5 summary slices present')
else:
    print(f'FAIL: Missing slices: {missing}')
"

# 6. Verify no Packet 01/02 regression (error codes unchanged)
curl -s -X POST -H 'Authorization: Bearer 1234' \
  -F 'file=@/tmp/test_reject.bin' \
  http://macmini.local:8000/upload | python3 -c "
import sys, json
data = json.load(sys.stdin)
error_code = data.get('error', {}).get('code')
if error_code == 'unsupported_extension':
    print('PASS: Packet 01 error code unchanged')
else:
    print(f'FAIL: Packet 01 regression — got {error_code}')
"
```

## Risk Assessment

- **Low risk**: Adding nullable column, no existing data affected
- **Backward-compatible**: Existing rows will have NULL for `original_filename`
- **No behavioral changes**: All existing upload and worker logic preserved
- **HTTP 500 root causes**: Likely missing migration, missing column in schema, or missing parameter in upsert (all addressed in fix)

## Review Outcome

- [ ] **PASS**: All minimum conditions met, no HTTP 500 regression, all behaviors intact
- [ ] **FAIL**: HTTP 500 still occurring or behavioral regression detected

## Recommendation

- **merge**: If all conditions pass on single live rerun
- **iterate**: If HTTP 500 still occurs or regression detected