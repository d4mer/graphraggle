# GraphRAG Quick Reference

> At-a-glance guide — ingest to query in under a page.

## Prerequisites

| Item | Value |
|------|-------|
| Host | **macmini.local** |
| Gateway API | **http://localhost:8000** (local) or **http://macmini.local:8000** (remote) |
| Open WebUI | **http://macmini.local:3000** |
| LightRAG Web UI | **http://macmini.local:9622** |
| Bearer token | **RAG_API_KEY=1234** (or read `~/rag-project/.env` on macmini) |

---

## 1. Ingest

**Upload via API:**

```bash
curl -X POST http://macmini.local:8000/upload \
  -H "Authorization: Bearer 1234" \
  -F "file=@/path/to/file" \
  -F "company=My Company"   # optional
```

**Upload via filesystem** (worker picks up automatically):

```bash
cp /path/to/file ~/rag-project/source_docs/          # unscoped
cp /path/to/file ~/rag-project/source_docs/Acme/      # company=Acme
```

**Supported file types:** `.txt` `.md` `.pdf` `.docx` `.pptx` `.xlsx` `.csv` `.json` `.html` `.htm`

---

## 2. Validation Gate

| Result | Meaning |
|--------|---------|
| **accept** | Passed — will ingest |
| **warn** | Passed with warnings — will still ingest |
| **reject** | Failed — fix `error_message` and re-upload |
| **auto_split** | Too large (>500 KB for text) — split into smaller chunks and re-upload |

**Duplicate detection:** SHA-256 content hash. Re-uploading identical content is flagged.

---

## 3. Monitor Ingestion

```bash
# All documents
curl -s http://macmini.local:8000/ingest/status \
  -H "Authorization: Bearer 1234"

# Summary counts
curl -s http://macmini.local:8000/ingest/status \
  -H "Authorization: Bearer 1234" | jq '.summary'

# By company
curl -s http://macmini.local:8000/ingest/status \
  -H "Authorization: Bearer 1234" | jq '.summary.by_company'

# By query readiness
curl -s http://macmini.local:8000/ingest/status \
  -H "Authorization: Bearer 1234" | jq '.summary.by_query_ready'
```

> **Query-ready** when: `status == ingested` **AND** `query_ready == true`

---

## 4. Query

```bash
# All documents
curl -X POST http://macmini.local:8000/query \
  -H "Authorization: Bearer 1234" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the data say?"}'

# Company-scoped
curl -X POST http://macmini.local:8000/query \
  -H "Authorization: Bearer 1234" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the data say?","company":"My Company"}'
```

Defaults and fallback:

1. Omitted mode defaults to `hybrid`.
2. Weak retrieval triggers a one-time fallback query in `naive` mode.
3. Transcript-like prompts use deeper retrieval settings.
4. For transcripts, include speaker/date anchors and a short exact quote fragment.

---

## 5. Reindex

```bash
# Reindex failed documents
curl -X POST http://macmini.local:8000/ingest/reindex \
  -H "Authorization: Bearer 1234" \
  -H "Content-Type: application/json" \
  -d '{"status":"failed"}'

# Force reindex one document
curl -X POST http://macmini.local:8000/ingest/reindex \
  -H "Authorization: Bearer 1234" \
  -H "Content-Type: application/json" \
  -d '{"document_id":"YOUR-DOC-ID","force":true}'

# Reindex GSK transcript/workshop docs (dry-run then apply)
./scripts/reindex_sct_docs.sh --endpoint http://macmini.local:8000 --token 1234
./scripts/reindex_sct_docs.sh --endpoint http://macmini.local:8000 --token 1234 --apply
```

---

## 6. Three Surfaces

| Surface | URL | Use for |
|---------|-----|---------|
| Gateway API | http://macmini.local:8000 | Upload, status, query, reindex, generate |
| Open WebUI | http://macmini.local:3000 | Conversational research |
| LightRAG Web UI | http://macmini.local:9622 | Admin / debug only |

---

## 7. Troubleshooting

| Problem | Fix |
|---------|-----|
| Document stuck at **auto_split** | Split file into <500 KB chunks and re-upload |
| Document stuck at **reject** | Check `error_message` and `error_code`; fix root cause, then reindex or re-upload |
| Query returns nothing | Confirm doc is `status=ingested` **AND** `query_ready=true`; for scoped queries, confirm matching `company` |
| Duplicate rejection | Same content SHA-256 already exists — use existing doc or rename if a separate copy is needed |
