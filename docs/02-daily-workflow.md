# Daily Workflow

## 1. Start The Stack

```bash
cd ~/rag-project
podman compose up -d
```

## 2. Confirm Services

```bash
podman compose ps
curl -i http://localhost:8000/health
```

Healthy behavior:

1. all four services show `Up`
2. gateway health returns HTTP `200`

## 3. Add Documents

### Option A: Put files into `source_docs`

```bash
cp ~/Desktop/my-document.pdf ~/rag-project/source_docs/
```

Recursive nested folders are fine. The first directory under `source_docs/` is inferred as the company:

```bash
mkdir -p ~/rag-project/source_docs/company_a
cp ~/Desktop/policy.pdf ~/rag-project/source_docs/company_a/
```

In this example, `company` is inferred as `company_a`, `company_source` is `path_inferred`.

### Option B: Upload through the API

```bash
curl -i -X POST http://localhost:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "file=@/Users/imac/Desktop/rag-smoke.txt"
```

Set company explicitly on upload:

```bash
curl -i -X POST http://localhost:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "file=@/Users/imac/Desktop/rag-smoke.txt" \
  -F "company=Acme Corp"
```

Company precedence: explicit `company` field wins over folder inference over null (unscoped).

## 4. Monitor Ingestion

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

### Status meanings

| Status | Meaning |
|--------|---------|
| `pending` | known to gateway, not yet submitted |
| `submitted` | sent to LightRAG, has `track_id` |
| `processing` | LightRAG is indexing |
| `ingested` | indexed and ready for retrieval |
| `failed` | ingestion failed — see error fields |

### Validation state meanings

| validation_state | Meaning |
|-----------------|---------|
| `accept` | passed validation gate |
| `warn` | passed but with warnings (check `warnings_json`) |
| `reject` | failed validation (check `error_message`) |
| `auto_split` | oversized — split the file and re-upload |

### Query readiness

A document is query-ready when:

1. `status` is `ingested` **and**
2. `query_ready` is `true`

If `status` is `ingested` but `query_ready` is `false`, check `readiness_reason` (e.g., `upstream_failed`, `superseded`).

Use the summary slice `by_query_ready` to count query-ready vs blocked documents:

```bash
curl -s http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY" | \
  jq '.summary.by_query_ready'
```

## 5. Query Through The API

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What are the key points in the new document?","mode":"mix"}'
```

Scope results to a company (returns only citations from documents with that company):

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What are the key points?","company":"Acme Corp","mode":"mix"}'
```

Expected response: `data.answer` and `data.citations`. The response includes `query_scope` showing which company scope was applied.

## 6. Use Open WebUI

```bash
open http://localhost:3000
```

Use it for: conversational research, threaded exploration, iterative follow-up questions.

## 6b. Inspect With LightRAG Web UI (Admin / Debug)

```bash
open http://macmini.local:9622
```

Use it for: browsing ingestion state visually, inspecting track status, debugging failures without curl.

**Do not use** for primary ingestion or conversational research.

## 7. Generate A Grounded Document

```bash
curl -i -X POST http://localhost:8000/generate-document \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"Draft a memo summarizing the new document","document_type":"memo"}'
```

## 8. End Of Day

If you want to stop the stack:

```bash
cd ~/rag-project
podman compose down
```
