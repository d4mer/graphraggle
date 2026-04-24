# Printable Handbook

## System Overview

This handbook is the condensed reference for operating the RAG stack on the Mac mini.

The stack consists of:

1. `lightrag-server`
2. `gateway-api`
3. `ingest-worker`
4. `open-webui`

Main URLs:

1. API docs: `http://localhost:8000/docs`
2. Open WebUI: `http://localhost:3000`

## Daily Startup

```bash
cd ~/rag-project
podman compose up -d
podman compose ps
curl -i http://localhost:8000/health
```

Expected result:

1. all containers are `Up`
2. health returns HTTP `200`

## Adding Documents

### Filesystem Path

```bash
cp ~/Desktop/my-document.pdf ~/rag-project/source_docs/
```

### Upload Path

```bash
curl -i -X POST http://localhost:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "file=@/path/to/file"
```

## Monitoring Ingestion

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Use these states:

1. `pending`
2. `submitted`
3. `processing`
4. `ingested`
5. `failed`

## Querying

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the document say?","mode":"mix"}'
```

Always confirm citations are present.

## Document Generation

```bash
curl -i -X POST http://localhost:8000/generate-document \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"Draft a report summarizing the document","document_type":"report"}'
```

Supported types:

1. `summary`
2. `memo`
3. `report`
4. `proposal`
5. `policy`
6. `brief`
7. `draft`

## Open WebUI

```bash
open http://localhost:3000
```

Use it for:

1. conversational research
2. follow-up questions
3. threaded exploration

## Logs And Diagnostics

```bash
podman logs rag-gateway-api --tail=200
podman logs rag-ingest-worker --tail=200
podman logs lightrag-server --tail=200
podman logs open-webui --tail=200
```

Track a specific ingestion job:

```bash
podman exec rag-gateway-api curl -i \
  -H "X-API-Key: $LIGHTRAG_API_KEY" \
  http://lightrag-server:9621/documents/track_status/YOUR_TRACK_ID
```

## Critical Known-Good Config

```bash
EMBEDDING_MODEL=mxbai-embed-large-v1
RERANK_MODEL=jina-reranker-v3-mlx
```

```yaml
image: ghcr.io/hkuds/lightrag:v1.4.15
```

## Known Rules

1. uploads stay in `uploads/`
2. ignore `source_docs/__enqueued__`
3. text files ingest via `/documents/text`
4. binary docs ingest via `/documents/upload`

## Shutdown

```bash
cd ~/rag-project
podman compose down
```
