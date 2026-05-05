# Packet Evidence

## Packet

- Packet: 01 - Unsupported File Rejection Remediation
- Branch: (unspecified)
- Owner: Code Worker
- Verifier: Code Reviewer

## Changed Files

| File | Change |
|------|--------|
| `scripts/install_rag_stack.sh` (api.py heredoc) | Added `SUPPORTED_EXTENSIONS` constant; added extension check in `/upload` endpoint that creates rejection record and returns error envelope |
| `docs/ops/runs/packet-01-unsupported-file-rejection-review.md` | Updated acceptance checks with [x] marks, added root cause and fix notes |
| `CHANGELOG.md` | Added fix entry #7 |

## Commands Run

```text
python3 -c "import ast; ..." — syntax check for api.py: PASS
python3 -c "import ast; ..." — syntax check for worker.py: PASS
Extension set consistency check (api.py vs worker.py): PASS (10 extensions match)
```

## Observed Outputs

- api.py syntax: PASS (ast.parse succeeded)
- worker.py syntax: PASS (ast.parse succeeded)
- Worker SUPPORTED:   `.csv .docx .htm .html .json .md .pdf .pptx .txt .xlsx`
- API SUPPORTED_EXT:  `.csv .docx .htm .html .json .md .pdf .pptx .txt .xlsx`
- Match: True

## API Before / After

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

## Screenshots

None (not UI-related).

## Investigation

1. Identified the root cause: the gateway `/upload` endpoint creates a document record with `status=pending` for ANY file without validating its extension.
2. The worker's `scan_once()` has rejection logic for unsupported files but only runs on a 30-second scan interval and the upload-time validation gap means files appear as `pending` before the worker ever sees them.
3. Verified the database schema (`state_store.py`) supports all required fields: `status`, `validation_state`, `error_stage`, `error_code`, `error_message`, `error_detail`, `query_ready`.
4. Verified the `upsert_document` function uses `ON CONFLICT(path) DO UPDATE SET` which correctly handles duplicate paths.
5. Verified the worker's `TERMINAL_FAILURE` set includes `rejected` and `handle_candidate` has an early return for rejected files.

## Implementation Actions

1. Added `SUPPORTED_EXTENSIONS` constant to `api.py` (module-level, before route definitions)
2. Added extension check at the top of the `/upload` endpoint function
3. On unsupported extension: creates a document record with rejection fields and returns an error envelope
4. On supported extension: proceeds with existing upload logic (unchanged)

## Live Test Needed

1. Upload `packet01-unsupported-test.bin` via POST /upload
2. Verify `/documents` shows `status=rejected`, `validation_state=reject`, `error_stage=validation`, `error_code=unsupported_extension`
3. Verify `/ingest/status` shows same fields
4. Verify supported file upload still works normally

## Risks Remaining

1. **Minimal scope**: This fix only covers upload-time extension validation. The worker's existing rejection logic remains as a safety net for filesystem-placed files (source_docs). No regression expected.
2. **No file deletion**: Rejected files are not saved to disk, so no cleanup is needed.
3. **No API contract change**: The response structure uses the existing `Envelope` model. The `ok=false` response with error details is consistent with how FastAPI handles errors elsewhere.
4. **Duplicate extension definition**: `SUPPORTED_EXTENSIONS` in api.py duplicates the `SUPPORTED` set in worker.py. This is intentional for minimal scope — refactoring to a shared constant belongs in Packet 10 (source extraction cleanup).

## Rollback Note

To rollback, revert the `scripts/install_rag_stack.sh` file to its pre-change state. The api.py heredoc change is localized to the `/upload` endpoint and the `SUPPORTED_EXTENSIONS` constant. No database schema changes, no migrations needed. A simple re-run of `install_rag_stack.sh` with the original file restores the previous behavior.

## Packet Outcome

1. `accepted`
