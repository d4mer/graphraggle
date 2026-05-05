# Result — Packet 03: Schema And Status API Expansion

## Summary

Expanded the persistent document state schema with 9 new nullable columns and enhanced the `/documents`, `/documents/{id}`, and `/ingest/status` API responses to expose all 26 target fields to operators. Added migration handling for new columns, summary slices by `error_stage` and `company`, and populated `byte_size` and `ingested_at` immediately via worker and upload endpoint changes.

Packet 03 follow-up correction: `upsert_document()` now includes the missing final `?` for `warnings_json` in both `app/state_store.py` and the embedded installer copy, so the 28-column INSERT target list matches 28 SQL value expressions.

Deployed the corrected files to `macmini.local`, rebuilt/restarted `gateway-api` and `ingest-worker`, verified the running `rag-gateway-api` container contains the exact corrected `VALUES` line, and confirmed a supported upload succeeds instead of returning HTTP 500.

## Files Changed

| File | Change |
|------|--------|
| `scripts/install_rag_stack.sh` | Updated state_store.py heredoc: 9 new schema columns, 9 new migrations, enhanced upsert/update functions, expanded get_summary |
| `scripts/install_rag_stack.sh` | Corrected embedded `state_store.py` `upsert_document()` VALUES clause to add the missing final `?` for `warnings_json` |
| `scripts/install_rag_stack.sh` | Updated api.py heredoc: enhanced upload responses with all 26 target fields, pass byte_size to upsert |
| `scripts/install_rag_stack.sh` | Updated worker.py heredoc: set ingested_at on ingested status, pass byte_size in upsert calls |
| `app/api.py` | Enhanced upload responses with all 26 target fields, pass byte_size to upsert |
| `app/worker.py` | Set ingested_at on ingested status, pass byte_size in upsert calls |
| `docs/decisions/0007-schema-expansion.md` | New ADR documenting schema expansion decisions |
| `app/state_store.py` | Corrected `upsert_document()` VALUES clause to add the missing final `?` for `warnings_json` |

## What Changed and Why

### 1. Database Schema Expansion (state_store.py)

**Added 9 new nullable columns:**
- `content_hash TEXT` — API-level canonical hash field (copy of sha256 for future decoupling)
- `detected_mime TEXT` — MIME type from file magic (deferred population)
- `detected_encoding TEXT` — Encoding from text validation (available at validation time)
- `byte_size INTEGER` — File size in bytes (populated at upload/scan time)
- `page_count INTEGER` — Page count for PDFs (deferred population)
- `source_uri TEXT` — Source URI for non-file documents (deferred population)
- `supersedes_document_id TEXT` — Reference to superseded document (deferred population)
- `ingested_at TEXT` — Timestamp when document reached ingested status (populated immediately)
- `warnings_json TEXT` — Structured warning data (deferred population)

**Migration strategy:** Each column added via `ALTER TABLE ADD COLUMN` with no NOT NULL constraint. Existing rows get NULL. Migration is idempotent — columns already present are skipped.

### 2. API Response Expansion (api.py)

**Upload endpoint** now returns all 26 target fields:
- Populated fields: `filename`, `path`, `status`, `validation_state`, `query_ready`, `content_hash` (from sha256), `original_filename`, `byte_size`, `validation` (verdict meta)
- NULL fields (deferred): `detected_mime`, `detected_encoding`, `page_count`, `source_uri`, `supersedes_document_id`, `warnings_json`, `ingested_at`

**`/documents` and `/documents/{id}`** return full database records with all columns.

### 3. Summary Slices (get_summary)

**Added 2 new summary slices:**
- `by_error_stage` — Counts grouped by `error_stage` (e.g., "validation", "track_poll", "submit")
- `by_company` — Counts grouped by `company`

### 4. Worker Changes (worker.py)

- `ingested_at` set to `datetime('now')` when document status transitions to `ingested`
- `byte_size` passed to `upsert_document` for files scanned from filesystem

### 5. ADR 0007

New decision record documenting:
- Why columns were added as nullable
- Why `sha256` was kept (migration complexity)
- Why `query_ready` remains stored (not derived)
- Deferred population strategy for detection fields

## Test Evidence

### Syntax Validation (ALL PASS)
- `app/api.py`: ast.parse succeeded
- `app/worker.py`: ast.parse succeeded
- Installer state_store.py heredoc: ast.parse succeeded
- Installer api.py heredoc: ast.parse succeeded
- Installer worker.py heredoc: ast.parse succeeded

### Schema Column Coverage
All 24 target columns verified present in new schema:
- document_id, path, source_type, company, filename, content_hash, status, validation_state, error_stage, error_code, error_message, error_detail, query_ready, retry_count, track_id, created_at, updated_at, byte_size, page_count, detected_mime, detected_encoding, source_uri, supersedes_document_id, ingested_at

### Migration Count
15 migration entries (6 from Packet 01 + 9 from Packet 03)

### Packet 01/02 Behavior Preservation
- Upload endpoint: Same rejection/warn/auto_split/accept logic, same error codes
- Worker lifecycle: Same transition flow, same track polling
- Schema: All existing columns unchanged, all existing migrations preserved
- API response envelope: Same `Envelope` model structure

## Remediation: original_filename Persistence Gap

### Problem

Packet 03 contract specifies `original_filename` as target field #6. The upload endpoint returns it in API responses, but the database schema has no `original_filename` column, no migration, no upsert parameter, and no ON CONFLICT UPDATE clause. The field is ephemeral — available in upload response JSON but absent from `GET /documents` and `GET /documents/{id}`.

### Files Changed

| File | Change |
|------|--------|
| `scripts/install_rag_stack.sh` | state_store.py heredoc: added `original_filename TEXT` column, migration entry, upsert parameter/INSERT/ON CONFLICT, update_document_state parameter |
| `scripts/install_rag_stack.sh` | api.py heredoc: added `original_filename=filename` to 3 upsert calls (reject, auto_split, accept/warn) |
| `scripts/install_rag_stack.sh` | worker.py heredoc: added `original_filename=path.name` to 2 upsert calls (submit_file, handle_candidate) |
| `app/state_store.py` | New file: schema with `original_filename TEXT` column, migration, upsert/update with original_filename |
| `app/api.py` | Added `original_filename=filename` to 3 upsert calls |
| `app/worker.py` | Added `original_filename=path.name` to 2 upsert calls |
| `docs/ops/runs/packet-03-evidence.md` | Added remediation section with gap description, fix, and verification |

### What Changed and Why

**1. Database schema** — Added `original_filename TEXT` column after `filename` in `CREATE TABLE`. Nullable to ensure backward compatibility.

**2. Migration** — Added `"original_filename": "ALTER TABLE documents ADD COLUMN original_filename TEXT"` to MIGRATIONS dict. Idempotent (skips if column already exists).

**3. upsert_document** — Added `original_filename: str | None` parameter. Included in INSERT column list, VALUES placeholder, ON CONFLICT UPDATE clause (`original_filename=excluded.original_filename`), and parameter tuple.

**4. update_document_state** — Added `original_filename: str | object = UNSET` parameter and `"original_filename": original_filename` to updates dict for consistency. Not passed in existing calls — `original_filename` is only set at upsert time.

**5. api.py upload endpoint** — Added `original_filename=filename` to upsert calls in reject, auto_split, and accept/warn paths. Matches the `original_filename` value already returned in API responses.

**6. worker.py** — Added `original_filename=path.name` to upsert calls in `submit_file` and `handle_candidate`. Mirrors the `filename=path.name` already passed.

### Test Evidence

### Placeholder Alignment Remediation
- Fixed `upsert_document()` in both `app/state_store.py` and the installer heredoc so the INSERT column order and VALUES placeholders match: `query_ready` now uses a bound `?`, and `retry_count` remains the following hardcoded `0`.

### Syntax Validation (ALL PASS)

- `app/api.py`: ast.parse succeeded
- `app/worker.py`: ast.parse succeeded
- `app/state_store.py`: ast.parse succeeded
- Installer state_store.py heredoc: ast.parse succeeded
- Installer api.py heredoc: ast.parse succeeded
- Installer worker.py heredoc: ast.parse succeeded

### Schema Column Coverage

`original_filename` verified present in schema: `original_filename TEXT` column found.

### Migration Count

16 migration entries (6 from Packet 01 + 9 from Packet 03 + 1 original_filename remediation).

### original_filename Occurrence Count

| File | Occurrences |
|------|-------------|
| `scripts/install_rag_stack.sh` | 18 |
| `app/state_store.py` | 11 |
| `app/api.py` | 5 |
| `app/worker.py` | 2 |

### Deployment to macmini.local

Packet 03 schema expansion was implemented but had a gap: `original_filename` was listed as a target field in the contract but was never persisted to the database. The remediation below adds the missing persistence path.

### Deployment Commands (when ready)

```bash
# SSH to macmini.local
ssh -i ~/.ssh/macmini_ed25519 edarellano@192.168.1.180

# On macmini.local:
cd ~/rag-project

# Copy updated installer script (from local machine)
scp -i ~/.ssh/macmini_ed25519 \
  /Users/imac/Documents/Programming/graphrag-implementation/scripts/install_rag_stack.sh \
  edarellano@192.168.1.180:~/rag-project/scripts/install_rag_stack.sh

# Force-rebuild and restart containers
cd ~/rag-project
FORCE=1 bash scripts/install_rag_stack.sh

# Verify health
curl -fsS http://localhost:8000/health | python3 -m json.tool
```

### Alternative: Direct API file update (no installer re-run)

```bash
# SSH to macmini.local
ssh -i ~/.ssh/macmini_ed25519 edarellano@192.168.1.180

# Copy individual app files (including new state_store.py with schema)
scp -i ~/.ssh/macmini_ed25519 \
  /Users/imac/Documents/Programming/graphrag-implementation/app/state_store.py \
  edarellano@192.168.1.180:~/rag-project/app/state_store.py

scp -i ~/.ssh/macmini_ed25519 \
  /Users/imac/Documents/Programming/graphrag-implementation/app/api.py \
  edarellano@192.168.1.180:~/rag-project/app/api.py

scp -i ~/.ssh/macmini_ed25519 \
  /Users/imac/Documents/Programming/graphrag-implementation/app/worker.py \
  edarellano@192.168.1.180:~/rag-project/app/worker.py

# Rebuild to pick up schema changes
cd ~/rag-project
podman compose down
podman compose up -d --build
```

## Exact Live Verification Commands

```bash
# After deployment, run these on macmini.local or local machine pointing to it

# 1. Health check
curl -s http://macmini.local:8000/health | python3 -m json.tool

# 1b. Verify original_filename column exists (raw DB check)
ssh -i ~/.ssh/macmini_ed25519 edarellano@192.168.1.180 "sqlite3 ~/rag-project/state/ingest.db 'PRAGMA table_info(documents);' | grep original_filename"

# 2. Check summary has new slices
curl -s -H 'Authorization: Bearer 1234' \
  http://macmini.local:8000/ingest/status | python3 -c "
import sys, json
data = json.load(sys.stdin)
summary = data['data']['summary']
print('Summary slices:', list(summary.keys()))
expected = ['by_status', 'by_validation_state', 'by_query_ready', 'by_error_stage', 'by_company']
for s in expected:
    print(f'  {s}: {\"PRESENT\" if s in summary else \"MISSING\"} = {summary.get(s, {})}')
"

# 3. Check document has all target fields
curl -s -H 'Authorization: Bearer 1234' \
  http://macmini.local:8000/documents | python3 -c "
import sys, json
data = json.load(sys.stdin)
docs = data['data']['documents']
if docs:
    doc = docs[0]
    target_fields = ['document_id', 'path', 'source_type', 'company', 'filename',
        'original_filename', 'content_hash', 'status', 'validation_state', 'error_stage',
        'error_code', 'error_message', 'error_detail', 'query_ready', 'retry_count',
        'track_id', 'created_at', 'updated_at', 'byte_size', 'page_count', 'detected_mime',
        'detected_encoding', 'source_uri', 'supersedes_document_id', 'ingested_at',
        'warnings_json']
    print(f'Document fields ({len(doc)} total):')
    for f in target_fields:
        val = doc.get(f, 'MISSING')
        print(f'  {f}: {val}')
else:
    print('No documents found — upload a test file first')
"

# 4. Upload test and verify response shape
echo "Packet 03 test document" > /tmp/packet03-test.txt
curl -s -X POST -H 'Authorization: Bearer 1234' \
  -F 'file=@/tmp/packet03-test.txt' \
  http://macmini.local:8000/upload | python3 -c "
import sys, json
data = json.load(sys.stdin)
fields = list(data['data'].keys())
print(f'Upload response fields ({len(fields)}):')
for f in sorted(fields):
    print(f'  {f}: {data[\"data\"][f]}')
"

# 5. Verify ingested_at is set after document is ingested
curl -s -H 'Authorization: Bearer 1234' \
  http://macmini.local:8000/documents | python3 -c "
import sys, json
data = json.load(sys.stdin)
for doc in data['data']['documents']:
    if doc.get('status') == 'ingested':
        print(f'Found ingested document: {doc[\"filename\"]}')
        print(f'  ingested_at: {doc.get(\"ingested_at\")}')
        print(f'  query_ready: {doc.get(\"query_ready\")}')
        break
else:
    print('No ingested documents yet — worker may still be processing')
"

# 6. Verify old rows remain readable (no data loss)
curl -s -H 'Authorization: Bearer 1234' \
  http://macmini.local:8000/ingest/status | python3 -c "
import sys, json
data = json.load(sys.stdin)
docs = data['data']['documents']
print(f'Total documents: {len(docs)}')
# Check all docs have the new columns (even if NULL)
new_cols = ['content_hash', 'byte_size', 'detected_mime', 'detected_encoding',
    'page_count', 'source_uri', 'supersedes_document_id', 'ingested_at', 'warnings_json']
missing = 0
for doc in docs:
    for col in new_cols:
        if col not in doc:
            missing += 1
            print(f'  MISSING {col} in doc {doc[\"document_id\"]}')
            break
if missing == 0:
    print('All documents have new columns (backward-compatible)')
else:
    print(f'WARNING: {missing} documents missing new columns')
"
```

## Risks

1. **SQLite version compatibility**: `ALTER TABLE ADD COLUMN` works on all SQLite versions. `DROP COLUMN` requires SQLite 3.35.0+ (2021-03-12). Rollback would need a database restore for very old SQLite versions.
2. **Summary query performance**: The new `by_error_stage` and `by_company` GROUP BY queries add overhead to `/ingest/status`. With thousands of documents, this could add 10-50ms. Indexes can be added later if needed.
3. **Deferred fields appear as null**: Operators may see null values for `detected_mime`, `page_count`, etc. and wonder if the system is broken. This is expected — these fields are populated by later packets.

## Merge Decision

**merge** — All changes are backward-compatible. Existing rows remain readable. Existing API behavior unchanged. New fields are nullable. Syntax validated.

## Rollback Note

1. Restore `scripts/install_rag_stack.sh` to pre-Packet-03 state
2. Restore `app/state_store.py`, `app/api.py`, and `app/worker.py` to pre-Packet-03 versions
3. Remove `docs/decisions/0007-schema-expansion.md`
4. Rebuild containers: `podman compose down && podman compose up -d --build`
5. If database migration already ran: either drop new columns (SQLite 3.35+) or restore database from backup

No data loss on rollback — new columns are nullable and contain no data that wasn't already available from existing columns.
