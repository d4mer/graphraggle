# Result - Packet 01: Unsupported File Rejection Remediation

## Summary

Added upload-time extension validation to the gateway `/upload` endpoint so unsupported files are immediately rejected with the correct Packet 01 contract fields (`status=rejected`, `validation_state=reject`, `error_stage=validation`, `error_code=unsupported_extension`) instead of lingering as `pending`.

## Files Changed

| File | Change |
|------|--------|
| `scripts/install_rag_stack.sh` (api.py heredoc) | Added `SUPPORTED_EXTENSIONS` constant; added extension check in `/upload` endpoint that creates rejection record and returns error envelope |
| `docs/ops/runs/packet-01-unsupported-file-rejection-review.md` | Updated acceptance checks with [x] marks, added root cause and fix notes |
| `CHANGELOG.md` | Added fix entry #7 |

## What Changed

### Before
```
POST /upload (file.bin)
  → file saved to /app/uploads/file.bin
  → document created: status=pending, validation_state=not_run
  → response: {ok: true, data: {status: "pending", ...}}
  → worker scans (30s later), rejects, but original pending record may persist
```

### After
```
POST /upload (file.bin)
  → extension check: .bin not in SUPPORTED_EXTENSIONS
  → document created: status=rejected, validation_state=reject, error_stage=validation, error_code=unsupported_extension
  → file NOT saved to disk
  → response: {ok: false, error: {code: "unsupported_extension", message: "File extension is not supported"}}
```

## Test Evidence

### Syntax Validation
- api.py: PASS (ast.parse succeeded)
- worker.py: PASS (ast.parse succeeded)

### Consistency Check
- Worker SUPPORTED:   `.csv .docx .htm .html .json .md .pdf .pptx .txt .xlsx`
- API SUPPORTED_EXT:  `.csv .docx .htm .html .json .md .pdf .pptx .txt .xlsx`
- Match: True

### Live Stack Verification (2026-05-04)

#### Unsupported File Upload Test
```
File: packet01-unsupported-test.bin (5 bytes)
Endpoint: POST /upload
Response: {"ok": false, "error": {"code": "unsupported_extension", "message": "File extension is not supported"}}
```

#### /documents Evidence
| Field | Value |
|-------|-------|
| document_id | d4b3c6a4-cc48-48a0-88d5-1246f8426fce |
| filename | packet01-unsupported-test.bin |
| status | rejected |
| validation_state | reject |
| error_stage | validation |
| error_code | unsupported_extension |
| error_message | File extension is not supported |
| query_ready | 0 |
| track_id | null |

#### /ingest/status Evidence
Same as /documents — confirmed rejection record persisted.

#### Supported File Upload Regression Test
```
File: packet01-supported-test.txt (41 bytes)
Endpoint: POST /upload
Response: {"ok": true, "data": {"filename": "packet01-supported-test.txt", "status": "pending", "validation_state": "not_run", "query_ready": false}}
```
Supported file returns normal non-rejected flow (status=pending, validation_state=not_run).

#### Contract Verification
- [x] status=rejected: VERIFIED
- [x] validation_state=reject: VERIFIED
- [x] error_stage=validation: VERIFIED
- [x] error_code=unsupported_extension: VERIFIED
- [x] query_ready=0/false: VERIFIED
- [x] track_id=null: VERIFIED (confirms document NOT in retry queue)

## Risks

1. **Minimal scope**: This fix only covers upload-time extension validation. The worker's existing rejection logic remains as a safety net for filesystem-placed files (source_docs). No regression expected.
2. **No file deletion**: Rejected files are not saved to disk, so no cleanup is needed.
3. **No API contract change**: The response structure uses the existing `Envelope` model. The `ok=false` response with error details is consistent with how FastAPI handles errors elsewhere.
4. **Duplicate extension definition**: `SUPPORTED_EXTENSIONS` in api.py duplicates the `SUPPORTED` set in worker.py. This is intentional for minimal scope — refactoring to a shared constant belongs in Packet 10 (source extraction cleanup).

## Rollback Note

To rollback, revert the `scripts/install_rag_stack.sh` file to its pre-change state. The api.py heredoc change is localized to the `/upload` endpoint and the `SUPPORTED_EXTENSIONS` constant. No database schema changes, no migrations needed. A simple re-run of `install_rag_stack.sh` with the original file restores the previous behavior.
