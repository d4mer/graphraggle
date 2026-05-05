# Packet Evidence

## Packet

- Packet: Packet 06 - Query Readiness And Reindex Controls
- Branch: master
- Owner: OpenCode (gpt-5.4)
- Verifier: OpenCode (gpt-5.4)

## Changed Files

1. `app/models.py` - checked-in API model contract and expanded `ReindexRequest`
2. `app/state_store.py` - readiness derivation, readiness reason exposure, readiness summary slice, and reindex selectors
3. `app/api.py` - readiness-aware payloads and real `/ingest/reindex` semantics
4. `app/worker.py` - worker consumption of explicit reindex markers
5. `docs/06-api-reference.md` - API usage updates
6. `docs/03-admin-operations.md` - operator commands
7. `docs/ops/runs/packet-06-result.md` - packet summary
8. `docs/ops/runs/packet-06-evidence.md` - evidence record
9. `CHANGELOG.md` - Packet 06 changelog entry

## Readiness Rules

1. `query_ready=true` only when the row is `status=ingested` and not blocked by supersession, rejection, or `auto_split` state.
2. `readiness_reason` values exposed by the API are:
   1. `ingested`
   2. `pending_validation`
   3. `validation_rejected`
   4. `awaiting_submit`
   5. `processing_upstream`
   6. `upstream_failed`
   7. `split_required`
   8. `superseded`
3. The stored `query_ready` column remains for compatibility, but API output is derived from current document state.

## Reindex Semantics

1. `/ingest/reindex` requires at least one selector.
2. Supported selectors are `document_id`, `path`, `status`, `validation_state`, `company`, and `source_type`.
3. Non-forced reindex blocks rows that are already query-ready, validation-rejected, split-required, or currently submitted/processing.
4. Forced reindex marks the row back to `pending`, clears `track_id`, resets `retry_count`, clears `ingested_at`, stamps `error_stage=reindex`, and sets worker-visible reindex metadata.
5. The worker consumes that marker and resubmits through the existing validation and LightRAG submission flow.

## Commands Run

```text
# Syntax-check Packet 06 runtime files without importing runtime dependencies
python3 -m py_compile app/models.py app/state_store.py app/api.py app/worker.py

# Verify readiness helpers and reindex marker helper exist in source
python3 -c "import ast, pathlib; files = ['app/state_store.py', 'app/api.py', 'app/worker.py']; trees = {f: ast.parse(pathlib.Path(f).read_text(), f) for f in files}; names = {f: {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))} for f, tree in trees.items()}; assert 'derive_readiness' in names['app/state_store.py']; assert 'select_documents_for_reindex' in names['app/state_store.py']; assert 'reindex' in names['app/api.py']; assert 'is_reindex_requested' in names['app/worker.py']; print('packet06 helpers: PASS')"

# Verify readiness reason and reindex strings are present in checked-in source
python3 -c "from pathlib import Path; source = Path('app/state_store.py').read_text() + Path('app/api.py').read_text() + Path('app/worker.py').read_text(); required = ['readiness_reason', 'by_readiness_reason', 'reindex_requested', 'Operator requested reindex']; missing = [item for item in required if item not in source]; assert not missing, missing; print('packet06 strings: PASS')"
```

## Observed Outputs

```text
app/models.py, app/state_store.py, app/api.py, app/worker.py: compiled successfully
packet06 helpers: PASS
packet06 strings: PASS
```

## Exact Live QA Commands

```bash
# 1. Confirm readiness fields and summary slice are exposed
curl -s http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"

# 2. Inspect one document's readiness reason
curl -s http://localhost:8000/documents/YOUR_DOCUMENT_ID \
  -H "Authorization: Bearer $RAG_API_KEY"

# 3. Queue all failed rows for worker-handled retry
curl -i -X POST http://localhost:8000/ingest/reindex \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status":"failed"}'

# 4. Force one specific row back through the worker
curl -i -X POST http://localhost:8000/ingest/reindex \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"document_id":"YOUR_DOCUMENT_ID","force":true}'

# 5. Watch worker/log state after requeue
podman logs rag-ingest-worker --tail=200

# 6. Confirm the row moved back through pending/submitted/processing and settles with the expected readiness reason
curl -s http://localhost:8000/documents \
  -H "Authorization: Bearer $RAG_API_KEY"
```

## Remaining Risks

1. Packet 06 derives readiness from current row state but full multi-version lineage still depends on later population of `supersedes_document_id`.
2. Forced reindex of duplicate-content rows still re-enters Packet 05 duplicate handling and may resolve back to a duplicate warning instead of a new submission.
3. `/query` itself is not gated on readiness in this packet by design; operators still need to use readiness state as the source of truth.
