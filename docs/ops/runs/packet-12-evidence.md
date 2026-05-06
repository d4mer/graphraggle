# Packet 12 Evidence

## Changed Files

1. `app/worker.py`
2. `scripts/reindex_sct_docs.sh`
3. `docs/06-api-reference.md`
4. `docs/03-admin-operations.md`
5. `docs/07-openwebui-guide.md`
6. `docs/09-operator-cheat-sheet.md`
7. `docs/quick-reference.md`
8. `scripts/README.md`
9. `CHANGELOG.md`
10. `docs/packets/12-retrieval-usability-hotfix.md`

## Verification Commands And Results

Command:

```bash
python3 -m py_compile app/models.py app/api.py app/generation.py app/worker.py
```

Result:

1. Exit code `0`
2. No syntax errors reported

## Before/After Behavior Notes

1. Before: worker submitted decoded text payloads without transcript-specific cleanup.
2. After: worker applies minimal preprocessing before `/documents/text` submission:
   - trims excessive whitespace
   - collapses repeated blank lines
   - removes safe timestamp noise patterns commonly found in transcripts
3. Before: no packet-specific helper for selecting SCT/workshop GSK docs for reindex.
4. After: `scripts/reindex_sct_docs.sh` supports dry-run selection via `/ingest/status` and apply mode that calls `/ingest/reindex` per `document_id`.
5. Before: docs still showed explicit `mode=mix` examples and did not describe Packet 12 defaults.
6. After: docs now describe default `hybrid`, bounded fallback to `naive`, transcript query guidance, and reindex helper usage.
