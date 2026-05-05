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

Recursive nested folders are fine:

```bash
mkdir -p ~/rag-project/source_docs/company_a
cp ~/Desktop/policy.pdf ~/rag-project/source_docs/company_a/
```

### Option B: Upload through the API

```bash
curl -i -X POST http://localhost:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "file=@/Users/imac/Desktop/rag-smoke.txt"
```

## 4. Monitor Ingestion

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Status meanings:

1. `pending`: known to gateway, not yet submitted
2. `submitted`: sent to LightRAG, has `track_id`
3. `processing`: LightRAG is indexing
4. `ingested`: ready for retrieval
5. `failed`: ingestion failed

## 5. Query Through The API

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What are the key points in the new document?","mode":"mix"}'
```

Expected response:

1. `data.answer`
2. `data.citations`

## 6. Use Open WebUI

```bash
open http://localhost:3000
```

Use it for:
1. conversational research
2. threaded exploration
3. iterative follow-up questions

## 6b. Inspect With LightRAG Web UI (Admin / Debug)

```bash
open http://localhost:9622
```

Use it for:
1. browsing ingestion state visually
2. inspecting track status and internal health
3. debugging ingestion failures without curl commands

Do not use for primary ingestion or conversational research.

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
